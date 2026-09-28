"""Seed the fixed accounts the Bruno end-to-end suite logs in as.

The suite drives the real HTTP API, so it needs accounts it can authenticate as
on the first request — and two things stand in its way. Login rejects an
unverified address, but the verification token only ever reaches the console
email backend, so an HTTP client cannot redeem it. And every IAM endpoint
requires ``ManageIAM``, which no account can grant itself. Both have to be done
from outside the HTTP layer, which is what this script is for.

Run it after ``seed_iam``, against the environment Bruno points at::

    uv run python -m scripts.seed_e2e
    uv run python -m scripts.seed_e2e --password 'SomethingElse123!'
    uv run python -m scripts.seed_e2e --quiet

Three accounts are seeded, all pre-verified and sharing one password:

``e2e-owner@example.com``
    An ordinary member. Owns the records the suite creates.
``e2e-other@example.com``
    A second ordinary member, so the suite can prove object-level
    authorization: what the owner creates, this account must fail to touch.
``e2e-admin@example.com``
    A test-only IAM operator. It is not a member of the product Admin role,
    whose login requires MFA.

Idempotent: existing accounts are reused, verification is re-applied, and group
membership is reconciled. It does NOT reset the password of an existing
account, so an environment seeded with a different ``--password`` keeps the old
one.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

from sqlalchemy import select

from cbpupsis_core.exceptions import ConflictError
from cbpupsis_database.models.iam import Group, GroupPolicy, Policy
from cbpupsis_database.models.users import User
from cbpupsis_database.session import AsyncSessionLocal
from cbpupsis_shared.domains.auth import service as auth_service
from cbpupsis_shared.domains.auth.constants import PASSWORD_MIN_LENGTH
from cbpupsis_shared.domains.iam import service as iam_service
from cbpupsis_shared.domains.iam.constants import ADMIN_GROUP
from cbpupsis_shared.domains.users import service as users_service

#: The group an ordinary user belongs to. Registration itself grants no groups,
#: so the API alone cannot produce an account that may create items; seed_iam
#: creates this one for exactly that purpose.
MEMBER_GROUP = "Members"

#: Exists only in E2E environments. Keeping the fixture out of ``Admins`` lets
#: the legacy IAM contract suite use ordinary login without weakening the
#: product Admin role's mandatory MFA boundary.
E2E_IAM_OPERATOR_GROUP = "E2EIAMOperators"
E2E_IAM_OPERATOR_POLICIES = ("IAMAdmin",)

#: Only used when neither --password nor E2E_PASSWORD is set. Long enough to
#: clear the register schema's minimum, which is asserted below rather than
#: assumed — the constant is free to move.
DEFAULT_PASSWORD = "E2ETestPassword123!"

#: (email, groups) for each seeded account. example.com is reserved by RFC 2606
#: and never deliverable, so a stray email send cannot reach a real inbox.
#:
#: Not .test, which would be the more natural reserved choice: Pydantic's
#: EmailStr rejects it as a special-use name, so the API answers 422 and an
#: account seeded there could never log in.
ACCOUNTS: list[tuple[str, list[str]]] = [
    ("e2e-owner@example.com", [MEMBER_GROUP]),
    ("e2e-other@example.com", [MEMBER_GROUP]),
    ("e2e-admin@example.com", [MEMBER_GROUP, E2E_IAM_OPERATOR_GROUP]),
]


async def _ensure_e2e_operator_group(session) -> int | None:
    """Create the test-only IAM operator group and attach its seeded policy."""
    group = await session.scalar(
        select(Group).where(Group.name == E2E_IAM_OPERATOR_GROUP)
    )
    if group is None:
        group = Group(
            name=E2E_IAM_OPERATOR_GROUP,
            description="Test-only IAM operator used by the Bruno suite",
        )
        session.add(group)
        await session.flush()

    for policy_name in E2E_IAM_OPERATOR_POLICIES:
        policy_id = await session.scalar(
            select(Policy.id).where(Policy.name == policy_name)
        )
        if policy_id is None:
            print(
                f"No policy named {policy_name!r}; run seed_iam first.",
                file=sys.stderr,
            )
            await session.rollback()
            return None
        attached = await session.scalar(
            select(GroupPolicy).where(
                GroupPolicy.group_id == group.id,
                GroupPolicy.policy_id == policy_id,
            )
        )
        if attached is None:
            session.add(GroupPolicy(group_id=group.id, policy_id=policy_id))

    await session.commit()
    return group.id


async def _resolve_group_ids(session, names: list[str]) -> dict[str, int] | None:
    """Map each group name to its id, or report the missing ones and return None.

    Resolved for every account up front, so a database missing the IAM seed
    fails before the first account is written rather than half way through.
    """
    ids: dict[str, int] = {}
    for name in names:
        # Select the id, not the ORM object. An instance loaded here would be
        # expired by the commit or rollback that follows, and reading .id from
        # an expired instance triggers a lazy load that fails under async.
        group_id = await session.scalar(select(Group.id).where(Group.name == name))
        if group_id is None:
            available = (await session.scalars(select(Group.name))).all()
            print(
                f"No group named {name!r}.\n"
                f"Available: {sorted(available) or '(none - run seed_iam first)'}",
                file=sys.stderr,
            )
            return None
        ids[name] = group_id
    return ids


async def seed_e2e(password: str, quiet: bool) -> int:
    """Create (or reuse) the three end-to-end accounts. Returns an exit code."""
    if len(password) < PASSWORD_MIN_LENGTH:
        print(
            f"Password is shorter than the {PASSWORD_MIN_LENGTH} characters the "
            "register schema requires; the seeded accounts could not log in.",
            file=sys.stderr,
        )
        return 1

    async with AsyncSessionLocal() as session:
        if await _ensure_e2e_operator_group(session) is None:
            return 1
        wanted = sorted({name for _, names in ACCOUNTS for name in names})
        group_ids = await _resolve_group_ids(session, wanted)
        if group_ids is None:
            return 1
        old_admin_group_id = await session.scalar(
            select(Group.id).where(Group.name == ADMIN_GROUP)
        )

        for email, group_names in ACCOUNTS:
            try:
                user = await auth_service.register(
                    session, email=email, password=password
                )
                created = True
            except ConflictError:
                # Already seeded: reuse the account, leaving its password as is.
                # Rolled back by register(), so the session is usable again.
                user = await session.scalar(
                    select(User).where(User.email == email.lower())
                )
                if user is None:  # pragma: no cover - only if deleted concurrently
                    print(f"Could not resolve existing user {email!r}", file=sys.stderr)
                    return 1
                created = False

            # Capture the identifiers NOW, before any further commit. Each commit
            # expires every loaded instance, and reading an expired attribute
            # triggers a lazy refresh — which raises MissingGreenlet under async
            # SQLAlchemy. Plain values survive where the instance does not.
            user_id, user_email = user.id, user.email

            # The reason this script exists: login refuses an unverified address,
            # and the token to verify it never leaves the console email backend.
            await users_service.mark_email_verified(session, user_id)

            for name in group_names:
                await iam_service.add_user_to_group(session, user_id, group_ids[name])

            # Repair databases seeded before Admin MFA was separated from the
            # Bruno IAM fixture. Ordinary login intentionally rejects members
            # of the product Admin role.
            if user_email == "e2e-admin@example.com" and old_admin_group_id is not None:
                await iam_service.remove_user_from_group(
                    session, user_id, old_admin_group_id
                )

            if not quiet:
                granted = await iam_service.get_effective_permissions(session, user_id)
                print(f"{'Created' if created else 'Found'} {user_email} ({user_id})")
                print(f"  Groups: {', '.join(group_names)}")
                print(f"  Permissions: {sorted(granted) or '(none)'}")

    if not quiet:
        print(f"\nE2E seed complete. Password for all accounts: {password}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--password",
        default=os.environ.get("E2E_PASSWORD", DEFAULT_PASSWORD),
        help="Password for all seeded accounts. Defaults to $E2E_PASSWORD.",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress per-account output. Failures are still reported.",
    )
    args = parser.parse_args()

    raise SystemExit(asyncio.run(seed_e2e(args.password, args.quiet)))


if __name__ == "__main__":
    main()
