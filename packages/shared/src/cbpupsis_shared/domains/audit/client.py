"""Public staging interface for audit rows owned by another transaction."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from cbpupsis_shared.domains.audit import repository


def stage(
    db: AsyncSession | Session,
    *,
    action: str,
    actor_id: uuid.UUID,
    target_type: str | None = None,
    target_id: str | None = None,
    prior_state: dict[str, Any] | None = None,
    new_state: dict[str, Any] | None = None,
    payload: dict[str, Any] | None = None,
) -> None:
    repository.add_entry(
        db,
        action=action,
        actor_id=actor_id,
        target_type=target_type,
        target_id=target_id,
        prior_state=prior_state if prior_state is not None else {},
        new_state=new_state if new_state is not None else {},
        payload=payload or {},
        occurred_at=datetime.now(UTC),
    )
    db.info["superadmin_audit_staged"] = True


@event.listens_for(Session, "before_commit")
def _stage_unclaimed_superadmin_commit(session: Session) -> None:
    """Keep unclaimed Superadmin mutations in the same transaction as their audit."""
    actor = session.info.get("superadmin_actor")
    if actor is None or session.info.get("superadmin_audit_staged"):
        return
    stage(
        session,
        action="superadmin.request",
        actor_id=actor,
        target_type="endpoint",
        target_id=session.info.get("superadmin_request_path"),
        payload={
            "method": session.info.get("superadmin_request_method"),
            "outcome": "committed",
        },
    )
