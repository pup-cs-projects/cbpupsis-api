"""Account creation and the generic email-and-password sign-in.

Accounts come to exist three ways, and only the first is reachable over HTTP:

- :func:`create_account` and :func:`create_accounts`: a Superadmin creates
  verified accounts with a system-generated temporary password, which reaches
  the user only by email. This is the product's account creation (FR19).
- :func:`register`: a Superadmin-attributed account with a password the caller
  chose. Kept for that attributed case; no route calls it today.
- :func:`create_console_account`: operator scripts and the test suite, before
  any Superadmin can sign in. No router may reach it;
  ``tests/core/test_architecture.py`` enforces that.
"""

from __future__ import annotations

import asyncio
import secrets
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_core.config import settings
from cbpupsis_core.events import Event
from cbpupsis_database.models.auth import TokenPurpose
from cbpupsis_shared.domains.audit import client as audit_client
from cbpupsis_shared.domains.auth import repository
from cbpupsis_shared.domains.auth.exceptions import InvalidCredentialsError
from cbpupsis_shared.domains.auth.schemas import TokenPair
from cbpupsis_shared.domains.auth.security import (
    TEMPORARY_PASSWORD_EMAIL,
    encrypt_mailed_token,
    generate_temporary_password,
    hash_password,
)
from cbpupsis_shared.domains.auth.service.passwords import check_password
from cbpupsis_shared.domains.auth.service.role_sign_in import (
    record_attempt,
    refuse_expired_temporary_password,
)
from cbpupsis_shared.domains.auth.service.sessions import (
    require_superadmin_actor,
    require_verified_email,
)
from cbpupsis_shared.domains.auth.service.tokens import (
    issue_role_session,
    stage_revoke_all_for_user,
)
from cbpupsis_shared.domains.auth.service.verification import (
    invalidate_outstanding,
    stage_verification_email,
)
from cbpupsis_shared.domains.iam import service as iam_service
from cbpupsis_shared.domains.iam.constants import ADMIN_GROUP, SUPERADMIN_GROUP
from cbpupsis_shared.domains.users import service as users_service
from cbpupsis_shared.domains.users.exceptions import UserAlreadyExistsError
from cbpupsis_shared.domains.users.schemas import AccountCreate
from cbpupsis_shared.outbox import publish_transactional

TEMPORARY_PASSWORD_EVENT = "auth.temporary_password_email"


async def create_account(
    db: AsyncSession, *, actor_id: uuid.UUID, account: AccountCreate
):
    """Create one verified account with a mailed temporary password.

    409 when the email or institutional id is taken, 422 for an unknown
    program. Returns the new user row.
    """
    await require_superadmin_actor(db, actor_id)
    program_id = await users_service.check_new_account(db, account)
    [user_id] = await _create(
        db,
        actor_id=actor_id,
        accounts=[account],
        program_ids=[program_id],
        action="user.created",
    )
    return await users_service.get_by_id(db, user_id)


async def create_accounts(
    db: AsyncSession, *, actor_id: uuid.UUID, accounts: Sequence[AccountCreate]
) -> list[uuid.UUID]:
    """Create a checked batch in one transaction: all of it, or none of it.

    The caller reports problems row by row first; the check repeats here so a
    collision that appeared since still refuses the whole batch. One audit row
    names every account the batch created.
    """
    await require_superadmin_actor(db, actor_id)
    problems, program_ids = await users_service.find_account_problems(db, accounts)
    users_service.raise_for_problems(problems)
    return await _create(
        db,
        actor_id=actor_id,
        accounts=accounts,
        program_ids=[users_service.program_id_for(a, program_ids) for a in accounts],
        action="user.imported",
    )


async def _create(
    db: AsyncSession,
    *,
    actor_id: uuid.UUID,
    accounts: Sequence[AccountCreate],
    program_ids: Sequence[uuid.UUID | None],
    action: str,
) -> list[uuid.UUID]:
    """Stage users, profiles, groups, one audit row, and the mail; commit once.

    Passwords are hashed in worker threads before anything is staged, so the
    transaction is not held open across the hashing. The audit row is staged
    first: inside a Superadmin request only the first staged row is kept.
    """
    raw_passwords = [generate_temporary_password() for _ in accounts]
    digests = await asyncio.gather(
        *(asyncio.to_thread(hash_password, raw) for raw in raw_passwords)
    )
    expires_at = datetime.now(UTC) + timedelta(
        days=settings.temporary_password_ttl_days
    )
    new_accounts = [
        users_service.NewAccount(
            account=account,
            password_hash=digest,
            expires_at=expires_at,
            program_id=program_id,
        )
        for account, digest, program_id in zip(
            accounts, digests, program_ids, strict=True
        )
    ]
    try:
        user_ids = await users_service.stage_accounts(db, new_accounts)
        # Ids and roles only: names, emails, and institutional ids stay out of
        # the audit trail (RA 10173), and the password never comes near it.
        audit_client.stage(
            db,
            action=action,
            actor_id=actor_id,
            target_type="user" if action == "user.created" else "user_import",
            target_id=user_ids[0] if action == "user.created" else uuid.uuid4(),
            prior_state={},
            new_state={
                "created": len(user_ids),
                "user_ids": [str(user_id) for user_id in user_ids],
                "roles": sorted({account.role.value for account in accounts}),
                "email_verified": True,
                "password_change_required": True,
            },
        )
        await iam_service.stage_new_members(
            db,
            [
                (user_id, users_service.role_group(account.role))
                for user_id, account in zip(user_ids, accounts, strict=True)
            ],
        )
        for user_id, raw in zip(user_ids, raw_passwords, strict=True):
            stage_temporary_password_email(db, user_id, raw, expires_at)
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise UserAlreadyExistsError(None) from exc
    return user_ids


async def reissue_temporary_password(
    db: AsyncSession, *, actor_id: uuid.UUID, user_id: uuid.UUID
) -> None:
    """Replace a user's password with a new temporary one, and end every session.

    The way out of an expired temporary password, and of a lost one. Clears the
    lockout and any outstanding reset links, so the mailed password is the one
    credential that works. 404 for a missing or inactive account.
    """
    await require_superadmin_actor(db, actor_id)
    raw = generate_temporary_password()
    digest = await asyncio.to_thread(hash_password, raw)
    expires_at = datetime.now(UTC) + timedelta(
        days=settings.temporary_password_ttl_days
    )
    await users_service.stage_temporary_password(db, user_id, digest, expires_at)
    audit_client.stage(
        db,
        action="user.temporary_password_reissued",
        actor_id=actor_id,
        target_type="user",
        target_id=user_id,
        prior_state={},
        new_state={"password_change_required": True, "sessions": "revoked"},
    )
    await stage_revoke_all_for_user(db, user_id)
    await repository.delete_authentication_lockout(db, user_id)
    await repository.delete_authentication_failures(db, user_id)
    await repository.delete_mfa_challenge(db, user_id)
    await invalidate_outstanding(db, user_id, TokenPurpose.password_reset)
    stage_temporary_password_email(db, user_id, raw, expires_at)
    await db.commit()


def stage_temporary_password_email(
    db: AsyncSession, user_id: uuid.UUID, raw_password: str, expires_at: datetime
) -> None:
    """Stage the welcome email; the password travels only encrypted.

    Bound to the recipient and a random issue id, like a mailed reset link, so
    the job cannot be replayed for anyone else. The worker mails it only while
    the stored digest still matches, so a reissue silences the older message.
    """
    issue_id = secrets.token_hex(16)
    payload = {
        "user_id": str(user_id),
        "issue_id": issue_id,
        "expires_at": expires_at.isoformat(),
        "encrypted_password": encrypt_mailed_token(
            raw_password,
            label=TEMPORARY_PASSWORD_EMAIL,
            user_id=user_id,
            token_hash=issue_id,
        ),
    }
    publish_transactional(db, Event(name=TEMPORARY_PASSWORD_EVENT, payload=payload))


async def register(
    db: AsyncSession,
    email: str,
    password: str,
    full_name: str | None = None,
    *,
    actor_id: uuid.UUID,
):
    """Create an account with a caller-chosen password, attributed to a Superadmin."""
    await require_superadmin_actor(db, actor_id)
    return await _create_with_password(
        db, email=email, password=password, full_name=full_name, actor_id=actor_id
    )


async def create_console_account(
    db: AsyncSession,
    email: str,
    password: str,
    full_name: str | None = None,
):
    """Create an account from an operator script, with no actor to attribute.

    For the first accounts on a fresh database (before any Superadmin can sign
    in), the seed scripts, and the test suite. The account is unverified and is
    mailed a verification link, as public signup used to be.
    """
    return await _create_with_password(
        db, email=email, password=password, full_name=full_name, actor_id=None
    )


async def _create_with_password(
    db: AsyncSession,
    *,
    email: str,
    password: str,
    full_name: str | None,
    actor_id: uuid.UUID | None,
):
    """Relies on the unique constraint rather than a prior SELECT.

    Checking first would leave a race between the check and the insert. The
    integrity error is translated to a clean 409, which is why the insert is
    staged through the users domain and committed here rather than there.
    """
    user = users_service.stage_new_user(
        db,
        email=email.lower(),
        password_hash=hash_password(password),
        full_name=full_name,
    )
    try:
        await db.flush()
        # Staged with the account, so a committed account always has its link
        # queued; delivery happens in the worker and cannot undo the account.
        await stage_verification_email(db, user.id)
        if actor_id is not None:
            audit_client.stage(
                db,
                action="user.created",
                actor_id=actor_id,
                target_type="user",
                target_id=user.id,
                prior_state={},
                new_state={"is_active": user.is_active},
            )
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise UserAlreadyExistsError("email") from exc
    await db.refresh(user)
    return user


async def login(
    db: AsyncSession,
    email: str,
    password: str,
    *,
    ip_address: str | None,
    device_info: str = "",
) -> TokenPair:
    """Verify credentials and issue a token pair.

    The checks run in a deliberate order, and the order is the security
    property:

    1. Unknown email **or** wrong password -> 401, one identical message.
    2. Correct password, but the account is inactive or deleted -> the *same*
       401. An attacker who guesses a password must not learn from the response
       that the address exists but is disabled.
    3. Correct password, temporary password expired -> 401
       ``AUTH_TEMPORARY_PASSWORD_EXPIRED``.
    4. Correct password, email not verified -> 403 ``email_not_verified``.
    5. Correct password, verified -> a token pair.

    **Why the 403 does not enable enumeration.** It is unreachable until the
    password has already been verified, so it only ever tells a caller who
    *already holds valid credentials* that their own address is unverified. An
    attacker probing an email list without the password sees nothing but the
    identical 401 at step 1, exactly as before. What the 403 reveals ("this
    account is registered, and you know its password") is information the
    caller demonstrably already has.

    The distinct status is what lets the frontend send the user to a "resend
    verification" screen rather than back to the login form, which is why this
    is a 403 with a stable code and not a fourth flavour of the 401.
    """
    user = await users_service.get_by_email_for_update(db, email.lower())
    valid = await check_password(password, user.password_hash if user else None)
    if user is None or not valid:
        raise InvalidCredentialsError
    if not user.is_active or user.deleted_at is not None:
        raise InvalidCredentialsError
    if (
        await iam_service.is_user_in_group(db, user.id, ADMIN_GROUP)
        or await iam_service.is_user_in_group(db, user.id, SUPERADMIN_GROUP)
        or await users_service.has_role_sign_in(db, user.id)
    ):
        # Every role with its own sign-in (/auth/student, /auth/faculty,
        # /auth/admin, /auth/superadmin) must enter there, so an email and
        # password alone can never skip that role's second factor or birthdate.
        # Checked before the verification gate so the answer is the same 401.
        raise InvalidCredentialsError
    await refuse_expired_temporary_password(
        db,
        user_id=user.id,
        password_change_required=user.password_change_required,
        expires_at=user.temporary_password_expires_at,
        attempted_id=user.email,
        ip_address=ip_address,
        device_info=device_info,
    )
    require_verified_email(user)
    record_attempt(
        db,
        attempted_id=user.email,
        user_id=user.id,
        ip_address=ip_address,
        device_info=device_info,
    )
    return await issue_role_session(
        db, user.id, role=None, ip_address=ip_address, device_info=device_info
    )
