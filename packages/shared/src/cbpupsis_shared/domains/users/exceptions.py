"""Errors the users domain raises.

See ``cbpupsis_api_student.domains.items.exceptions`` for why each domain
owns its errors and why they share a base class.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable

from cbpupsis_core.exceptions import (
    AppError,
    ForbiddenError,
    NotFoundError,
    UnauthorizedError,
    ValidationError,
)


class UsersError(AppError):
    """Base for every error raised by the users domain."""


class UserNotFoundError(UsersError, NotFoundError):
    """No live user with this id (HTTP 404)."""

    def __init__(self, user_id: uuid.UUID) -> None:
        super().__init__(f"User {user_id} not found")


class ProfileIncompleteError(UsersError, ValidationError):
    """Onboarding was completed with required profile fields still unset
    (HTTP 422).

    Lists the missing field names so the client can focus the right inputs
    rather than making the user hunt for them.
    """

    def __init__(self, missing: Iterable[str]) -> None:
        super().__init__(f"Missing required profile fields: {sorted(missing)}")


class IncorrectPasswordError(UsersError, UnauthorizedError):
    """The supplied password did not match (HTTP 401).

    Raised when confirming a destructive action — deleting an account — so a
    stolen access token alone is not enough to perform it.
    """

    def __init__(self) -> None:
        super().__init__("Password is incorrect")


class AccountDeletedError(UsersError, ValidationError):
    """A soft-deleted account cannot be reactivated (HTTP 422).

    Deletion scrubs the identifying fields, so there is nothing left to restore;
    reactivating would produce an account with no identity rather than the one
    the caller meant.
    """

    def __init__(self) -> None:
        super().__init__("A deleted account cannot be reactivated")


class ProfileAccessDeniedError(UsersError, ForbiddenError):
    """The caller may not read another user's profile (HTTP 403).

    Formats the message from the permission constant rather than restating it,
    so this and ``require_permission``'s own refusal cannot drift apart — they
    are the same refusal reported from two places.
    """

    def __init__(self, permission: str) -> None:
        super().__init__(f"Missing required permission(s): {[permission]}")


class CannotAdministerSelfError(UsersError, ValidationError):
    """An administrator tried to deactivate their own account (HTTP 422).

    Refused because it has no undo through the API: reactivating requires
    ``ManageUser``, which the caller has just cut themselves off from. Self
    deactivation is still available at ``POST /users/me/deactivate``, which is
    an explicit, deliberate choice rather than an administrative accident.
    """

    def __init__(self) -> None:
        super().__init__(
            "You cannot deactivate your own account through the admin endpoint. "
            "Use /users/me/deactivate if that is what you intend."
        )
