"""Business logic for the audit domain — recording and reading the trail.

This domain is a **subscriber**, not a caller: IAM and users emit events saying
what changed, and :func:`record_event` turns those into rows. Neither of those
domains imports this one, which is the property that matters — auditing must not
be something a future change to IAM can forget to do, and it must not be able to
fail an IAM operation either.

That independence is also the honest limitation. Dispatch is in-process and in
the background (see ``cbpupsis_core.events``), so a crash between the commit of an
IAM change and the write of its audit row loses the row. For a template that is
the right trade: the alternative that closes it — a transactional outbox, or
audit columns written in the same transaction — costs every domain a coupling to
this one. When an audit gap becomes a compliance problem rather than an
inconvenience, the outbox is the upgrade, and it goes here rather than in the
emitting domains.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_core.events import Event
from cbpupsis_core.pagination import Page
from cbpupsis_database.models.audit import AuditEntry
from cbpupsis_database.session import AsyncSessionLocal
from cbpupsis_shared.domains.audit import repository

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


async def record_event(event: Event) -> None:
    """Persist one event as an audit entry. The subscriber's entry point.

    Opens its **own** session rather than taking one, because it runs after the
    emitting request has returned: the session that produced the change is
    closed by then, and reusing a request-scoped session from a background task
    is a use-after-free that surfaces as an occasional
    ``InterfaceError``/``MissingGreenlet`` under load rather than a clean
    failure.

    Never raises. The event bus already isolates handler failures, but an audit
    write that fails must additionally not look like it succeeded, so the
    exception is logged by the bus and the trail simply lacks the row — a gap
    the module docstring is explicit about.

    The write itself is :func:`record_event_on`, which takes a session. Opening
    the session and doing the work were one function until it became clear that
    made the work untestable: a test cannot observe a row written through the
    application engine while the suite runs on in-memory SQLite, so the only
    assertion available was "it did not raise".
    """
    async with AsyncSessionLocal() as db:
        await record_event_on(db, event)


async def record_event_on(db: AsyncSession, event: Event) -> AuditEntry:
    """Write one event as an audit entry on ``db``, and commit.

    The half of :func:`record_event` that does the work. Split out so it can be
    driven against a test session — and so the outbox worker, which already owns
    a session per batch, can call it without opening a second one.
    """
    target_type, target_key = _TARGETS.get(event.name, (None, None))
    target_id = event.payload.get(target_key) if target_key else None

    actor = event.payload.get("actor_id")

    entry = repository.add_entry(
        db,
        action=event.name,
        occurred_at=datetime.now(UTC),
        actor_id=uuid.UUID(actor) if actor else None,
        target_type=target_type,
        target_id=str(target_id) if target_id is not None else None,
        payload=dict(event.payload),
    )
    await db.commit()
    return entry


async def record(
    db: AsyncSession,
    action: str,
    actor_id: uuid.UUID | None = None,
    target_type: str | None = None,
    target_id: str | None = None,
    payload: dict[str, Any] | None = None,
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
    )
    await db.commit()
    return entry


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
