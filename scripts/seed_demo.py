"""Seed one shared demo account per portal, for teammates trying the system.

Superadmin-created accounts get a mailed temporary password and must enroll an
authenticator on first sign-in, so an account set up by one person cannot be
used by the next. This script gives every demo account a known password, no
forced change, and (for the roles that require MFA) one pre-enrolled TOTP seed
that everyone adds to their authenticator app.

Run it after ``seed_iam``, never against production::

    uv run python -m scripts.seed_demo
    uv run python -m scripts.seed_demo --password 'SomethingElse123!'

Accounts (portal: how to sign in):

Student
    student number ``2026-00001-DM-0``, birthdate ``2005-01-15``. No MFA.
Faculty
    employee id ``2026-00002-DM-0``, then a TOTP code.
Admin (Registrar)
    ``demo-admin@example.com``, then a TOTP code.
Superadmin
    ``demo-superadmin@example.com``, then a TOTP code.

Idempotent, and a re-run is also the reset: it restores the password, the TOTP
seed, and clears any lockout, so a teammate who changed the password or locked
the account out does not break it for everyone else.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import func, select

from cbpupsis_core.config import settings
from cbpupsis_database.models.auth import MfaType
from cbpupsis_database.models.curriculum import Program
from cbpupsis_database.models.iam import Group
from cbpupsis_database.models.users import User
from cbpupsis_database.session import AsyncSessionLocal
from cbpupsis_shared.domains.auth import repository as auth_repository
from cbpupsis_shared.domains.auth.constants import PASSWORD_MIN_LENGTH
from cbpupsis_shared.domains.auth.security import (
    encrypt_secret,
    hash_password,
    totp_provisioning_uri,
)
from cbpupsis_shared.domains.iam import service as iam_service
from cbpupsis_shared.domains.iam.constants import SUPERADMIN_GROUP
from cbpupsis_shared.domains.users import repository as users_repository
from cbpupsis_shared.domains.users import service as users_service
from cbpupsis_shared.domains.users.constants import AccountRole
from cbpupsis_shared.domains.users.schemas import AccountCreate

DEFAULT_PASSWORD = "DemoPassword123!"

#: A fixed RFC 6238 Base32 seed, so every teammate's authenticator shows the
#: same codes. Demo-only: it is in the repository, so it protects nothing.
DEFAULT_TOTP_SECRET = "UB4LBXS3FTK3K25EM6UQHV7Z6V45H77O"

#: The program the demo student is attached to. Created when missing, because
#: the curriculum domain has no write path yet.
DEMO_PROGRAM = (
    "BSCS",
    "Bachelor of Science in Computer Science",
    "Computer Science",
    Decimal("150.0"),
)


@dataclass(frozen=True)
class DemoAccount:
    label: str
    account: AccountCreate | None
    email: str
    mfa: bool


ACCOUNTS: list[DemoAccount] = [
    DemoAccount(
        label="Student",
        email="demo-student@example.com",
        mfa=False,
        account=AccountCreate(
            first_name="Demo",
            surname="Student",
            email="demo-student@example.com",
            role=AccountRole.student,
            institutional_id="2026-00001-DM-0",
            birthdate=date(2005, 1, 15),
            program=DEMO_PROGRAM[0],
            year_level=1,
        ),
    ),
    DemoAccount(
        label="Faculty",
        email="demo-faculty@example.com",
        mfa=True,
        account=AccountCreate(
            first_name="Demo",
            surname="Faculty",
            email="demo-faculty@example.com",
            role=AccountRole.faculty,
            institutional_id="2026-00002-DM-0",
            department="Computer Science",
            college="College of Computer and Information Sciences",
        ),
    ),
    DemoAccount(
        label="Admin (Registrar)",
        email="demo-admin@example.com",
        mfa=True,
        account=AccountCreate(
            first_name="Demo",
            surname="Admin",
            email="demo-admin@example.com",
            role=AccountRole.admin_registrar,
        ),
    ),
    # Not an AccountRole: Superadmins are never created through the API.
    DemoAccount(
        label="Superadmin",
        email="demo-superadmin@example.com",
        mfa=True,
        account=None,
    ),
]


async def _ensure_program(session) -> None:
    code, name, department, units = DEMO_PROGRAM
    exists = await session.scalar(
        select(Program.id).where(func.upper(Program.code) == code)
    )
    if exists is None:
        session.add(
            Program(
                code=code,
                name=name,
                department=department,
                total_units_required=units,
                is_active=True,
            )
        )
        await session.commit()


async def _create(session, demo: DemoAccount, password_hash: str) -> uuid.UUID:
    """Stage a new account with its profiles and role group; commit."""
    if demo.account is None:
        user_id = uuid.uuid4()
        users_repository.add_user(
            session,
            email=demo.email,
            password_hash=password_hash,
            full_name="Demo Superadmin",
            user_id=user_id,
            email_verified_at=datetime.now(UTC),
        )
        group_id = await session.scalar(
            select(Group.id).where(Group.name == SUPERADMIN_GROUP)
        )
        await session.flush()
        await iam_service.add_user_to_group(session, user_id, group_id)
        return user_id

    program_id = await users_service.check_new_account(session, demo.account)
    [user_id] = await users_service.stage_accounts(
        session,
        [
            users_service.NewAccount(
                account=demo.account,
                password_hash=password_hash,
                expires_at=datetime.now(UTC),
                program_id=program_id,
            )
        ],
    )
    await iam_service.stage_new_members(
        session, [(user_id, users_service.role_group(demo.account.role))]
    )
    await session.commit()
    return user_id


async def _reset_credentials(
    session, user_id: uuid.UUID, password_hash: str, totp_secret: str, mfa: bool
) -> None:
    """Restore the shared password and TOTP seed, and lift any lockout."""
    user = await session.get(User, user_id)
    user.password_hash = password_hash
    user.password_change_required = False
    user.temporary_password_expires_at = None
    user.is_active = True
    if user.email_verified_at is None:
        user.email_verified_at = datetime.now(UTC)
    await auth_repository.delete_authentication_lockout(session, user_id)
    await auth_repository.delete_authentication_failures(session, user_id)

    if mfa:
        credential = await auth_repository.get_mfa_credential(
            session, user_id=user_id, mfa_type=MfaType.totp
        )
        if credential is None:
            auth_repository.add_mfa_credential(
                session,
                user_id=user_id,
                mfa_type=MfaType.totp,
                totp_secret=encrypt_secret(totp_secret),
                is_enrolled=True,
            )
        else:
            credential.totp_secret = encrypt_secret(totp_secret)
            credential.totp_last_counter = None
            credential.is_enrolled = True
    await session.commit()


async def seed_demo(password: str, totp_secret: str) -> int:
    if settings.environment == "production":
        print("Refusing to seed shared demo accounts in production.", file=sys.stderr)
        return 1
    if len(password) < PASSWORD_MIN_LENGTH:
        print(
            f"Password must be at least {PASSWORD_MIN_LENGTH} characters.",
            file=sys.stderr,
        )
        return 1

    password_hash = await asyncio.to_thread(hash_password, password)
    async with AsyncSessionLocal() as session:
        if await session.scalar(select(Group.id).limit(1)) is None:
            print("No IAM groups; run seed_iam first.", file=sys.stderr)
            return 1
        await _ensure_program(session)

        for demo in ACCOUNTS:
            user_id = await session.scalar(
                select(User.id).where(User.email == demo.email)
            )
            created = user_id is None
            if created:
                user_id = await _create(session, demo, password_hash)
            await _reset_credentials(
                session, user_id, password_hash, totp_secret, demo.mfa
            )
            print(f"{'Created' if created else 'Reset'} {demo.label}: {demo.email}")

    print(f"\nPassword for every demo account: {password}")
    print(f"TOTP seed (Faculty, Admin, Superadmin): {totp_secret}")
    print(f"  {totp_provisioning_uri(totp_secret, 'demo@cbpupsis')}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--password",
        default=os.environ.get("DEMO_PASSWORD", DEFAULT_PASSWORD),
        help="Password for every demo account. Defaults to $DEMO_PASSWORD.",
    )
    parser.add_argument(
        "--totp-secret",
        default=os.environ.get("DEMO_TOTP_SECRET", DEFAULT_TOTP_SECRET),
        help="Base32 TOTP seed shared by the MFA accounts.",
    )
    args = parser.parse_args()
    raise SystemExit(asyncio.run(seed_demo(args.password, args.totp_secret)))


if __name__ == "__main__":
    main()
