"""Data access for the users domain. CRUD only: no rules, no authorization.

Every lookup here returns an ORM row or ``None``. The service turns ``None``
into :class:`~cbpupsis_core.exceptions.NotFoundError` where a caller expects the row
to exist, and leaves it as ``None`` where the absence is itself meaningful —
login must not distinguish "no such account" from "wrong password", so it needs
the ``None``, not an exception.

**Three lookups, not one with flags.** ``get_user``, ``get_active_user``, and
``get_user_by_email`` differ only in their WHERE clause, but they are separate
functions rather than one with boolean parameters because the filter is the
whole meaning of the call. ``get_active_user`` runs on every authenticated
request and its ``is_active`` predicate is what makes deactivation take effect
immediately; hiding that behind ``get_user(..., active_only=True)`` would make
the security-relevant default invisible at the call site and easy to get wrong.

All three exclude soft-deleted rows. There is deliberately no unfiltered "get
any user" here: nothing in the domain needs one, and offering it would make
resurrecting a deleted account a one-word change.

Transactions belong to the service: nothing here commits.
"""

from __future__ import annotations

import uuid

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_database.models.users import StudentProfile, User, UserProfile


def _live() -> Select[tuple[User]]:
    """Base query excluding soft-deleted rows, so no caller has to remember it."""
    return select(User).where(User.deleted_at.is_(None))


async def get_user(db: AsyncSession, user_id: uuid.UUID) -> User | None:
    """Return one live user by primary key, or ``None``.

    Raising for a missing row is the service's job — see the module docstring.

    SQL::

        SELECT users.email, users.password_hash, users.full_name,
               users.is_active, users.email_verified_at, users.display_name,
               users.bio, users.avatar_url, users.timezone, users.locale,
               users.phone_number, users.onboarding_completed_at,
               users.anonymized_at, users.id, users.created_at,
               users.updated_at, users.deleted_at
        FROM users
        WHERE users.deleted_at IS NULL AND users.id = :id_1::UUID
    """
    return await db.scalar(_live().where(User.id == user_id))


async def get_active_user(db: AsyncSession, user_id: uuid.UUID) -> User | None:
    """Return a user only if they exist, are active, and are not soft-deleted.

    Used on every authenticated request, so deactivation takes effect at once
    rather than when the current access token happens to expire.

    SQL::

        SELECT users.email, users.password_hash, users.full_name,
               users.is_active, users.email_verified_at, users.display_name,
               users.bio, users.avatar_url, users.timezone, users.locale,
               users.phone_number, users.onboarding_completed_at,
               users.anonymized_at, users.id, users.created_at,
               users.updated_at, users.deleted_at
        FROM users
        WHERE users.deleted_at IS NULL AND users.id = :id_1::UUID
          AND users.is_active IS true
    """
    return await db.scalar(_live().where(User.id == user_id, User.is_active.is_(True)))


async def get_user_by_email(db: AsyncSession, email: str) -> User | None:
    """Return one live user by email address, or ``None``.

    The caller is expected to pass an already-normalised (lower-cased) address:
    the column stores addresses lower-cased so uniqueness is case-insensitive,
    and normalising here as well would hide a caller that forgot to.

    SQL::

        SELECT users.email, users.password_hash, users.full_name,
               users.is_active, users.email_verified_at, users.display_name,
               users.bio, users.avatar_url, users.timezone, users.locale,
               users.phone_number, users.onboarding_completed_at,
               users.anonymized_at, users.id, users.created_at,
               users.updated_at, users.deleted_at
        FROM users
        WHERE users.deleted_at IS NULL AND users.email = :email_1
    """
    return await db.scalar(_live().where(User.email == email))


async def get_student_login_context(
    db: AsyncSession, student_number: str
) -> tuple[User, UserProfile | None] | None:
    """Return the user and birthdate profile for one student number.

    SQL::

        SELECT users.*, user_profiles.*
        FROM student_profiles
        JOIN users ON users.id = student_profiles.user_id
        LEFT JOIN user_profiles ON user_profiles.user_id = users.id
        WHERE student_profiles.student_number = :student_number
          AND users.deleted_at IS NULL
    """
    stmt = (
        select(User, UserProfile)
        .join(StudentProfile, StudentProfile.user_id == User.id)
        .outerjoin(UserProfile, UserProfile.user_id == User.id)
        .where(
            StudentProfile.student_number == student_number,
            User.deleted_at.is_(None),
        )
    )
    row = (await db.execute(stmt)).first()
    return None if row is None else (row[0], row[1])


def add_user(
    db: AsyncSession,
    email: str,
    password_hash: str,
    full_name: str | None = None,
) -> User:
    """Stage a new user on the session and return it.

    Synchronous because ``Session.add`` issues no SQL — the INSERT happens when
    the service commits, which is also where the unique-email integrity error
    surfaces and gets translated into a 409.

    Emits no SQL here. On the service's commit::

        INSERT INTO users (email, password_hash, full_name, is_active,
                           email_verified_at, display_name, bio, avatar_url,
                           timezone, locale, phone_number,
                           onboarding_completed_at, anonymized_at, id,
                           deleted_at)
        VALUES (:email, :password_hash, :full_name, :is_active,
                :email_verified_at, :display_name, :bio, :avatar_url,
                :timezone, :locale, :phone_number, :onboarding_completed_at,
                :anonymized_at, :id::UUID, :deleted_at)
        RETURNING users.created_at, users.updated_at
    """
    user = User(email=email, password_hash=password_hash, full_name=full_name)
    db.add(user)
    return user


def update_user(db: AsyncSession, user: User, **fields: object) -> User:
    """Write the given attributes onto a loaded user row and return it.

    Takes an already-loaded row because every caller has had to fetch it to
    apply a rule first (does it exist, is it anonymised, is the password right).

    This function does **not** decide which fields may be written — that is a
    business rule, and it lives in the service's ``EDITABLE_PROFILE_FIELDS``
    check. Passing an unknown attribute name here is a programming error and
    will fail loudly rather than being silently filtered.

    Emits no SQL here. On the service's commit, with the SET list built from
    whichever ``fields`` were passed — the variant below is
    ``full_name`` and ``bio``::

        UPDATE users
        SET full_name=:full_name, bio=:bio, updated_at=now()
        WHERE users.id = :id_1::UUID
    """
    for name, value in fields.items():
        setattr(user, name, value)
    return user


async def list_users(
    db: AsyncSession,
    limit: int,
    offset: int,
    is_active: bool | None = None,
    is_verified: bool | None = None,
) -> tuple[list[User], int]:
    """Return one page of live users and the total matching count.

    Both filters are tri-state: ``None`` means "do not filter", so an
    administrator can ask for active users, inactive users, or both without the
    endpoint needing three code paths. Verification is stored as a nullable
    timestamp rather than a boolean, so ``is_verified`` maps to an IS NULL /
    IS NOT NULL test rather than an equality.

    Ordered newest-first with ``User.id`` as a tiebreaker, for the same reason
    as the items listing: ``created_at`` is not unique, and without a unique
    second key adjacent pages can repeat or skip a row.

    Two statements. Both predicates shown below are present only when the
    corresponding argument is not ``None``; otherwise each keeps just the
    ``deleted_at IS NULL`` filter.

    SQL::

        -- 1. total matching rows, before limit/offset
        SELECT count(*) AS count_1
        FROM (SELECT users.email AS email,
                     users.password_hash AS password_hash,
                     users.full_name AS full_name,
                     users.is_active AS is_active,
                     users.email_verified_at AS email_verified_at,
                     users.display_name AS display_name, users.bio AS bio,
                     users.avatar_url AS avatar_url,
                     users.timezone AS timezone, users.locale AS locale,
                     users.phone_number AS phone_number,
                     users.onboarding_completed_at AS onboarding_completed_at,
                     users.anonymized_at AS anonymized_at, users.id AS id,
                     users.created_at AS created_at,
                     users.updated_at AS updated_at,
                     users.deleted_at AS deleted_at
              FROM users
              WHERE users.deleted_at IS NULL AND users.is_active IS true
                AND users.email_verified_at IS NOT NULL) AS anon_1

        -- 2. the page itself
        SELECT users.email, users.password_hash, users.full_name,
               users.is_active, users.email_verified_at, users.display_name,
               users.bio, users.avatar_url, users.timezone, users.locale,
               users.phone_number, users.onboarding_completed_at,
               users.anonymized_at, users.id, users.created_at,
               users.updated_at, users.deleted_at
        FROM users
        WHERE users.deleted_at IS NULL AND users.is_active IS true
          AND users.email_verified_at IS NOT NULL
        ORDER BY users.created_at DESC, users.id DESC
        LIMIT :param_1 OFFSET :param_2
    """
    query = _live()
    if is_active is not None:
        query = query.where(User.is_active.is_(is_active))
    if is_verified is not None:
        query = query.where(
            User.email_verified_at.is_not(None)
            if is_verified
            else User.email_verified_at.is_(None)
        )

    total = await db.scalar(select(func.count()).select_from(query.subquery()))
    result = await db.execute(
        query.order_by(User.created_at.desc(), User.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return list(result.scalars().all()), total or 0
