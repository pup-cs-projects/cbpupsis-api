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
from collections.abc import Collection
from datetime import date, datetime

from sqlalchemy import delete, func, select, union_all
from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_database.models.users import (
    AdminProfile,
    EmergencyContact,
    FacultyProfile,
    StudentProfile,
    User,
    UserAvatar,
    UserProfile,
)

#: The base of every user lookup: soft-deleted rows excluded once, here, so no
#: query can forget it. A ``Select`` is immutable, so sharing one is safe.
LIVE_USERS = select(User).where(User.deleted_at.is_(None))


async def get_record_identity(db: AsyncSession, student_id: uuid.UUID):
    """Read identity only for the already-authenticated owner's active account.

    SQL::

        SELECT s.student_number, u.full_name, p.first_name, p.last_name
        FROM users u JOIN student_profiles s ON s.user_id = u.id
        LEFT JOIN user_profiles p ON p.user_id = u.id
        WHERE u.id = :student_id AND u.deleted_at IS NULL AND u.is_active;
    """
    result = await db.execute(
        select(
            StudentProfile.student_number,
            User.full_name,
            UserProfile.first_name,
            UserProfile.last_name,
        )
        .join(StudentProfile, StudentProfile.user_id == User.id)
        .outerjoin(UserProfile, UserProfile.user_id == User.id)
        .where(
            User.id == student_id, User.deleted_at.is_(None), User.is_active.is_(True)
        )
    )
    return result.one_or_none()


async def schedule_identity(db: AsyncSession, user_id: uuid.UUID):
    """Read only the caller's institutional identity.

    SQL::

        SELECT sp.student_number, u.full_name, p.first_name, p.last_name
        FROM users u JOIN student_profiles sp ON sp.user_id = u.id
        LEFT JOIN user_profiles p ON p.user_id = u.id
        WHERE u.id = :user_id AND u.deleted_at IS NULL AND u.is_active = true;
    """
    return (
        await db.execute(
            select(
                StudentProfile.student_number,
                User.full_name,
                UserProfile.first_name,
                UserProfile.last_name,
            )
            .join(StudentProfile, StudentProfile.user_id == User.id)
            .outerjoin(UserProfile, UserProfile.user_id == User.id)
            .where(
                User.id == user_id, User.deleted_at.is_(None), User.is_active.is_(True)
            )
        )
    ).first()


async def schedule_names(db: AsyncSession, ids: list[uuid.UUID]):
    """Batch-read live faculty display names without exposing user records.

    SQL::

        SELECT u.id, u.full_name, p.first_name, p.last_name
        FROM users u LEFT JOIN user_profiles p ON p.user_id = u.id
        WHERE u.id IN (:ids) AND u.deleted_at IS NULL AND u.is_active = true;
    """
    if not ids:
        return []
    return (
        await db.execute(
            select(
                User.id, User.full_name, UserProfile.first_name, UserProfile.last_name
            )
            .outerjoin(UserProfile, UserProfile.user_id == User.id)
            .where(
                User.id.in_(ids), User.deleted_at.is_(None), User.is_active.is_(True)
            )
        )
    ).all()


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
    return await db.scalar(LIVE_USERS.where(User.id == user_id))


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
    return await db.scalar(
        LIVE_USERS.where(User.id == user_id, User.is_active.is_(True))
    )


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
    return await db.scalar(LIVE_USERS.where(User.email == email))


def add_user(
    db: AsyncSession,
    email: str,
    password_hash: str,
    full_name: str | None = None,
    *,
    user_id: uuid.UUID | None = None,
    email_verified_at: datetime | None = None,
    password_change_required: bool = False,
    temporary_password_expires_at: datetime | None = None,
) -> User:
    """Stage a new user on the session and return it.

    Synchronous because ``Session.add`` issues no SQL: the INSERT happens when
    the service commits, which is also where the unique-email integrity error
    surfaces and gets translated into a 409. ``user_id`` lets a caller stage
    rows that reference the user before a flush would assign one.

    Emits no SQL here. On the service's commit::

        INSERT INTO users (email, password_hash, full_name, is_active,
                           email_verified_at, password_change_required,
                           temporary_password_expires_at, display_name, bio,
                           avatar_url, timezone, locale, phone_number,
                           onboarding_completed_at, anonymized_at, id,
                           deleted_at)
        VALUES (:email, :password_hash, :full_name, :is_active,
                :email_verified_at, :password_change_required,
                :temporary_password_expires_at, :display_name, :bio,
                :avatar_url, :timezone, :locale, :phone_number,
                :onboarding_completed_at, :anonymized_at, :id::UUID,
                :deleted_at)
        RETURNING users.created_at, users.updated_at
    """
    user = User(
        email=email,
        password_hash=password_hash,
        full_name=full_name,
        email_verified_at=email_verified_at,
        password_change_required=password_change_required,
        temporary_password_expires_at=temporary_password_expires_at,
    )
    if user_id is not None:
        user.id = user_id
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
    query = LIVE_USERS
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


async def get_user_for_update(db: AsyncSession, user_id: uuid.UUID) -> User | None:
    """Serialize password changes and session issuance on the account row.

    SQL::

        SELECT * FROM users WHERE id = :user_id AND deleted_at IS NULL FOR UPDATE
    """
    return await db.scalar(
        LIVE_USERS.where(User.id == user_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


async def get_user_by_email_for_update(db: AsyncSession, email: str) -> User | None:
    """Authenticate against the credential protected by the account lock.

    SQL::

        SELECT * FROM users WHERE email = :email AND deleted_at IS NULL FOR UPDATE
    """
    return await db.scalar(
        LIVE_USERS.where(User.email == email)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


async def get_student_by_number(
    db: AsyncSession, student_number: str
) -> tuple[User, UserProfile | None] | None:
    """Return the live user holding a student number, with their profile.

    SQL::

        SELECT users.*, user_profiles.*
        FROM users
        JOIN student_profiles ON student_profiles.user_id = users.id
        LEFT OUTER JOIN user_profiles ON user_profiles.user_id = users.id
        WHERE users.deleted_at IS NULL
          AND student_profiles.student_number = :student_number
    """
    row = (
        await db.execute(
            select(User, UserProfile)
            .join(StudentProfile, StudentProfile.user_id == User.id)
            .outerjoin(UserProfile, UserProfile.user_id == User.id)
            .where(
                User.deleted_at.is_(None),
                StudentProfile.student_number == student_number,
            )
        )
    ).first()
    return None if row is None else (row[0], row[1])


async def get_faculty_by_employee_id(
    db: AsyncSession, employee_id: str
) -> tuple[User, FacultyProfile] | None:
    """Return the live user holding an employee id, with their faculty profile.

    SQL::

        SELECT users.*, faculty_profiles.*
        FROM users
        JOIN faculty_profiles ON faculty_profiles.user_id = users.id
        WHERE users.deleted_at IS NULL
          AND faculty_profiles.employee_id = :employee_id
    """
    row = (
        await db.execute(
            select(User, FacultyProfile)
            .join(FacultyProfile, FacultyProfile.user_id == User.id)
            .where(
                User.deleted_at.is_(None),
                FacultyProfile.employee_id == employee_id,
            )
        )
    ).first()
    return None if row is None else (row[0], row[1])


async def get_faculty_profile(
    db: AsyncSession, user_id: uuid.UUID
) -> FacultyProfile | None:
    """Return a user's faculty profile, or ``None``.

    SQL::

        SELECT * FROM faculty_profiles WHERE user_id = :user_id
    """
    return await db.get(FacultyProfile, user_id, populate_existing=True)


async def exists_role_profile(db: AsyncSession, user_id: uuid.UUID) -> bool:
    """Return whether a user holds a student or faculty profile.

    SQL::

        SELECT student_profiles.user_id FROM student_profiles
        WHERE student_profiles.user_id = :user_id
        UNION ALL
        SELECT faculty_profiles.user_id FROM faculty_profiles
        WHERE faculty_profiles.user_id = :user_id
        LIMIT 1
    """
    statement = union_all(
        select(StudentProfile.user_id).where(StudentProfile.user_id == user_id),
        select(FacultyProfile.user_id).where(FacultyProfile.user_id == user_id),
    ).limit(1)
    return (await db.execute(statement)).first() is not None


async def get_admin_profile(
    db: AsyncSession, user_id: uuid.UUID
) -> AdminProfile | None:
    """Return a user's Admin position profile, or ``None``.

    SQL::

        SELECT * FROM admin_profiles WHERE user_id = :user_id
    """
    return await db.get(AdminProfile, user_id, populate_existing=True)


def add_admin_profile(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    position: str,
    department: str | None,
    college: str | None,
    employee_id: str | None = None,
) -> AdminProfile:
    """Stage an Admin position profile.

    On commit::

        INSERT INTO admin_profiles (user_id, employee_id, position, department,
                                    college, is_active)
        VALUES (:user_id, :employee_id, :position, :department, :college, true)
    """
    profile = AdminProfile(
        user_id=user_id,
        employee_id=employee_id,
        position=position,
        department=department,
        college=college,
        is_active=True,
    )
    db.add(profile)
    return profile


def update_admin_profile(profile: AdminProfile, **fields: object) -> AdminProfile:
    """Stage changes to a loaded Admin position profile.

    On commit, using the supplied fields::

        UPDATE admin_profiles SET position = :position, ... WHERE user_id = :id
    """
    for name, value in fields.items():
        setattr(profile, name, value)
    return profile


def add_user_profile(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    first_name: str,
    last_name: str,
    birthdate: date | None,
) -> UserProfile:
    """Stage a user's personal-details profile.

    On commit::

        INSERT INTO user_profiles (user_id, first_name, last_name, birthdate,
                                   profile_completed, email_notifications)
        VALUES (:user_id, :first_name, :last_name, :birthdate, false, true)
    """
    profile = UserProfile(
        user_id=user_id,
        first_name=first_name,
        last_name=last_name,
        birthdate=birthdate,
    )
    db.add(profile)
    return profile


def add_student_profile(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    student_number: str,
    program_id: uuid.UUID,
    year_level: int,
) -> StudentProfile:
    """Stage a student profile.

    On commit::

        INSERT INTO student_profiles (user_id, student_number, program_id,
                                      year_level, enrollment_status, ...)
        VALUES (:user_id, :student_number, :program_id, :year_level,
                'pending', ...)
    """
    profile = StudentProfile(
        user_id=user_id,
        student_number=student_number,
        program_id=program_id,
        year_level=year_level,
    )
    db.add(profile)
    return profile


def add_faculty_profile(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    employee_id: str,
    department: str,
    college: str | None,
) -> FacultyProfile:
    """Stage a faculty profile.

    On commit::

        INSERT INTO faculty_profiles (user_id, employee_id, department,
                                      college, employment_type, is_active)
        VALUES (:user_id, :employee_id, :department, :college, 'full_time',
                true)
    """
    profile = FacultyProfile(
        user_id=user_id,
        employee_id=employee_id,
        department=department,
        college=college,
    )
    db.add(profile)
    return profile


async def list_taken_emails(db: AsyncSession, emails: Collection[str]) -> list[str]:
    """Return which of ``emails`` some row already holds, deleted rows included.

    Unlike every lookup above, soft-deleted rows count: the unique constraint
    covers them, so an address one holds cannot go to a new account.

    SQL::

        SELECT users.email FROM users
        WHERE users.email IN (__[POSTCOMPILE_email_1])
    """
    if not emails:
        return []
    return list(await db.scalars(select(User.email).where(User.email.in_(emails))))


async def list_taken_student_numbers(
    db: AsyncSession, numbers: Collection[str]
) -> list[str]:
    """Return which of ``numbers`` a student profile already holds.

    SQL::

        SELECT student_profiles.student_number FROM student_profiles
        WHERE student_profiles.student_number IN (__[POSTCOMPILE_number_1])
    """
    if not numbers:
        return []
    column = StudentProfile.student_number
    return list(await db.scalars(select(column).where(column.in_(numbers))))


async def list_taken_faculty_employee_ids(
    db: AsyncSession, employee_ids: Collection[str]
) -> list[str]:
    """Return which of ``employee_ids`` a faculty profile already holds.

    SQL::

        SELECT faculty_profiles.employee_id FROM faculty_profiles
        WHERE faculty_profiles.employee_id IN (__[POSTCOMPILE_employee_id_1])
    """
    if not employee_ids:
        return []
    column = FacultyProfile.employee_id
    return list(await db.scalars(select(column).where(column.in_(employee_ids))))


async def list_taken_admin_employee_ids(
    db: AsyncSession, employee_ids: Collection[str]
) -> list[str]:
    """Return which of ``employee_ids`` an Admin profile already holds.

    SQL::

        SELECT admin_profiles.employee_id FROM admin_profiles
        WHERE admin_profiles.employee_id IN (__[POSTCOMPILE_employee_id_1])
    """
    if not employee_ids:
        return []
    column = AdminProfile.employee_id
    return list(await db.scalars(select(column).where(column.in_(employee_ids))))


async def get_user_profile(db: AsyncSession, user_id: uuid.UUID) -> UserProfile | None:
    """Return a user's personal-details profile, or ``None``.

    SQL::

        SELECT * FROM user_profiles WHERE user_id = :user_id
    """
    return await db.get(UserProfile, user_id, populate_existing=True)


def update_user_profile(profile: UserProfile, **fields: object) -> UserProfile:
    """Stage changes onto a loaded ``UserProfile`` row.

    On commit, using the supplied fields::

        UPDATE user_profiles SET personal_email = :personal_email, ...
        WHERE user_id = :user_id
    """
    for name, value in fields.items():
        setattr(profile, name, value)
    return profile


async def get_student_profile(
    db: AsyncSession, user_id: uuid.UUID
) -> StudentProfile | None:
    """Return a user's student profile, or ``None``.

    SQL::

        SELECT * FROM student_profiles WHERE user_id = :user_id
    """
    return await db.get(StudentProfile, user_id, populate_existing=True)


async def list_emergency_contacts(
    db: AsyncSession, user_id: uuid.UUID
) -> list[EmergencyContact]:
    """Return all emergency contacts belonging to ``user_id``, primary first.

    SQL::

        SELECT * FROM emergency_contacts
        WHERE user_id = :user_id
        ORDER BY is_primary DESC, id ASC
    """
    result = await db.scalars(
        select(EmergencyContact)
        .where(EmergencyContact.user_id == user_id)
        .order_by(EmergencyContact.is_primary.desc(), EmergencyContact.id.asc())
    )
    return list(result.all())


async def get_emergency_contact(
    db: AsyncSession, contact_id: uuid.UUID
) -> EmergencyContact | None:
    """Return one emergency contact by primary key, or ``None``.

    SQL::

        SELECT * FROM emergency_contacts WHERE id = :contact_id
    """
    return await db.get(EmergencyContact, contact_id, populate_existing=True)


def add_emergency_contact(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    contact_name: str,
    relationship: str,
    phone_number: str,
    email: str | None = None,
    address: str | None = None,
    is_primary: bool = False,
) -> EmergencyContact:
    """Stage a new emergency contact row.

    On commit::

        INSERT INTO emergency_contacts (id, user_id, contact_name, relationship,
                                        phone_number, email, address, is_primary)
        VALUES (:id, :user_id, :contact_name, :relationship, :phone_number,
                :email, :address, :is_primary)
    """
    contact = EmergencyContact(
        user_id=user_id,
        contact_name=contact_name,
        relationship=relationship,
        phone_number=phone_number,
        email=email,
        address=address,
        is_primary=is_primary,
    )
    db.add(contact)
    return contact


def update_emergency_contact(
    contact: EmergencyContact, **fields: object
) -> EmergencyContact:
    """Stage changes onto a loaded ``EmergencyContact`` row.

    On commit, using the supplied fields::

        UPDATE emergency_contacts SET contact_name = :contact_name, ...
        WHERE id = :contact_id
    """
    for name, value in fields.items():
        setattr(contact, name, value)
    return contact


async def delete_emergency_contact(db: AsyncSession, contact: EmergencyContact) -> None:
    """Stage deletion of a loaded ``EmergencyContact`` row.

    On commit::

        DELETE FROM emergency_contacts WHERE id = :contact_id
    """
    await db.delete(contact)


async def replace_avatar(
    db: AsyncSession, *, user_id: uuid.UUID, content: bytes, content_type: str
) -> UserAvatar:
    """Stage ``user_id``'s profile picture, replacing any earlier one.

    On commit::

        DELETE FROM user_avatars WHERE user_id = :user_id
        INSERT INTO user_avatars (id, user_id, content_type, content)
        VALUES (:id, :user_id, :content_type, :content)
    """
    await db.execute(delete(UserAvatar).where(UserAvatar.user_id == user_id))
    avatar = UserAvatar(user_id=user_id, content=content, content_type=content_type)
    db.add(avatar)
    return avatar


async def get_avatar(db: AsyncSession, avatar_id: uuid.UUID) -> UserAvatar | None:
    """Return one stored profile picture by id, or ``None``.

    SQL::

        SELECT * FROM user_avatars WHERE id = :avatar_id
    """
    return await db.get(UserAvatar, avatar_id)


async def get_student_by_student_number(
    db: AsyncSession, student_number: str
) -> StudentProfile | None:
    """Return a student profile by student number.

    SQL::

        SELECT student_profiles.user_id, student_profiles.student_number,
               student_profiles.program_id, student_profiles.curriculum_id,
               student_profiles.year_level, student_profiles.section,
               student_profiles.enrollment_status, student_profiles.fhe_qualified,
               student_profiles.fhe_forfeited, student_profiles.fhe_forfeiture_reason,
               student_profiles.is_graduating_senior,
               student_profiles.has_financial_hold,
               student_profiles.has_academic_hold
        FROM student_profiles
        WHERE student_profiles.student_number = :student_number
    """
    return await db.scalar(
        select(StudentProfile).where(StudentProfile.student_number == student_number)
    )
