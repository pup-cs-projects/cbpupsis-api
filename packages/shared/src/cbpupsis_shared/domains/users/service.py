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
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_core.events import Event, event_bus
from cbpupsis_core.pagination import Page
from cbpupsis_database.models.users import User
from cbpupsis_shared.domains.auth import client as auth_client
from cbpupsis_shared.domains.iam import service as iam_service
from cbpupsis_shared.domains.users import repository
from cbpupsis_shared.domains.users.constants import (
    EDITABLE_PROFILE_FIELDS,
    MANAGE_USER,
    READ_ALL_USER,
    REQUIRED_ONBOARDING_FIELDS,
)
from cbpupsis_shared.domains.users.exceptions import (
    AccountDeletedError,
    CannotAdministerSelfError,
    IncorrectPasswordError,
    ProfileAccessDeniedError,
    ProfileIncompleteError,
    UserNotFoundError,
)


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
    repository.update_user(db, user, **fields)
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
    user = await deactivate(db, user_id)
    await auth_client.revoke_all_sessions(db, user_id)
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
    if not auth_client.check_password(password, user.password_hash):
        raise IncorrectPasswordError

    await soft_delete(db, user_id)
    await auth_client.revoke_all_sessions(db, user_id)


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


async def soft_delete(db: AsyncSession, user_id: uuid.UUID) -> User:
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
        granted = await iam_service.get_effective_permissions(db, viewer_id)
        if READ_ALL_USER not in granted:
            raise ProfileAccessDeniedError(READ_ALL_USER)
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
    granted = await iam_service.get_effective_permissions(db, actor_id)
    if MANAGE_USER not in granted:
        raise ProfileAccessDeniedError(MANAGE_USER)
    if actor_id == user_id:
        raise CannotAdministerSelfError

    user = await deactivate(db, user_id)
    await auth_client.revoke_all_sessions(db, user_id)
    await event_bus.publish(
        Event(
            name="user.deactivated",
            payload={"user_id": str(user_id), "actor_id": str(actor_id)},
        )
    )
    return user


async def reactivate_as_admin(
    db: AsyncSession, *, actor_id: uuid.UUID, user_id: uuid.UUID
) -> User:
    """Restore an account someone deactivated, as a holder of ``ManageUser``.

    Refuses an anonymised row for the same reason :func:`reactivate` does: a
    deleted account has lost the identity that would make it usable again.
    """
    granted = await iam_service.get_effective_permissions(db, actor_id)
    if MANAGE_USER not in granted:
        raise ProfileAccessDeniedError(MANAGE_USER)

    user = await reactivate(db, user_id)
    await event_bus.publish(
        Event(
            name="user.reactivated",
            payload={"user_id": str(user_id), "actor_id": str(actor_id)},
        )
    )
    return user
