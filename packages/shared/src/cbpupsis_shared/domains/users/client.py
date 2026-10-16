"""Internal client for the users domain — the extraction swap point.

Other domains depend on THIS module, never on ``service.py`` directly. Today it
forwards to the local service; if users is ever extracted into its own service,
only this file changes to make an HTTP call, and its callers stay untouched.

It returns DTOs (``UserRead``), never ORM objects — an ORM object crossing a
domain boundary would carry a session and a table dependency with it, which is
exactly what a service split cannot preserve.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_shared.domains.users import service
from cbpupsis_shared.domains.users.schemas import UserRead


async def get_user(db: AsyncSession, user_id: uuid.UUID) -> UserRead:
    """Return a user as a DTO. Raises NotFoundError if absent."""
    user = await service.get_by_id(db, user_id)
    return UserRead.model_validate(user)
    # After extraction, this becomes roughly:
    # resp = await http_client.get(f"{settings.users_service_url}/users/{user_id}")
    # return UserRead.model_validate(resp.json())


async def user_exists(db: AsyncSession, user_id: uuid.UUID) -> bool:
    """Return whether a live user exists — the cross-domain integrity check
    that replaces a database-level foreign key."""
    return await service.get_active_user(db, user_id) is not None


async def stage_admin_deactivation_override(
    db: AsyncSession, *, actor_id: uuid.UUID, user_id: uuid.UUID
):
    return await service.stage_admin_deactivation_override(
        db, actor_id=actor_id, user_id=user_id
    )


async def invalidate_role_sessions(db: AsyncSession, user_id: uuid.UUID) -> None:
    from cbpupsis_shared.domains.auth import client as auth_client  # noqa: PLC0415

    await auth_client.stage_revoke_all_sessions(db, user_id)


async def lock_role_subject(db: AsyncSession, user_id: uuid.UUID) -> UserRead:
    return UserRead.model_validate(
        await service.get_role_subject_for_update(db, user_id)
    )
