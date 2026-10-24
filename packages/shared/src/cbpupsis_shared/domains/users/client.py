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
from collections.abc import Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_database.models.users import AdminPosition
from cbpupsis_shared.domains.users import service
from cbpupsis_shared.domains.users.schemas import (
    AccountCreate,
    AdminProfileRead,
    FacultyProfileRead,
    ScheduleIdentity,
    StudentProfileRead,
    UserRead,
)
from cbpupsis_shared.domains.users.service import (
    AccountProblem,
    SignInSubject,
    record_identity,
)

__all__ = [
    "AccountProblem",
    "SignInSubject",
    "StudentProfileRead",
    "admin_scope",
    "find_account_problems",
    "get_active_user",
    "get_admin_profile",
    "get_email_sign_in",
    "get_faculty_profile",
    "get_faculty_sign_in",
    "get_session_version",
    "get_student_by_number",
    "get_student_profile",
    "get_student_sign_in",
    "get_user",
    "invalidate_role_sessions",
    "lock_active_user",
    "lock_role_subject",
    "record_identity",
    "save_admin_profile",
    "schedule_identity",
    "schedule_names",
    "stage_admin_deactivation_override",
    "user_exists",
]


async def schedule_identity(
    db: AsyncSession, user_id: uuid.UUID
) -> ScheduleIdentity | None:
    return await service.schedule_identity(db, user_id)


async def schedule_names(
    db: AsyncSession, ids: list[uuid.UUID]
) -> dict[uuid.UUID, str]:
    return await service.schedule_names(db, ids)


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


async def get_email_sign_in(db: AsyncSession, email: str) -> SignInSubject | None:
    """Return who holds an email, for the Admin and Superadmin sign-ins."""
    return await service.get_email_sign_in(db, email)


async def get_session_version(db: AsyncSession, user_id: uuid.UUID) -> int | None:
    return await service.get_session_version(db, user_id)


async def get_student_sign_in(
    db: AsyncSession, student_number: str
) -> SignInSubject | None:
    """Return who holds a student number, for the student sign-in domain."""
    return await service.get_student_sign_in(db, student_number)


async def get_faculty_sign_in(
    db: AsyncSession, employee_id: str
) -> SignInSubject | None:
    """Return who holds an employee id, for the faculty sign-in domain."""
    return await service.get_faculty_sign_in(db, employee_id)


async def get_faculty_profile(
    db: AsyncSession, user_id: uuid.UUID
) -> FacultyProfileRead | None:
    return await service.get_faculty_profile(db, user_id)


async def get_admin_profile(
    db: AsyncSession, user_id: uuid.UUID
) -> AdminProfileRead | None:
    return await service.get_admin_profile(db, user_id)


async def save_admin_profile(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    position: AdminPosition,
    department: str | None,
    college: str | None,
) -> AdminProfileRead:
    return await service.save_admin_profile(
        db,
        user_id=user_id,
        position=position,
        department=department,
        college=college,
    )


async def get_active_user(db: AsyncSession, user_id: uuid.UUID) -> UserRead | None:
    """Return a live, active user as a DTO, or ``None``."""
    user = await service.get_active_user(db, user_id)
    return None if user is None else UserRead.model_validate(user)


async def lock_active_user(db: AsyncSession, user_id: uuid.UUID) -> UserRead | None:
    """Take the account row lock shared with resets; ``None`` if not usable."""
    user = await service.get_active_user_for_update(db, user_id)
    return None if user is None else UserRead.model_validate(user)


def admin_scope(
    position: AdminPosition, department: str | None, college: str | None
) -> tuple[str | None, str | None]:
    return service.admin_scope(position, department, college)


async def find_account_problems(
    db: AsyncSession, accounts: Sequence[AccountCreate]
) -> list[AccountProblem]:
    """Check new accounts against each other and existing accounts."""
    problems, _program_ids = await service.find_account_problems(db, accounts)
    return problems


async def get_student_profile(
    db: AsyncSession, user_id: uuid.UUID
) -> StudentProfileRead | None:
    """Return a user's Student profile, or None."""
    return await service.get_student_profile(db, user_id)


async def get_student_by_number(
    db: AsyncSession, student_number: str
) -> StudentProfileRead | None:
    """Return a student profile by student number, or None."""
    return await service.get_student_by_number(db, student_number)
