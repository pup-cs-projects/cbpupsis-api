"""Errors the IAM domain raises.

See ``app/domains/items/exceptions.py`` for why each domain owns its errors and
why they share a base class.
"""

from __future__ import annotations

from collections.abc import Iterable

from app.core.exceptions import AppError, ForbiddenError, NotFoundError


class IAMError(AppError):
    """Base for every error raised by the IAM domain."""


class PermissionDeniedError(IAMError, ForbiddenError):
    """The caller lacks a permission the endpoint requires (HTTP 403).

    Names the missing permissions rather than answering a bare "forbidden": an
    operator reading the log needs to know which grant is absent, and the set is
    not a secret — knowing ``ModerateItem`` exists does not confer it.

    This is the one message formatted in more than one domain, since a service
    can make the same check its endpoint's dependency already made. Both go
    through this class so the two cannot drift apart.
    """

    def __init__(self, permissions: Iterable[str]) -> None:
        super().__init__(f"Missing required permission(s): {sorted(permissions)}")


class InsufficientPermissionsError(IAMError, ForbiddenError):
    """The caller holds none of the permissions, any one of which would do
    (HTTP 403).

    Distinct from :class:`PermissionDeniedError`, which reports permissions that
    are *all* required. Reporting "any of" as "missing" would send an operator
    looking to grant the whole set.
    """

    def __init__(self, accepted: Iterable[str]) -> None:
        super().__init__(f"Requires at least one of: {sorted(accepted)}")


class UnknownPermissionsError(IAMError, NotFoundError):
    """A policy referenced actions that do not exist (HTTP 404).

    404 rather than 422: the request shape was valid, and the named permissions
    are genuinely absent. Silently creating a policy that grants nothing would
    look like success while denying every caller it was meant to allow.
    """

    def __init__(self, actions: Iterable[str]) -> None:
        super().__init__(f"Unknown permissions: {sorted(actions)}")


class PermissionNotFoundError(IAMError, NotFoundError):
    """No permission with this id (HTTP 404)."""

    def __init__(self, permission_id: int) -> None:
        super().__init__(f"Permission {permission_id} not found")


class PolicyNotFoundError(IAMError, NotFoundError):
    """No policy with this id (HTTP 404)."""

    def __init__(self, policy_id: int) -> None:
        super().__init__(f"Policy {policy_id} not found")


class GroupNotFoundError(IAMError, NotFoundError):
    """No group with this id (HTTP 404)."""

    def __init__(self, group_id: int) -> None:
        super().__init__(f"Group {group_id} not found")
