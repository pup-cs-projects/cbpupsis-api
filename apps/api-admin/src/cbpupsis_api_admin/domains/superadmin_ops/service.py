"""Execute privileged operations and their audit obligations atomically."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_api_admin.domains.superadmin_ops import repository
from cbpupsis_api_admin.domains.superadmin_ops.schemas import (
    OverrideRequest,
    OverrideResult,
    RestoreRequestRead,
)
from cbpupsis_core.exceptions import (
    ConflictError,
    ForbiddenError,
    NotFoundError,
    ValidationError,
)
from cbpupsis_database.models.superadmin_ops import SystemBackup
from cbpupsis_shared.domains.audit import client as audit_client
from cbpupsis_shared.domains.auth import client as auth_client
from cbpupsis_shared.domains.users import client as users_client

# Adding a rule is explicit; arbitrary paths or service names are never dispatched.
OVERRIDE_RULES = MappingProxyType(
    {"users.deactivate_admin": users_client.stage_admin_deactivation_override}
)


async def _audit_and_commit(db: AsyncSession, **entry: Any) -> None:
    try:
        audit_client.stage(db, **entry)
        await db.commit()
    except BaseException:
        await db.rollback()
        raise


async def execute_override(
    db: AsyncSession, *, actor_id: uuid.UUID, rule_id: str, data: OverrideRequest
) -> OverrideResult:
    operation = OVERRIDE_RULES.get(rule_id)
    if operation is None:
        raise NotFoundError("Unknown override rule")
    justification = (data.justification or "").strip()
    if not justification:
        raise ValidationError(
            "Override justification is required", code="OVERRIDE_JUSTIFICATION_REQUIRED"
        )
    await auth_client.require_superadmin_actor(db, actor_id)
    prior, new = await operation(db, actor_id=actor_id, user_id=data.target_id)
    await _audit_and_commit(
        db,
        action="superadmin.override",
        actor_id=actor_id,
        target_type="user",
        target_id=str(data.target_id),
        prior_state=prior,
        new_state=new,
        payload={
            "rule_id": rule_id,
            "justification": justification,
            "outcome": "success",
        },
    )
    return OverrideResult(rule_id=rule_id, target_id=data.target_id, result="applied")


def _snapshot(backup: SystemBackup) -> dict[str, object]:
    return {
        "restore_status": backup.restore_status,
        "requested_restore_by": str(backup.requested_restore_by)
        if backup.requested_restore_by
        else None,
        "approved_restore_by": str(backup.approved_restore_by)
        if backup.approved_restore_by
        else None,
    }


def _read(backup: SystemBackup) -> RestoreRequestRead:
    return RestoreRequestRead(
        backup_id=backup.id,
        status=backup.restore_status,
        requested_by=backup.requested_restore_by,
        approved_by=backup.approved_restore_by,
        requested_at=backup.restore_requested_at,
    )


async def request_restore(
    db: AsyncSession, *, actor_id: uuid.UUID, backup_id: uuid.UUID
) -> RestoreRequestRead:
    await auth_client.require_superadmin_actor(db, actor_id)
    backup = await repository.lock_backup(db, backup_id)
    if backup is None:
        raise NotFoundError("Backup not found")
    if backup.restore_status is not None:
        raise ConflictError("A restore request already exists")
    prior = _snapshot(backup)
    backup.requested_restore_by = actor_id
    backup.approved_restore_by = None
    backup.restore_status = "pending"
    backup.restore_requested_at = datetime.now(UTC)
    result = _read(backup)
    await _audit_and_commit(
        db,
        action="superadmin.backup_restore_requested",
        actor_id=actor_id,
        target_type="backup",
        target_id=str(backup_id),
        prior_state=prior,
        new_state=_snapshot(backup),
        payload={"outcome": "success"},
    )
    return result


async def approve_restore(
    db: AsyncSession, *, actor_id: uuid.UUID, backup_id: uuid.UUID
) -> RestoreRequestRead:
    await auth_client.require_superadmin_actor(db, actor_id)
    backup = await repository.lock_backup(db, backup_id)
    if backup is None:
        raise NotFoundError("Backup not found")
    if backup.restore_status != "pending" or backup.requested_restore_by is None:
        raise ConflictError("Restore request is not pending")
    if backup.requested_restore_by == actor_id:
        raise ForbiddenError(
            "A different Superadmin must approve the restore",
            code="DUAL_AUTH_SAME_ACTOR",
        )
    await auth_client.require_superadmin_actor(db, backup.requested_restore_by)
    prior = _snapshot(backup)
    backup.approved_restore_by = actor_id
    # Approval is authorization, never a claim that a restore has executed.
    backup.restore_status = "approved"
    result = _read(backup)
    await _audit_and_commit(
        db,
        action="superadmin.backup_restore_approved",
        actor_id=actor_id,
        target_type="backup",
        target_id=str(backup_id),
        prior_state=prior,
        new_state=_snapshot(backup),
        payload={"outcome": "success"},
    )
    return result
