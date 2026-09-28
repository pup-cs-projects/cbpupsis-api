"""FastAPI dependencies for enforcing authorization.

These build on the authenticated principal provided by the auth layer
(:func:`app.domains.auth.dependencies.get_current_user`) and the effective
permissions resolved by :func:`app.domains.iam.service.get_effective_permissions`.

Endpoints should depend on :func:`require_permission` (or
:func:`require_any_permission`) and phrase their requirement in terms of
*permissions*, never groups or policies. This keeps "who can do what"
configurable through data while endpoints stay stable.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.domains.auth.dependencies import (
    CurrentUser,
    get_current_user,
    require_admin_session,
)
from app.domains.iam import service as iam_service
from app.domains.iam.constants import ADMIN_GROUP
from app.domains.iam.exceptions import (
    InsufficientAdminRoleError,
    InsufficientPermissionsError,
    PermissionDeniedError,
)

logger = logging.getLogger(__name__)


def require_permission(
    *required: str,
) -> Callable[..., Awaitable[CurrentUser]]:
    """Build a dependency that requires *all* of ``required`` permissions.

    Usage — pass the action as a constant from the owning domain's
    ``constants.py``, never as a literal: a mistyped literal is an endpoint that
    403s for everyone, while a mistyped name fails at import::

        from app.domains.items.constants import MODERATE_ITEM

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


async def require_admin_role(
    request: Request,
    user: CurrentUser = Depends(require_admin_session),
    db: AsyncSession = Depends(get_db),
) -> CurrentUser:
    """Require the signed Admin role and current membership in ``Admins``."""
    if not await iam_service.is_user_in_group(db, user.id, ADMIN_GROUP):
        error = InsufficientAdminRoleError()
        logger.warning(
            "admin.authorization_refused",
            extra={
                "method": request.method,
                "path": request.url.path,
                "status_code": error.status_code,
                "error_code": error.code,
            },
        )
        raise error
    return user


def require_admin_permission(
    *required: str,
) -> Callable[..., Awaitable[CurrentUser]]:
    """Require a live, MFA-completed Admin and every named permission."""

    async def checker(
        user: CurrentUser = Depends(require_admin_role),
        db: AsyncSession = Depends(get_db),
    ) -> CurrentUser:
        granted = await iam_service.get_effective_permissions(db, user.id)
        if not set(required).issubset(granted):
            missing = sorted(set(required) - granted)
            raise PermissionDeniedError(missing)
        return user

    return checker


async def require_admin_scope(
    department_id: str | None = None,
    college_id: str | None = None,
    user: CurrentUser = Depends(require_admin_role),
) -> CurrentUser:
    """Apply position scope as a pre-handler dependency on resource routes."""
    user.require_resource_scope(
        department_id=department_id,
        college_id=college_id,
    )
    return user
