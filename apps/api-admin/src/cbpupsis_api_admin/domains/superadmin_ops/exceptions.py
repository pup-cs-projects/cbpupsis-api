"""Errors the Superadmin operations domain raises."""

from __future__ import annotations

from cbpupsis_core.exceptions import (
    AppError,
    ConflictError,
    ForbiddenError,
    NotFoundError,
    ValidationError,
)


class SuperadminOpsError(AppError):
    """Base for every error raised by Superadmin operations."""


class UnknownOverrideRuleError(SuperadminOpsError, NotFoundError):
    def __init__(self) -> None:
        super().__init__("Unknown override rule")


class OverrideJustificationRequiredError(SuperadminOpsError, ValidationError):
    code = "OVERRIDE_JUSTIFICATION_REQUIRED"

    def __init__(self) -> None:
        super().__init__("Override justification is required")


class BackupNotFoundError(SuperadminOpsError, NotFoundError):
    def __init__(self) -> None:
        super().__init__("Backup not found")


class RestoreAlreadyRequestedError(SuperadminOpsError, ConflictError):
    def __init__(self) -> None:
        super().__init__("A restore request already exists")


class RestoreNotPendingError(SuperadminOpsError, ConflictError):
    def __init__(self) -> None:
        super().__init__("Restore request is not pending")


class DualAuthSameActorError(SuperadminOpsError, ForbiddenError):
    code = "DUAL_AUTH_SAME_ACTOR"

    def __init__(self) -> None:
        super().__init__("A different Superadmin must approve the restore")
