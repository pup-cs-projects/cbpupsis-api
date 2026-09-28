"""FastAPI dependencies for enforcing authorization.

These build on the authenticated principal provided by the auth layer
(:func:`cbpupsis_shared.domains.auth.dependencies.get_current_user`) and the
effective permissions resolved by
:func:`cbpupsis_shared.domains.iam.service.get_effective_permissions`.

Endpoints should depend on :func:`require_permission` (or
:func:`require_any_permission`) and phrase their requirement in terms of
*permissions*, never groups or policies. This keeps "who can do what"
configurable through data while endpoints stay stable.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_database.session import get_db
from cbpupsis_shared.domains.auth.dependencies import CurrentUser, get_current_user
from cbpupsis_shared.domains.iam import service as iam_service
from cbpupsis_shared.domains.iam.exceptions import (
    InsufficientPermissionsError,
    PermissionDeniedError,
)


def require_permission(
    *required: str,
) -> Callable[..., Awaitable[CurrentUser]]:
    """Build a dependency that requires *all* of ``required`` permissions.

    Usage — pass the action as a constant from the owning domain's
    ``constants.py``, never as a literal: a mistyped literal is an endpoint that
    403s for everyone, while a mistyped name fails at import::

        from cbpupsis_api_student.domains.items.constants import MODERATE_ITEM

        @router.delete("/items/{item_id}")
        async def delete_item(
            user: CurrentUser = Depends(require_permission(MODERATE_ITEM)),
        ):
            ...

    The returned dependency loads the user's effective permissions and raises
    :class:`ForbiddenError` (HTTP 403) unless every action in ``required`` is
    present. It returns the authenticated user on success so the endpoint can
    use it directly.
    """

    async def checker(
        user: CurrentUser = Depends(get_current_user),
        db: AsyncSession = Depends(get_db),
    ) -> CurrentUser:
        granted = await iam_service.get_effective_permissions(db, user.id)
        if not set(required).issubset(granted):
            missing = sorted(set(required) - granted)
            raise PermissionDeniedError(missing)
        return user

    return checker


def require_any_permission(
    *accepted: str,
) -> Callable[..., Awaitable[CurrentUser]]:
    """Build a dependency that requires *at least one* of ``accepted``.

    Useful when multiple distinct permissions should each grant access to the
    same endpoint. Raises :class:`ForbiddenError` if the user has none of them.
    """

    async def checker(
        user: CurrentUser = Depends(get_current_user),
        db: AsyncSession = Depends(get_db),
    ) -> CurrentUser:
        granted = await iam_service.get_effective_permissions(db, user.id)
        if not set(accepted) & granted:
            raise InsufficientPermissionsError(accepted)
        return user

    return checker
