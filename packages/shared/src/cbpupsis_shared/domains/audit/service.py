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

from sqlalchemy import event as sa_event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from cbpupsis_core.events import Event
from cbpupsis_core.pagination import Page
from cbpupsis_database.models.audit import AuditEntry, AuthAuditLog
from cbpupsis_database.session import AsyncSessionLocal
from cbpupsis_shared.domains.audit import repository
from cbpupsis_shared.domains.audit.constants import AUDIT_TARGETS, AUDITED_EVENTS
from cbpupsis_shared.domains.audit.schemas import (
    StudentRecentActivityRead,
    render_in_utc8,
)
from cbpupsis_shared.outbox import add_receipt, already_delivered


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
    if event.payload.get("_audit_recorded") is True:
        return None
    if message_id is not None and await already_delivered(db, message_id, "audit"):
        return None

    target_type, target_key = AUDIT_TARGETS.get(event.name, (None, None))
    target_id = event.payload.get(target_key) if target_key else None

    actor = event.payload.get("actor_id")
    prior_state, new_state = prior_and_new_state(event)

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


def prior_and_new_state(
    event: Event,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Return the event's before and after snapshots for the audit row.

    Explicit ``prior_state``/``new_state`` in the payload win; otherwise IAM
    attach, detach, and create events get safe defaults from their name.
    """
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


def stage(
    db,
    *,
    action,
    actor_id,
    target_type,
    target_id,
    prior_state=None,
    new_state=None,
    payload=None,
    occurred_at=None,
):
    """Stage a canonical row; never commit or copy credential-bearing bodies."""
    context = db.info.get("superadmin_audit")
    if context is not None and context["staged"]:
        return None
    entry = repository.add_entry(
        db,
        action=action,
        actor_id=actor_id,
        target_type=target_type,
        target_id=str(target_id),
        occurred_at=occurred_at if occurred_at is not None else datetime.now(UTC),
        prior_state=prior_state if prior_state is not None else {},
        new_state=new_state if new_state is not None else {},
        payload={"outcome": "success", **(payload or {})},
    )
    if context is not None:
        context["staged"] = True
    return entry


def capture_event(db, event: Event) -> None:
    """Make an existing domain event synchronous for privileged requests."""
    context = db.info.get("superadmin_audit")
    if context is None or event.name not in AUDITED_EVENTS:
        return
    target_type, key = AUDIT_TARGETS[event.name]
    prior, new = prior_and_new_state(event)
    stage(
        db,
        action=event.name,
        actor_id=context["actor"],
        target_type=target_type,
        target_id=event.payload.get(key),
        prior_state=prior,
        new_state=new,
        payload={k: v for k, v in event.payload.items() if not k.startswith("_")},
    )
    event.payload["_audit_recorded"] = True


def begin_superadmin_request(
    db: AsyncSession, actor_id: uuid.UUID, method: str, path: str
) -> dict:
    """Mark ``db`` as serving a superadmin request so its commit stages an audit."""
    context = {"actor": actor_id, "method": method, "path": path, "staged": False}
    db.info["superadmin_audit"] = context
    return context


@sa_event.listens_for(Session, "before_commit")
def stage_superadmin_audit_before_commit(session: Session) -> None:
    """Even legacy service commits must include their privileged audit row."""
    context = session.info.get("superadmin_audit")
    if context is not None and not context["staged"]:
        stage(
            session,
            action="superadmin.request",
            actor_id=context["actor"],
            target_type="endpoint",
            target_id=context["path"],
            payload={"method": context["method"]},
        )


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


def stage_record(
    db: AsyncSession,
    *,
    action: str,
    actor_id: uuid.UUID | None,
    ip_address: str | None,
    outcome: str,
    sessions_revoked: int | None = None,
) -> AuditEntry:
    """Append a credential-free auth row in the caller's business transaction."""
    payload = {
        "actor": str(actor_id) if actor_id else "anonymous",
        "ip_address": ip_address,
        "outcome": outcome,
    }
    if sessions_revoked is not None:
        payload["sessions_revoked"] = sessions_revoked
    return repository.add_entry(
        db,
        action=action,
        occurred_at=datetime.now(UTC),
        actor_id=actor_id,
        target_type="user" if actor_id else None,
        target_id=str(actor_id) if actor_id else None,
        payload=payload,
    )


def stage_sign_in_attempt(
    db: AsyncSession,
    *,
    attempted_id: str,
    user_id: uuid.UUID | None,
    ip_address: str | None,
    user_agent: str | None,
    success: bool,
    failure_reason: str | None = None,
    mfa_used: bool = False,
) -> None:
    """Append one credential-free sign-in attempt in the caller's transaction.

    The attempted identifier is recorded, never the password or code, and the
    row commits with the caller's lockout bookkeeping so neither can be lost
    without the other.
    """
    repository.add_sign_in_attempt(
        db,
        attempted_id=attempted_id[:50],
        user_id=user_id,
        ip_address=ip_address,
        user_agent=user_agent,
        success=success,
        failure_reason=failure_reason,
        mfa_used=mfa_used,
        occurred_at=datetime.now(UTC),
    )


async def list_sign_in_attempts(
    db: AsyncSession, limit: int = 50, offset: int = 0
) -> Page[AuthAuditLog]:
    """Return a page of sign-in attempts, newest first."""
    rows, total = await repository.list_sign_in_attempts(db, limit=limit, offset=offset)
    return Page(items=rows, total=total, limit=limit, offset=offset)


async def list_student_recent_activity(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    student_number: str | None = None,
    limit: int = 10,
) -> list[StudentRecentActivityRead]:
    """Return unified recent account and auth activity for a student."""
    auth_logs = await repository.list_student_auth_events(
        db, user_id=user_id, student_number=student_number or "", limit=limit
    )
    audit_entries = await repository.list_student_audit_entries(
        db, user_id=user_id, limit=limit
    )

    combined: list[tuple[datetime, StudentRecentActivityRead]] = []

    for log in auth_logs:
        act_type = "LOGIN_SUCCESS" if log.success else "LOGIN_FAILED"
        severity = "SUCCESS" if log.success else "WARNING"
        fail_msg = log.failure_reason or "Incorrect password entered."
        desc = (
            "Sign in successful."
            if log.success
            else f"Login attempt failed. {fail_msg}"
        )
        dt = log.created_at
        combined.append(
            (
                dt,
                StudentRecentActivityRead(
                    id=f"act_{str(log.id)[:8]}",
                    occurred_at=render_in_utc8(dt),
                    type=act_type,
                    severity=severity,
                    description=desc,
                ),
            )
        )

    for entry in audit_entries:
        action = entry.action.lower()
        if "enrollment" in action:
            act_type = "ENROLLMENT_SUBMITTED"
            severity = "SUCCESS"
            desc = entry.payload.get("description", "Successfully enrolled for term.")
        elif "cor" in action:
            act_type = "COR_GENERATED"
            severity = "SUCCESS"
            desc = entry.payload.get(
                "description", "Certificate of Registration generated."
            )
        elif "password" in action:
            act_type = "PASSWORD_CHANGED"
            severity = "INFO"
            desc = "Password was changed."
        elif "profile" in action:
            act_type = "PROFILE_UPDATED"
            severity = "INFO"
            desc = "Profile information updated."
        elif "grade" in action:
            act_type = "GRADE_POSTED"
            severity = "INFO"
            desc = "New grade posted."
        elif "payment" in action:
            act_type = "PAYMENT_RECEIVED"
            severity = "SUCCESS"
            desc = "Payment received."
        else:
            act_type = "PROFILE_UPDATED"
            severity = "INFO"
            desc = entry.payload.get("description", f"Action {entry.action} completed.")

        dt = entry.occurred_at
        combined.append(
            (
                dt,
                StudentRecentActivityRead(
                    id=f"act_{str(entry.id)[:8]}",
                    occurred_at=render_in_utc8(dt),
                    type=act_type,
                    severity=severity,
                    description=desc,
                ),
            )
        )

    combined.sort(key=lambda x: x[0], reverse=True)
    return [item[1] for item in combined[:limit]]
