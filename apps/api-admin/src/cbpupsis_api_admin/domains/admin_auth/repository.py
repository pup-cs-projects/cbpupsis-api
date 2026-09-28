"""SQL access for Admin profiles, factors, and rolling lockout state."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_database.models.admin_auth import (
    AdminPosition,
    AdminProfile,
    AuthenticationFailure,
    AuthenticationLockout,
    AuthFailureStep,
    MfaCredential,
    MfaType,
)


async def get_admin_profile(
    db: AsyncSession, user_id: uuid.UUID
) -> AdminProfile | None:
    """Return one Admin persona.

    SQL::

        SELECT * FROM admin_profiles WHERE user_id = :user_id
    """
    return await db.scalar(select(AdminProfile).where(AdminProfile.user_id == user_id))


def add_admin_profile(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    position: AdminPosition,
    department: str | None,
    college: str | None,
) -> AdminProfile:
    """Stage an Admin persona.

    On commit::

        INSERT INTO admin_profiles (user_id, position, department, college)
        VALUES (:user_id, :position, :department, :college)
    """
    profile = AdminProfile(
        user_id=user_id,
        position=position.value,
        department=department,
        college=college,
        is_active=True,
    )
    db.add(profile)
    return profile


def update_admin_profile(
    db: AsyncSession, profile: AdminProfile, **fields: object
) -> AdminProfile:
    """Stage changes to an Admin persona.

    On commit, using the supplied fields::

        UPDATE admin_profiles SET position = :position WHERE user_id = :user_id
    """
    for name, value in fields.items():
        setattr(profile, name, value)
    return profile


async def get_mfa_credential(
    db: AsyncSession, *, user_id: uuid.UUID, mfa_type: MfaType
) -> MfaCredential | None:
    """Return the user's credential for one factor type.

    SQL::

        SELECT * FROM user_mfa_credentials
        WHERE user_id = :user_id AND mfa_type = :mfa_type
        ORDER BY created_at DESC LIMIT 1
    """
    return await db.scalar(
        select(MfaCredential)
        .where(
            MfaCredential.user_id == user_id,
            MfaCredential.mfa_type == mfa_type.value,
        )
        .order_by(MfaCredential.created_at.desc())
        .limit(1)
    )


def add_mfa_credential(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    mfa_type: MfaType,
    **fields: object,
) -> MfaCredential:
    """Stage a new factor credential.

    On commit::

        INSERT INTO user_mfa_credentials (user_id, mfa_type)
        VALUES (:user_id, :mfa_type)
    """
    credential = MfaCredential(user_id=user_id, mfa_type=mfa_type.value, **fields)
    db.add(credential)
    return credential


def update_mfa_credential(
    db: AsyncSession, credential: MfaCredential, **fields: object
) -> MfaCredential:
    """Stage changes to an MFA credential.

    On commit, using the supplied fields::

        UPDATE user_mfa_credentials SET is_enrolled = :is_enrolled WHERE id = :id
    """
    for name, value in fields.items():
        setattr(credential, name, value)
    return credential


async def acquire_authentication_lock(db: AsyncSession, user_id: uuid.UUID) -> None:
    """Serialize one account's sign-in state for this transaction.

    SQL::

        SELECT pg_advisory_xact_lock(hashtextextended(CAST(:user_id AS text), 0))
    """
    if db.get_bind().dialect.name != "postgresql":
        return
    await db.execute(
        text(
            "SELECT pg_advisory_xact_lock(hashtextextended(CAST(:user_id AS text), 0))"
        ),
        {"user_id": str(user_id)},
    )


def add_authentication_failure(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    step: AuthFailureStep,
    occurred_at: datetime,
) -> AuthenticationFailure:
    """Stage one failed sign-in step.

    On commit::

        INSERT INTO authentication_failures (user_id, step, occurred_at)
        VALUES (:user_id, :step, :occurred_at)
    """
    failure = AuthenticationFailure(user_id=user_id, step=step, occurred_at=occurred_at)
    db.add(failure)
    return failure


async def count_authentication_failures_since(
    db: AsyncSession, *, user_id: uuid.UUID, since: datetime
) -> int:
    """Count password and MFA failures in the rolling window.

    SQL::

        SELECT count(*) FROM authentication_failures
        WHERE user_id = :user_id AND occurred_at >= :since
    """
    count = await db.scalar(
        select(func.count())
        .select_from(AuthenticationFailure)
        .where(
            AuthenticationFailure.user_id == user_id,
            AuthenticationFailure.occurred_at >= since,
        )
    )
    return count or 0


async def clear_authentication_failures(
    db: AsyncSession, *, user_id: uuid.UUID
) -> None:
    """Remove the counter after completed sign-in.

    SQL::

        DELETE FROM authentication_failures WHERE user_id = :user_id
    """
    await db.execute(
        delete(AuthenticationFailure).where(AuthenticationFailure.user_id == user_id)
    )


async def get_authentication_lockout(
    db: AsyncSession, user_id: uuid.UUID
) -> AuthenticationLockout | None:
    """Return the account's current lock row.

    SQL::

        SELECT * FROM authentication_lockouts WHERE user_id = :user_id
    """
    return await db.scalar(
        select(AuthenticationLockout).where(AuthenticationLockout.user_id == user_id)
    )


def add_authentication_lockout(
    db: AsyncSession, *, user_id: uuid.UUID, locked_until: datetime
) -> AuthenticationLockout:
    """Stage a new account lock.

    On commit::

        INSERT INTO authentication_lockouts (user_id, locked_until)
        VALUES (:user_id, :locked_until)
    """
    lockout = AuthenticationLockout(user_id=user_id, locked_until=locked_until)
    db.add(lockout)
    return lockout


def update_authentication_lockout(
    db: AsyncSession,
    lockout: AuthenticationLockout,
    *,
    locked_until: datetime,
) -> AuthenticationLockout:
    """Extend an existing account lock.

    On commit::

        UPDATE authentication_lockouts SET locked_until = :locked_until
        WHERE user_id = :user_id
    """
    lockout.locked_until = locked_until
    return lockout


async def clear_authentication_lockout(db: AsyncSession, *, user_id: uuid.UUID) -> None:
    """Remove an expired or completed lock.

    SQL::

        DELETE FROM authentication_lockouts WHERE user_id = :user_id
    """
    await db.execute(
        delete(AuthenticationLockout).where(AuthenticationLockout.user_id == user_id)
    )
