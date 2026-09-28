"""Create a user and optionally add them to groups.

For seeding a development database and for one-off administrative accounts.
Everyday user creation goes through ``POST /api/v1/auth/register``; this exists
for the cases the API cannot serve — the first accounts on a fresh database,
before anyone holds ``ManageIAM``.

    uv run python -m scripts.create_user alice@example.com
    uv run python -m scripts.create_user alice@example.com --group Members
    uv run python -m scripts.create_user alice@example.com
        --group Members --group Moderators

The password is read from ``--password`` or generated. A generated one is
printed once and never stored in plaintext, so capture it from the output.

Idempotent: an existing account is reused, and re-adding an existing group
membership is a no-op. Re-running only ever adds groups, never removes them.
"""

from __future__ import annotations

import argparse
import asyncio
import secrets
import sys

from sqlalchemy import select

from cbpupsis_core.exceptions import ConflictError
from cbpupsis_database.models.iam import Group
from cbpupsis_database.models.users import User
from cbpupsis_database.session import AsyncSessionLocal
from cbpupsis_shared.domains.auth import service as auth_service
from cbpupsis_shared.domains.iam import service as iam_service

#: Long enough that a generated development password is not worth cracking, and
#: comfortably over the 12-character minimum the register schema enforces.
GENERATED_PASSWORD_BYTES = 18


async def create_user(
    email: str,
    password: str | None,
    group_names: list[str],
    full_name: str | None,
) -> int:
    """Create (or reuse) a user and add them to ``group_names``.

    Returns a process exit code: 0 on success, 1 on a failure the caller should
    see, such as a group that does not exist.
    """
    generated = password is None
    password = password or secrets.token_urlsafe(GENERATED_PASSWORD_BYTES)

    async with AsyncSessionLocal() as session:
        # Resolve every group BEFORE creating anything, so a typo does not leave
        # a half-configured account behind.
        group_ids: list[int] = []
        for name in group_names:
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
                return 1
            group_ids.append(group_id)

        try:
            user = await auth_service.register(
                session, email=email, password=password, full_name=full_name
            )
            created = True
        except ConflictError:
            # Already registered: reuse the account and just apply the groups.
            # Rolled back by register(), so the session is usable again.
            user = await session.scalar(select(User).where(User.email == email.lower()))
            if user is None:  # pragma: no cover - only if deleted concurrently
                print(f"Could not resolve existing user {email!r}", file=sys.stderr)
                return 1
            created = False
            generated = False  # the stored password is unchanged; do not print one

        # Capture the identifiers NOW, before any further commit. Each commit
        # expires every loaded instance, and reading an expired attribute
        # triggers a lazy refresh — which raises MissingGreenlet under async
        # SQLAlchemy. Plain values survive where the instance does not.
        user_id, user_email = user.id, user.email

        for group_id in group_ids:
            await iam_service.add_user_to_group(session, user_id, group_id)

        granted = await iam_service.get_effective_permissions(session, user_id)

    print(f"{'Created' if created else 'Found'} {user_email} ({user_id})")
    if group_names:
        print(f"Groups: {', '.join(group_names)}")
    print(f"Permissions: {sorted(granted) or '(none)'}")
    if generated:
        print(f"\nGenerated password (shown once): {password}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("email")
    parser.add_argument(
        "--group",
        action="append",
        default=[],
        metavar="NAME",
        help="Group to add the user to. Repeat for several.",
    )
    parser.add_argument("--password", help="Password. Generated when omitted.")
    parser.add_argument("--full-name", default=None)
    args = parser.parse_args()

    raise SystemExit(
        asyncio.run(create_user(args.email, args.password, args.group, args.full_name))
    )


if __name__ == "__main__":
    main()
