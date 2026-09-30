"""Explicit Superadmin rules and two-person backup authorization."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_api_admin.domains.superadmin_ops import repository
from cbpupsis_api_admin.domains.superadmin_ops.exceptions import (
    BackupNotFoundError,
    DualAuthSameActorError,
    OverrideJustificationRequiredError,
    RestoreRequestConflictError,
    RuleNotFoundError,
)
from cbpupsis_api_admin.domains.superadmin_ops.schemas import (
    OverrideResult,
    RestoreAuthorizationRead,
)
from cbpupsis_shared.domains.audit import client as audit_client
from cbpupsis_shared.domains.auth.exceptions import SuperadminRoleRequiredError
from cbpupsis_shared.domains.iam import service as iam_service
from cbpupsis_shared.domains.iam.constants import ADMIN_GROUP, SUPERADMIN_GROUP
from cbpupsis_shared.domains.users import client as users_client


@dataclass(frozen=True)
class OverridableRule:
    target_type: str
    execute: Callable[
        [AsyncSession, uuid.UUID, uuid.UUID],
        Awaitable[tuple[dict[str, bool], dict[str, bool]]],
    ]


async def _deactivate_admin(
    db: AsyncSession, actor_id: uuid.UUID, target_id: uuid.UUID
) -> tuple[dict[str, bool], dict[str, bool]]:
    return await users_client.stage_admin_deactivation_override(
        db, actor_id=actor_id, user_id=target_id
    )


RULES: dict[str, OverridableRule] = {
    "users.deactivate_admin": OverridableRule("user", _deactivate_admin),
}


async def _require_superadmin(db: AsyncSession, actor_id: uuid.UUID) -> None:
    if (
        not await iam_service.is_user_in_group(db, actor_id, SUPERADMIN_GROUP)
        or await iam_service.is_user_in_group(db, actor_id, ADMIN_GROUP)
        or not await users_client.user_exists(db, actor_id)
    ):
        raise SuperadminRoleRequiredError


async def perform_override(
    db: AsyncSession,
    *,
    actor_id: uuid.UUID,
    rule_id: str,
    target_id: uuid.UUID,
    justification: str | None,
) -> OverrideResult:
    await _require_superadmin(db, actor_id)
    reason = (justification or "").strip()
    if not reason:
        raise OverrideJustificationRequiredError
    rule = RULES.get(rule_id)
    if rule is None:
        raise RuleNotFoundError
    prior, new = await rule.execute(db, actor_id, target_id)
    audit_client.stage(
        db,
        action="superadmin.override",
        actor_id=actor_id,
        target_type=rule.target_type,
        target_id=str(target_id),
        prior_state=prior,
        new_state=new,
        payload={"rule_id": rule_id, "justification": reason, "result": "success"},
    )
    await db.commit()
    return OverrideResult(
        rule_id=rule_id, target_type=rule.target_type, target_id=target_id
    )


def _read_backup(backup) -> RestoreAuthorizationRead:
    return RestoreAuthorizationRead(
        backup_id=backup.id,
        status=backup.restore_status,
        requested_by=backup.requested_restore_by,
        approved_by=backup.approved_restore_by,
        requested_at=backup.restore_requested_at,
    )


async def request_restore(
    db: AsyncSession, *, actor_id: uuid.UUID, backup_id: uuid.UUID
) -> RestoreAuthorizationRead:
    await _require_superadmin(db, actor_id)
    backup = await repository.get_backup_for_update(db, backup_id)
    if backup is None:
        raise BackupNotFoundError
    if backup.restore_status in {"pending", "approved"}:
        raise RestoreRequestConflictError
    prior = {"restore_status": backup.restore_status}
    backup.requested_restore_by = actor_id
    backup.approved_restore_by = None
    backup.restore_status = "pending"
    backup.restore_requested_at = datetime.now(UTC)
    audit_client.stage(
        db,
        action="superadmin.restore_requested",
        actor_id=actor_id,
        target_type="backup",
        target_id=str(backup_id),
        prior_state=prior,
        new_state={"restore_status": "pending"},
        payload={"result": "success"},
    )
    await db.commit()
    return _read_backup(backup)


async def approve_restore(
    db: AsyncSession, *, actor_id: uuid.UUID, backup_id: uuid.UUID
) -> RestoreAuthorizationRead:
    await _require_superadmin(db, actor_id)
    backup = await repository.get_backup_for_update(db, backup_id)
    if backup is None:
        raise BackupNotFoundError
    if backup.restore_status != "pending":
        raise RestoreRequestConflictError
    if backup.requested_restore_by == actor_id:
        raise DualAuthSameActorError
    requester_id = backup.requested_restore_by
    if (
        requester_id is None
        or not await iam_service.is_user_in_group(db, requester_id, SUPERADMIN_GROUP)
        or await iam_service.is_user_in_group(db, requester_id, ADMIN_GROUP)
        or not await users_client.user_exists(db, requester_id)
    ):
        raise RestoreRequestConflictError
    backup.approved_restore_by = actor_id
    backup.restore_status = "approved"
    audit_client.stage(
        db,
        action="superadmin.restore_approved",
        actor_id=actor_id,
        target_type="backup",
        target_id=str(backup_id),
        prior_state={"restore_status": "pending"},
        new_state={"restore_status": "approved"},
        payload={"result": "success"},
    )
    await db.commit()
    return _read_backup(backup)
