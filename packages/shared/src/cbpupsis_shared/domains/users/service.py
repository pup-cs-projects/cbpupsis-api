"""Business logic for the users domain — its framework-agnostic public interface.

Other domains call these functions (via ``client.py``) rather than touching the
users table directly. Every read here excludes soft-deleted rows, so callers
never have to remember the ``deleted_at IS NULL`` filter.

No SQL lives here: the queries are in ``repository.py``, and this module holds
the rules layered on top of them — what counts as missing, what may be edited,
and when a change is committed.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_core.events import Event
from cbpupsis_core.exceptions import ForbiddenError
from cbpupsis_core.pagination import Page
from cbpupsis_database.models.users import AdminPosition, User
from cbpupsis_shared.domains.audit import client as audit_client
from cbpupsis_shared.domains.auth import client as auth_client
from cbpupsis_shared.domains.auth.exceptions import SuperadminRoleRequiredError
from cbpupsis_shared.domains.curriculum import client as curriculum_client
from cbpupsis_shared.domains.iam import service as iam_service
from cbpupsis_shared.domains.iam.constants import (
    ADMIN_GROUP,
    FACULTY_GROUP,
    STUDENT_GROUP,
)
from cbpupsis_shared.domains.users import repository
from cbpupsis_shared.domains.users.constants import (
    ADMIN_ROLE_POSITIONS,
    EDITABLE_PROFILE_FIELDS,
    INSTITUTION_WIDE_POSITIONS,
    MANAGE_USER,
    READ_ALL_USER,
    REQUIRED_ONBOARDING_FIELDS,
    AccountRole,
)
from cbpupsis_shared.domains.users.exceptions import (
    AccountDeletedError,
    CannotAdministerSelfError,
    IncorrectPasswordError,
    ProfileAccessDeniedError,
    ProfileIncompleteError,
    UnknownProgramError,
    UserAlreadyExistsError,
    UserNotFoundError,
)
from cbpupsis_shared.domains.users.schemas import (
    AccountCreate,
    AdminProfileRead,
    EmergencyContactRead,
    FacultyProfileRead,
    StudentProfileRead,
    UserProfileRead,
)
from cbpupsis_shared.outbox import publish_transactional


async def get_by_id(db: AsyncSession, user_id: uuid.UUID) -> User:
    """Return a live user by primary key, or raise :class:`NotFoundError`."""
    user = await repository.get_user(db, user_id)
    if user is None:
        raise UserNotFoundError(user_id)
    return user


async def get_by_email(db: AsyncSession, email: str) -> User | None:
    """Return a live user by email, or ``None``.

    Returns ``None`` rather than raising because the caller (login) must not
    distinguish "no such account" from "wrong password".
    """
    return await repository.get_user_by_email(db, email)


async def get_active_user(db: AsyncSession, user_id: uuid.UUID) -> User | None:
    """Return a user only if they exist, are active, and are not soft-deleted.

    Used on every authenticated request, so deactivation takes effect at once.
    """
    return await repository.get_active_user(db, user_id)


def stage_new_user(
    db: AsyncSession,
    email: str,
    password_hash: str,
    full_name: str | None = None,
) -> User:
    """Stage a new user on the session and return it, **without committing**.

    The one write in this module that leaves its transaction open, because the
    caller (auth's registration flow) needs the INSERT to surface inside its own
    ``try``: uniqueness on the email is enforced by the database rather than by
    a prior SELECT, and the resulting ``IntegrityError`` is what it translates
    into a 409. Committing here would move that error out of the caller's reach
    and reintroduce the check-then-insert race the constraint exists to close.

    Takes a digest, not a raw password, for the same reason as
    :func:`set_password_hash`: hashing belongs to auth.
    """
    return repository.add_user(
        db, email=email, password_hash=password_hash, full_name=full_name
    )


async def update_profile(db: AsyncSession, user_id: uuid.UUID, **fields: Any) -> User:
    """Update a user's editable profile fields.

    Only keys present in ``fields`` are written, and only if they are editable —
    so a PATCH that omits a field leaves it alone, which is what distinguishes a
    partial update from a replace. Callers pass ``model_dump(exclude_unset=True)``
    so "absent" and "explicitly set to null" stay distinguishable: sending
    ``{"bio": null}`` clears the bio, while omitting ``bio`` preserves it.
    """
    unknown = set(fields) - EDITABLE_PROFILE_FIELDS
    if unknown:
        raise ValueError(f"Not editable profile fields: {sorted(unknown)}")

    user = await get_by_id(db, user_id)
    prior = {field: getattr(user, field) for field in fields}
    repository.update_user(db, user, **fields)
    if db.info.get("superadmin_audit") is not None:
        audit_client.stage(
            db,
            action="user.profile_updated",
            actor_id=user_id,
            target_type="user",
            target_id=user_id,
            prior_state=prior,
            new_state=fields,
        )
    await db.commit()
    await db.refresh(user)
    return user


async def complete_onboarding(db: AsyncSession, user_id: uuid.UUID) -> User:
    """Stamp ``onboarding_completed_at`` once the required fields are present.

    Raises :class:`ValidationError` listing what is missing, so the client can
    highlight the offending inputs instead of guessing.

    Idempotent: completing twice keeps the original timestamp, because the
    meaningful fact is when the user first finished, not when they last
    re-submitted the form.
    """
    user = await get_by_id(db, user_id)

    missing = [
        name for name in REQUIRED_ONBOARDING_FIELDS if getattr(user, name) is None
    ]
    if missing:
        raise ProfileIncompleteError(missing)

    if user.onboarding_completed_at is None:
        repository.update_user(db, user, onboarding_completed_at=datetime.now(UTC))
        if db.info.get("superadmin_audit"):
            audit_client.stage(
                db,
                action="user.onboarding_completed",
                actor_id=user_id,
                target_type="user",
                target_id=user_id,
                prior_state={"completed": False},
                new_state={"completed": True},
            )
        await db.commit()
        await db.refresh(user)
    return user


async def mark_email_verified(db: AsyncSession, user_id: uuid.UUID) -> User:
    """Record that the user's email address was proven reachable.

    Idempotent for the same reason as onboarding: re-verifying should not
    rewrite the date the address was first confirmed.
    """
    user = await get_by_id(db, user_id)
    if user.email_verified_at is None:
        repository.update_user(db, user, email_verified_at=datetime.now(UTC))
        await db.commit()
        await db.refresh(user)
    return user


async def set_password_hash(
    db: AsyncSession, user_id: uuid.UUID, password_hash: str
) -> User:
    """Store an already-hashed password.

    Takes the digest, not the raw password: hashing belongs to
    ``cbpupsis_shared.domains.auth.security``, and this domain should never hold a
    plaintext credential even briefly.
    """
    user = await get_by_id(db, user_id)
    repository.update_user(db, user, password_hash=password_hash)
    await db.commit()
    await db.refresh(user)
    return user


async def deactivate(db: AsyncSession, user_id: uuid.UUID) -> User:
    """Suspend an account without deleting it.

    Reversible by design — nothing is scrubbed, so reactivation restores the
    account intact. Owns the users table only: it does not revoke sessions, so
    that :func:`deactivate_own_account` can compose the two without this
    function reaching across a domain boundary on every internal call.
    """
    user = await get_by_id(db, user_id)
    repository.update_user(db, user, is_active=False)
    await db.commit()
    await db.refresh(user)
    return user


async def deactivate_own_account(db: AsyncSession, user_id: uuid.UUID) -> User:
    """Deactivate the caller's own account and end every session.

    Revoking the refresh tokens is what makes deactivation take effect: the
    ``is_active`` flag already stops new access tokens being accepted, but a
    live refresh token would otherwise let a client keep minting them right up
    until it expired — except that ``refresh`` re-checks the account, so this is
    belt and braces. It also means reactivating does not silently restore
    sessions that were open weeks earlier.
    """
    user = await get_by_id(db, user_id)
    prior = {"is_active": user.is_active}
    repository.update_user(db, user, is_active=False)
    await auth_client.stage_revoke_all_sessions(db, user_id)
    if db.info.get("superadmin_audit") is not None:
        audit_client.stage(
            db,
            action="user.self_deactivated",
            actor_id=user_id,
            target_type="user",
            target_id=user_id,
            prior_state=prior,
            new_state={"is_active": False},
        )
    await db.commit()
    return user


async def delete_own_account(
    db: AsyncSession, user_id: uuid.UUID, password: str
) -> None:
    """Soft-delete the caller's own account after re-checking their password.

    The password check is the point: an access token is a bearer credential that
    can be stolen from a log, a proxy, or a shared machine, and account deletion
    is the least reversible thing the API offers. Requiring the password means a
    stolen token alone cannot destroy an account.

    Raises :class:`UnauthorizedError` on a wrong password. Sessions are revoked
    after the scrub, so every token the account held is dead.
    """
    user = await get_by_id(db, user_id)
    if not await auth_client.check_password(password, user.password_hash):
        raise IncorrectPasswordError

    await soft_delete(db, user_id, commit=False)
    await auth_client.stage_revoke_all_sessions(db, user_id)
    if db.info.get("superadmin_audit") is not None:
        audit_client.stage(
            db,
            action="user.self_deleted",
            actor_id=user_id,
            target_type="user",
            target_id=user_id,
            prior_state={"deleted": False},
            new_state={"deleted": True},
        )
    await db.commit()


async def reactivate(db: AsyncSession, user_id: uuid.UUID) -> User:
    """Restore a deactivated account.

    Refuses an anonymised row: a deleted account has lost the credentials and
    identity that would make it usable again, so silently "restoring" one would
    hand back an account nobody can log into.
    """
    user = await get_by_id(db, user_id)
    if user.anonymized_at is not None:
        raise AccountDeletedError
    repository.update_user(db, user, is_active=True)
    await db.commit()
    await db.refresh(user)
    return user


async def soft_delete(
    db: AsyncSession, user_id: uuid.UUID, *, commit: bool = True
) -> User:
    """Soft-delete an account and scrub its personal data.

    **What survives and why.** The row itself stays: items, audit entries, and
    IAM grants reference this id, and deleting it would orphan them (or, with a
    hard FK, fail). What survives is exactly what preserves those references —
    the primary key, the timestamps, and the fact of deletion.

    **What is scrubbed.** Everything that identifies a person: email, name,
    display name, bio, avatar, phone, locale, timezone. The email is replaced
    with a unique ``deleted+<uuid>@invalid`` placeholder rather than NULL, for
    two reasons — the column is NOT NULL and uniquely indexed, so several
    deletions would otherwise collide, and ``.invalid`` is reserved by RFC 2606
    so the address can never route anywhere real.

    **The password hash is overwritten** with an unusable marker, not left in
    place: a stale argon2 digest of a password the user reuses elsewhere is a
    liability with no remaining purpose.

    Deactivating as well as deleting is deliberate, so every existing
    ``is_active`` check (login, ``get_active_user``) rejects the account without
    needing to learn about anonymisation.
    """
    user = await get_by_id(db, user_id)
    now = datetime.now(UTC)

    repository.update_user(
        db,
        user,
        email=f"deleted+{user.id}@invalid",
        full_name=None,
        display_name=None,
        bio=None,
        avatar_url=None,
        phone_number=None,
        timezone=None,
        locale=None,
        # Not a valid argon2 digest, so verify_password can never match on it.
        password_hash="!deleted",
        is_active=False,
        anonymized_at=now,
        deleted_at=now,
    )

    if commit:
        await db.commit()
        await db.refresh(user)
    return user


async def get_profile_for(
    db: AsyncSession, *, viewer_id: uuid.UUID, user_id: uuid.UUID
) -> User:
    """Return ``user_id``'s profile as seen by ``viewer_id``.

    Two levels of authorization, and only the second can live here: the endpoint
    checks that the caller may read users at all, and this checks that they may
    read *this* one. Reading your own profile is always allowed; reading anyone
    else's needs ``ReadAllUser``.

    The check belongs in the service rather than the router because the router
    is meant to stay thin, and because a rule enforced at the edge is one a
    background job or another domain can walk straight past.
    """
    if user_id != viewer_id:
        # Someone else's record answers 404, never 403: a 403 would confirm to
        # a caller who may not read it that the id exists.
        try:
            await auth_client.require_superadmin_actor(db, viewer_id)
        except SuperadminRoleRequiredError:
            raise UserNotFoundError(user_id) from None
        granted = await iam_service.get_effective_permissions(db, viewer_id)
        if READ_ALL_USER not in granted:
            raise UserNotFoundError(user_id)
    return await get_by_id(db, user_id)


async def list_users_for(
    db: AsyncSession,
    *,
    viewer_id: uuid.UUID,
    limit: int = 50,
    offset: int = 0,
    is_active: bool | None = None,
    is_verified: bool | None = None,
) -> Page[User]:
    """Return a page of users, for a viewer holding ``ReadAllUser``.

    Unlike :func:`get_profile_for` there is no self-service branch to fall back
    on: a listing is inherently a cross-ownership read, so the permission is the
    whole rule. It is still checked here rather than only in the router, for the
    same reason as everywhere else — a rule enforced at the edge is one a
    background job or another domain walks straight past.
    """
    await auth_client.require_superadmin_actor(db, viewer_id)
    granted = await iam_service.get_effective_permissions(db, viewer_id)
    if READ_ALL_USER not in granted:
        raise ProfileAccessDeniedError(READ_ALL_USER)

    rows, total = await repository.list_users(
        db, limit=limit, offset=offset, is_active=is_active, is_verified=is_verified
    )
    return Page(items=rows, total=total, limit=limit, offset=offset)


async def deactivate_as_admin(
    db: AsyncSession, *, actor_id: uuid.UUID, user_id: uuid.UUID
) -> User:
    """Deactivate someone else's account, as a holder of ``ManageUser``.

    Distinct from :func:`deactivate_own_account` rather than a parameter on it:
    the two differ in who may call them and in what they mean. Self-service
    deactivation is a user choosing to leave; this is an operator suspending an
    account, and only this one needs an actor to attribute it to.

    Sessions are revoked, so suspension takes effect immediately rather than
    when the current access token happens to expire.

    Refuses to act on the caller's own account: an administrator locking
    themselves out is an accident with no undo through the API, since
    reactivating needs the permission they just lost access to.
    """
    await auth_client.require_superadmin_actor(db, actor_id)
    user = await repository.get_user_for_update(db, user_id)
    if user is None:
        raise UserNotFoundError(user_id)
    if await iam_service.is_user_in_group(db, user_id, ADMIN_GROUP):
        raise ForbiddenError(
            "Admin suspension requires a justified override", code="OVERRIDE_REQUIRED"
        )
    granted = await iam_service.get_effective_permissions(db, actor_id)
    if MANAGE_USER not in granted:
        raise ProfileAccessDeniedError(MANAGE_USER)
    if actor_id == user_id:
        raise CannotAdministerSelfError

    prior_state = {"is_active": user.is_active}
    repository.update_user(db, user, is_active=False)
    await auth_client.stage_revoke_all_sessions(db, user_id)
    publish_transactional(
        db,
        Event(
            name="user.deactivated",
            payload={
                "user_id": str(user_id),
                "actor_id": str(actor_id),
                "prior_state": prior_state,
                "new_state": {"is_active": False},
            },
        ),
    )
    await db.commit()
    await db.refresh(user)
    return user


async def reactivate_as_admin(
    db: AsyncSession, *, actor_id: uuid.UUID, user_id: uuid.UUID
) -> User:
    """Restore an account someone deactivated, as a holder of ``ManageUser``.

    Refuses an anonymised row for the same reason :func:`reactivate` does: a
    deleted account has lost the identity that would make it usable again.
    """
    await auth_client.require_superadmin_actor(db, actor_id)
    granted = await iam_service.get_effective_permissions(db, actor_id)
    if MANAGE_USER not in granted:
        raise ProfileAccessDeniedError(MANAGE_USER)

    user = await get_by_id(db, user_id)
    if user.anonymized_at is not None:
        raise AccountDeletedError
    prior_state = {"is_active": user.is_active}
    repository.update_user(db, user, is_active=True)
    publish_transactional(
        db,
        Event(
            name="user.reactivated",
            payload={
                "user_id": str(user_id),
                "actor_id": str(actor_id),
                "prior_state": prior_state,
                "new_state": {"is_active": True},
            },
        ),
    )
    await db.commit()
    await db.refresh(user)
    return user


async def get_by_email_for_update(db: AsyncSession, email: str):
    """Read a credential while holding the account lock until commit."""
    return await repository.get_user_by_email_for_update(db, email)


async def get_active_user_for_update(db: AsyncSession, user_id: uuid.UUID):
    """Read a usable account under the shared password/session lock."""
    user = await repository.get_user_for_update(db, user_id)
    return user if user is not None and user.is_active else None


async def get_role_subject_for_update(db: AsyncSession, user_id: uuid.UUID):
    """Serialize privileged membership changes on the account row."""
    user = await repository.get_user_for_update(db, user_id)
    if user is None or user.deleted_at is not None:
        raise UserNotFoundError(user_id)
    return user


async def stage_password_reset(
    db: AsyncSession, user_id: uuid.UUID, password_hash: str
):
    """Stage a digest the user chose, ending any temporary-password state.

    Shared by the reset and change flows: either way the user now holds a
    password of their own, so the forced change no longer applies.
    """
    user = await repository.get_user_for_update(db, user_id)
    if user is None:
        raise UserNotFoundError(user_id)
    repository.update_user(
        db,
        user,
        password_hash=password_hash,
        password_change_required=False,
        temporary_password_expires_at=None,
    )
    return user


async def stage_temporary_password(
    db: AsyncSession, user_id: uuid.UUID, password_hash: str, expires_at: datetime
):
    """Stage a system-issued digest that the user must replace before ``expires_at``.

    404 for a missing, deleted, or deactivated account: a temporary password
    for an account that cannot sign in would only put a credential in an inbox.
    """
    user = await repository.get_user_for_update(db, user_id)
    if user is None or not user.is_active:
        raise UserNotFoundError(user_id)
    repository.update_user(
        db,
        user,
        password_hash=password_hash,
        password_change_required=True,
        temporary_password_expires_at=expires_at,
    )
    return user


@dataclass(frozen=True)
class SignInSubject:
    """What a role-specific sign-in needs to verify an institutional identifier.

    Internal only, never a response: it carries the password hash, which is why
    it is a plain dataclass here rather than a schema in ``schemas.py``.
    """

    user_id: uuid.UUID
    email: str
    password_hash: str
    is_active: bool
    email_verified: bool
    session_version: int
    birthdate: date | None = None
    password_change_required: bool = False
    temporary_password_expires_at: datetime | None = None

    @classmethod
    def from_user(cls, user: User, birthdate: date | None = None) -> SignInSubject:
        """Build the subject from a user row; an anonymized account is inactive."""
        return cls(
            user_id=user.id,
            email=user.email,
            password_hash=user.password_hash,
            is_active=user.is_active and user.anonymized_at is None,
            email_verified=user.email_verified_at is not None,
            session_version=user.session_version,
            birthdate=birthdate,
            password_change_required=user.password_change_required,
            temporary_password_expires_at=user.temporary_password_expires_at,
        )


async def get_email_sign_in(db: AsyncSession, email: str) -> SignInSubject | None:
    """Return the sign-in subject for an email, holding the account row lock."""
    user = await repository.get_user_by_email_for_update(db, email.lower())
    return None if user is None else SignInSubject.from_user(user)


async def get_session_version(db: AsyncSession, user_id: uuid.UUID) -> int | None:
    """Return a usable account's session generation, or ``None``."""
    user = await repository.get_active_user(db, user_id)
    return None if user is None else user.session_version


async def get_student_sign_in(
    db: AsyncSession, student_number: str
) -> SignInSubject | None:
    """Return the sign-in subject holding a student number, or ``None``."""
    row = await repository.get_student_by_number(db, student_number)
    if row is None:
        return None
    user, profile = row
    return SignInSubject.from_user(
        user, profile.birthdate if profile is not None else None
    )


async def get_faculty_sign_in(
    db: AsyncSession, employee_id: str
) -> SignInSubject | None:
    """Return the sign-in subject holding an employee id, or ``None``."""
    row = await repository.get_faculty_by_employee_id(db, employee_id)
    return None if row is None else SignInSubject.from_user(row[0])


async def get_faculty_profile(
    db: AsyncSession, user_id: uuid.UUID
) -> FacultyProfileRead | None:
    """Return a user's faculty profile, or ``None``."""
    profile = await repository.get_faculty_profile(db, user_id)
    return None if profile is None else FacultyProfileRead.model_validate(profile)


async def get_user_profile(
    db: AsyncSession, user_id: uuid.UUID
) -> UserProfileRead | None:
    """Return a user's personal-details profile, or ``None``."""
    profile = await repository.get_user_profile(db, user_id)
    return None if profile is None else UserProfileRead.model_validate(profile)


async def get_student_profile(
    db: AsyncSession, user_id: uuid.UUID
) -> StudentProfileRead | None:
    """Return a user's student profile, or ``None``."""
    profile = await repository.get_student_profile(db, user_id)
    return None if profile is None else StudentProfileRead.model_validate(profile)


async def stage_update_user_profile(
    db: AsyncSession, user_id: uuid.UUID, **fields: object
) -> UserProfileRead:
    """Stage changes onto ``user_id``'s personal-details profile without committing."""
    profile = await repository.get_user_profile(db, user_id)
    if profile is None:
        user = await get_by_id(db, user_id)
        parts = (user.full_name or "Student Account").split(" ", 1)
        profile = repository.add_user_profile(
            db,
            user_id=user_id,
            first_name=parts[0],
            last_name=parts[1] if len(parts) > 1 else "",
            birthdate=None,
        )
    repository.update_user_profile(profile, updated_at=datetime.now(UTC), **fields)
    await db.flush()
    return UserProfileRead.model_validate(profile)


async def list_emergency_contacts(
    db: AsyncSession, user_id: uuid.UUID
) -> list[EmergencyContactRead]:
    """Return ``user_id``'s emergency contacts, primary first."""
    rows = await repository.list_emergency_contacts(db, user_id)
    return [EmergencyContactRead.model_validate(row) for row in rows]


async def get_emergency_contact(
    db: AsyncSession, contact_id: uuid.UUID
) -> EmergencyContactRead | None:
    """Return one emergency contact as a DTO, or ``None``."""
    row = await repository.get_emergency_contact(db, contact_id)
    return None if row is None else EmergencyContactRead.model_validate(row)


async def stage_create_emergency_contact(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    contact_name: str,
    relationship: str,
    phone_number: str,
    email: str | None = None,
    address: str | None = None,
    is_primary: bool = False,
) -> EmergencyContactRead:
    """Stage a new emergency contact while maintaining exactly one primary."""
    existing = await repository.list_emergency_contacts(db, user_id)
    has_primary = any(c.is_primary for c in existing)
    make_primary = is_primary or not existing or not has_primary
    if make_primary:
        for other in existing:
            if other.is_primary:
                repository.update_emergency_contact(other, is_primary=False)
    contact = repository.add_emergency_contact(
        db,
        user_id=user_id,
        contact_name=contact_name,
        relationship=relationship,
        phone_number=phone_number,
        email=email,
        address=address,
        is_primary=make_primary,
    )
    await db.flush()
    return EmergencyContactRead.model_validate(contact)


async def stage_update_emergency_contact(
    db: AsyncSession, contact_id: uuid.UUID, **fields: object
) -> EmergencyContactRead | None:
    """Stage changes onto an emergency contact while maintaining one primary."""
    contact = await repository.get_emergency_contact(db, contact_id)
    if contact is None:
        return None
    existing = await repository.list_emergency_contacts(db, contact.user_id)
    others = [c for c in existing if c.id != contact.id]
    if "is_primary" in fields:
        if fields["is_primary"]:
            for other in others:
                if other.is_primary:
                    repository.update_emergency_contact(other, is_primary=False)
        elif contact.is_primary:
            if others:
                repository.update_emergency_contact(others[0], is_primary=True)
            else:
                fields["is_primary"] = True
    repository.update_emergency_contact(contact, **fields)
    await db.flush()
    return EmergencyContactRead.model_validate(contact)


async def stage_delete_emergency_contact(
    db: AsyncSession, contact_id: uuid.UUID
) -> EmergencyContactRead | None:
    """Stage deletion of an emergency contact, promoting a remaining one if needed."""
    contact = await repository.get_emergency_contact(db, contact_id)
    if contact is None:
        return None
    snapshot = EmergencyContactRead.model_validate(contact)
    user_id = contact.user_id
    was_primary = contact.is_primary
    await repository.delete_emergency_contact(db, contact)
    await db.flush()
    if was_primary:
        remaining = await repository.list_emergency_contacts(db, user_id)
        if remaining and not any(c.is_primary for c in remaining):
            repository.update_emergency_contact(remaining[0], is_primary=True)
            await db.flush()
    return snapshot


async def has_role_sign_in(db: AsyncSession, user_id: uuid.UUID) -> bool:
    """Return whether the account signs in through a student or faculty route."""
    return await repository.exists_role_profile(db, user_id)


async def get_admin_profile(
    db: AsyncSession, user_id: uuid.UUID
) -> AdminProfileRead | None:
    """Return a user's Admin position profile, or ``None``."""
    profile = await repository.get_admin_profile(db, user_id)
    return None if profile is None else AdminProfileRead.model_validate(profile)


async def save_admin_profile(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    position: AdminPosition,
    department: str | None,
    college: str | None,
) -> AdminProfileRead:
    """Create or replace a user's active Admin position profile, and commit."""
    profile = await repository.get_admin_profile(db, user_id)
    if profile is None:
        profile = repository.add_admin_profile(
            db,
            user_id=user_id,
            position=position.value,
            department=department,
            college=college,
        )
    else:
        repository.update_admin_profile(
            profile,
            position=position.value,
            department=department,
            college=college,
            is_active=True,
        )
    await db.commit()
    await db.refresh(profile)
    return AdminProfileRead.model_validate(profile)


async def stage_invalidate_sessions(db: AsyncSession, user_id: uuid.UUID) -> None:
    """Stage a generation change so all earlier access tokens stop working."""
    user = await repository.get_user_for_update(db, user_id)
    if user is not None:
        repository.update_user(db, user, session_version=user.session_version + 1)


async def stage_admin_deactivation_override(
    db: AsyncSession, *, actor_id: uuid.UUID, user_id: uuid.UUID
):
    await auth_client.require_superadmin_actor(db, actor_id)
    if actor_id == user_id:
        raise CannotAdministerSelfError
    user = await repository.get_user_for_update(db, user_id)
    if user is None:
        raise UserNotFoundError(user_id)
    if not await iam_service.is_user_in_group(db, user_id, ADMIN_GROUP):
        raise ForbiddenError(
            "Override target must be an Admin", code="OVERRIDE_TARGET_INVALID"
        )
    prior = {"is_active": user.is_active}
    repository.update_user(db, user, is_active=False)
    await auth_client.stage_revoke_all_sessions(db, user_id)
    return prior, {"is_active": False}


#: The IAM group each account role joins.
ROLE_GROUPS: dict[AccountRole, str] = {
    AccountRole.student: STUDENT_GROUP,
    AccountRole.faculty: FACULTY_GROUP,
    **dict.fromkeys(ADMIN_ROLE_POSITIONS, ADMIN_GROUP),
}


def role_group(role: AccountRole) -> str:
    """Return the IAM group an account of ``role`` belongs to."""
    return ROLE_GROUPS[role]


def admin_scope(
    position: AdminPosition, department: str | None, college: str | None
) -> tuple[str | None, str | None]:
    """Keep only the scope a position holds: ``(department, college)``.

    A chairperson keeps a department, a dean a college, and an
    institution-wide position (registrar, finance) neither.
    """
    if position is not AdminPosition.chairperson:
        department = None
    if position in INSTITUTION_WIDE_POSITIONS:
        college = None
    return department, college


@dataclass(frozen=True)
class AccountProblem:
    """Why one account in a batch cannot be created. Never carries the value."""

    index: int
    column: str
    rule: str
    #: True when the account collides with one that already exists.
    taken: bool = False


@dataclass(frozen=True)
class NewAccount:
    """A checked account with its issued credential, ready to stage."""

    account: AccountCreate
    password_hash: str
    expires_at: datetime
    program_id: uuid.UUID | None


def _id_kind(role: AccountRole) -> str:
    """Which profile table holds the institutional id; each one is unique alone."""
    if role is AccountRole.student:
        return "student"
    return "faculty" if role is AccountRole.faculty else "admin"


async def find_account_problems(
    db: AsyncSession, accounts: Sequence[AccountCreate]
) -> tuple[list[AccountProblem], dict[str, uuid.UUID]]:
    """Check a batch against itself and the database, before anything is staged.

    Returns every problem, ordered by account, and the id of each program code
    the batch names (upper-cased). An institutional id is unique per profile
    table, as the constraints are: a chairperson may hold the same employee id
    on an Admin account as on a faculty one.
    """
    problems: list[AccountProblem] = []
    emails: set[str] = set()
    ids: dict[str, set[str]] = {"student": set(), "faculty": set(), "admin": set()}
    for index, account in enumerate(accounts):
        if account.email in emails:
            problems.append(AccountProblem(index, "email", "duplicated in this file"))
        emails.add(account.email)
        if account.institutional_id is not None:
            kind_ids = ids[_id_kind(account.role)]
            if account.institutional_id in kind_ids:
                problems.append(
                    AccountProblem(index, "institutional_id", "duplicated in this file")
                )
            kind_ids.add(account.institutional_id)

    taken_emails = set(await repository.list_taken_emails(db, emails))
    taken_ids = {
        "student": set(await repository.list_taken_student_numbers(db, ids["student"])),
        "faculty": set(
            await repository.list_taken_faculty_employee_ids(db, ids["faculty"])
        ),
        "admin": set(await repository.list_taken_admin_employee_ids(db, ids["admin"])),
    }
    programs = await curriculum_client.active_programs_by_code(
        db, (a.program for a in accounts if a.role is AccountRole.student and a.program)
    )
    for index, account in enumerate(accounts):
        if account.email in taken_emails:
            problems.append(
                AccountProblem(index, "email", "already registered", taken=True)
            )
        if account.institutional_id in taken_ids[_id_kind(account.role)]:
            problems.append(
                AccountProblem(
                    index, "institutional_id", "already registered", taken=True
                )
            )
        if (
            account.role is AccountRole.student
            and (account.program or "").upper() not in programs
        ):
            problems.append(
                AccountProblem(index, "program", "unknown or inactive program code")
            )
    problems.sort(key=lambda problem: problem.index)
    return problems, {code: program.id for code, program in programs.items()}


def program_id_for(
    account: AccountCreate, program_ids: dict[str, uuid.UUID]
) -> uuid.UUID | None:
    """The program a checked student account joins; ``None`` for other roles."""
    if account.role is not AccountRole.student or account.program is None:
        return None
    return program_ids[account.program.upper()]


def raise_for_problems(problems: Sequence[AccountProblem]) -> None:
    """Raise the error one problem stands for: a collision first, then a program."""
    for problem in problems:
        if problem.taken or problem.column != "program":
            raise UserAlreadyExistsError(problem.column)
    if problems:
        raise UnknownProgramError


async def check_new_account(
    db: AsyncSession, account: AccountCreate
) -> uuid.UUID | None:
    """Check one account, raising its first problem; return its program id."""
    problems, program_ids = await find_account_problems(db, [account])
    raise_for_problems(problems)
    return program_id_for(account, program_ids)


async def stage_accounts(
    db: AsyncSession, accounts: Sequence[NewAccount]
) -> list[uuid.UUID]:
    """Stage verified users and their profiles, **without committing**.

    Flushes the users before their profiles, so the profile rows never reach
    the database ahead of the row they reference. A unique-constraint race
    surfaces as ``IntegrityError`` from either flush, inside the caller's
    transaction.
    """
    now = datetime.now(UTC)
    user_ids: list[uuid.UUID] = []
    for item in accounts:
        account = item.account
        user_id = uuid.uuid4()
        repository.add_user(
            db,
            email=account.email,
            password_hash=item.password_hash,
            full_name=f"{account.first_name} {account.surname}",
            user_id=user_id,
            email_verified_at=now,
            password_change_required=True,
            temporary_password_expires_at=item.expires_at,
        )
        user_ids.append(user_id)
    await db.flush()
    for user_id, item in zip(user_ids, accounts, strict=True):
        _stage_profiles(db, user_id, item)
    await db.flush()
    return user_ids


def _stage_profiles(db: AsyncSession, user_id: uuid.UUID, item: NewAccount) -> None:
    account = item.account
    repository.add_user_profile(
        db,
        user_id=user_id,
        first_name=account.first_name,
        last_name=account.surname,
        birthdate=account.birthdate,
    )
    if account.role is AccountRole.student:
        repository.add_student_profile(
            db,
            user_id=user_id,
            student_number=account.institutional_id,
            program_id=item.program_id,
            year_level=account.year_level,
        )
    elif account.role is AccountRole.faculty:
        repository.add_faculty_profile(
            db,
            user_id=user_id,
            employee_id=account.institutional_id,
            department=account.department,
            college=account.college,
        )
    else:
        position = ADMIN_ROLE_POSITIONS[account.role]
        department, college = admin_scope(position, account.department, account.college)
        repository.add_admin_profile(
            db,
            user_id=user_id,
            employee_id=account.institutional_id,
            position=position.value,
            department=department,
            college=college,
        )
