"""Business logic for recording and reading the append-only audit trail.

Administrative domains stage events through the transactional outbox. The
worker calls :func:`record_event` after the business transaction commits, and a
delivery receipt makes retries idempotent. This preserves domain separation
without leaving a crash window between a state change and its audit obligation.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_core.events import Event
from cbpupsis_core.pagination import Page
from cbpupsis_database.models.audit import AuditEntry
from cbpupsis_database.session import AsyncSessionLocal
from cbpupsis_shared.domains.audit import repository
from cbpupsis_shared.outbox import add_receipt, already_delivered

#: Which payload key identifies the thing acted upon, per event. The audit row
#: keeps a generic (target_type, target_id) pair so one index answers "what
#: happened to this?" regardless of which domain emitted it.
_TARGETS: dict[str, tuple[str, str]] = {
    "iam.policy_attached_to_group": ("group", "group_id"),
    "iam.policy_detached_from_group": ("group", "group_id"),
    "iam.user_added_to_group": ("user", "user_id"),
    "iam.user_removed_from_group": ("user", "user_id"),
    "iam.policy_attached_to_user": ("user", "user_id"),
    "iam.policy_detached_from_user": ("user", "user_id"),
    "iam.permission_created": ("permission", "permission_id"),
    "iam.policy_created": ("policy", "policy_id"),
    "iam.group_created": ("group", "group_id"),
    "user.deactivated": ("user", "user_id"),
    "user.reactivated": ("user", "user_id"),
}


async def record_event(event: Event, *, message_id: uuid.UUID) -> None:
    """Persist one outbox event as an audit entry. The worker entry point.

    Opens its **own** session rather than taking one, because it runs after the
    emitting request has returned: the session that produced the change is
    closed by then, and reusing a request-scoped session from a background task
    is a use-after-free that surfaces as an occasional
    ``InterfaceError``/``MissingGreenlet`` under load rather than a clean
    failure.

    Failures propagate so the worker retries instead of marking an unwritten
    audit event as delivered.

    The write itself is :func:`record_event_on`, which takes a session. Opening
    the session and doing the work were one function until it became clear that
    made the work untestable: a test cannot observe a row written through the
    application engine while the suite runs on in-memory SQLite, so the only
    assertion available was "it did not raise".
    """
    async with AsyncSessionLocal() as db:
        await record_event_on(db, event, message_id=message_id)


async def record_event_on(
    db: AsyncSession,
    event: Event,
    *,
    message_id: uuid.UUID | None = None,
) -> AuditEntry | None:
    """Write one event and its optional delivery receipt atomically.

    The half of :func:`record_event` that does the work. Split out so it can be
    driven against a test session — and so the outbox worker, which already owns
    a session per batch, can call it without opening a second one.
    """
    if message_id is not None and await already_delivered(db, message_id, "audit"):
        return None

    target_type, target_key = _TARGETS.get(event.name, (None, None))
    target_id = event.payload.get(target_key) if target_key else None

    actor = event.payload.get("actor_id")
    prior_state, new_state = _event_states(event)

    entry = repository.add_entry(
        db,
        action=event.name,
        occurred_at=datetime.now(UTC),
        actor_id=uuid.UUID(actor) if actor else None,
        target_type=target_type,
        target_id=str(target_id) if target_id is not None else None,
        payload=dict(event.payload),
        prior_state=prior_state,
        new_state=new_state,
    )
    if message_id is not None:
        add_receipt(db, message_id, "audit")
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        if message_id is not None and await already_delivered(db, message_id, "audit"):
            return None
        raise
    return entry


async def record(
    db: AsyncSession,
    action: str,
    actor_id: uuid.UUID | None = None,
    target_type: str | None = None,
    target_id: str | None = None,
    payload: dict[str, Any] | None = None,
    prior_state: dict[str, Any] | None = None,
    new_state: dict[str, Any] | None = None,
) -> AuditEntry:
    """Write one entry on a caller-supplied session, and commit.

    The direct path, for a caller that already has a session and wants the row
    written synchronously with its own work — a script, or a future outbox.
    Ordinary application code should emit an event instead.
    """
    entry = repository.add_entry(
        db,
        action=action,
        occurred_at=datetime.now(UTC),
        actor_id=actor_id,
        target_type=target_type,
        target_id=target_id,
        payload=payload,
        prior_state=prior_state,
        new_state=new_state,
    )
    await db.commit()
    return entry


def _event_states(event: Event) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Return explicit snapshots, with safe defaults for existing IAM events."""
    prior = event.payload.get("prior_state")
    new = event.payload.get("new_state")
    if isinstance(prior, dict) or isinstance(new, dict):
        return (
            dict(prior) if isinstance(prior, dict) else None,
            dict(new) if isinstance(new, dict) else None,
        )

    if "_attached_" in event.name or event.name.endswith("user_added_to_group"):
        return {"attached": False}, {"attached": True}
    if "_detached_" in event.name or event.name.endswith("user_removed_from_group"):
        return {"attached": True}, {"attached": False}
    if event.name.endswith("_created"):
        return None, {
            key: value for key, value in event.payload.items() if key != "actor_id"
        }
    return None, None


async def list_entries(
    db: AsyncSession,
    limit: int = 50,
    offset: int = 0,
    action: str | None = None,
    actor_id: uuid.UUID | None = None,
    target_id: str | None = None,
) -> Page[AuditEntry]:
    """Return a page of audit entries, newest first."""
    rows, total = await repository.list_entries(
        db,
        limit=limit,
        offset=offset,
        action=action,
        actor_id=actor_id,
        target_id=target_id,
    )
    return Page(items=rows, total=total, limit=limit, offset=offset)
