"""Stable Superadmin operation errors."""

from cbpupsis_core.exceptions import (
    ConflictError,
    ForbiddenError,
    NotFoundError,
    ValidationError,
)


class OverrideJustificationRequiredError(ValidationError):
    code = "OVERRIDE_JUSTIFICATION_REQUIRED"

    def __init__(self) -> None:
        super().__init__("A non-empty justification is required")


class RuleNotFoundError(NotFoundError):
    def __init__(self) -> None:
        super().__init__("Overridable rule not found")


class BackupNotFoundError(NotFoundError):
    def __init__(self) -> None:
        super().__init__("Backup not found")


class RestoreRequestConflictError(ConflictError):
    def __init__(self) -> None:
        super().__init__("Restore authorization is not pending")


class DualAuthSameActorError(ForbiddenError):
    code = "DUAL_AUTH_SAME_ACTOR"

    def __init__(self) -> None:
        super().__init__("A second distinct Superadmin must approve")
