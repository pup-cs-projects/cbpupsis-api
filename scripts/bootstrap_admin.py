"""Assign an existing user the Admin role, position, and data scope.

Admin provisioning is deliberately performed outside the public authentication
API. The role permits Admin sign-in but does not grant Superadmin-owned IAM,
user-management, notification-administration, or audit-reading capabilities.

Run it once per environment, after ``seed_iam``::

    uv run python -m scripts.bootstrap_admin admin@example.com

The user must already exist (register through ``/api/v1/auth/register`` first).
Creating the account here too would mean this script sets passwords, and a
password typed on a command line lands in shell history.

Idempotent: re-running keeps the membership and updates the profile scope.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from sqlalchemy import select

from cbpupsis_api_admin.domains.admin_auth import service as admin_auth_service
from cbpupsis_database.models.admin_auth import AdminPosition
from cbpupsis_database.models.iam import Group
from cbpupsis_database.models.users import User
from cbpupsis_database.session import AsyncSessionLocal
from cbpupsis_shared.domains.iam import service as iam_service
from cbpupsis_shared.domains.iam.constants import ADMIN_GROUP

#: Re-exported so existing callers (and tests) can keep importing it from here.
#: The value itself lives in the IAM domain because seed_iam creates the group
#: this script looks up: two copies of the string is how the seed creates
#: "Admins" while the bootstrap searches for something else and finds nothing.
__all__ = ["ADMIN_GROUP", "bootstrap_admin", "main"]


async def bootstrap_admin(
    email: str,
    *,
    position: AdminPosition = AdminPosition.registrar,
    department_id: str | None = None,
    college_id: str | None = None,
) -> int:
    """Add the user to Admins and assign the position's data scope."""
    async with AsyncSessionLocal() as session:
        user = await session.scalar(
            select(User).where(User.email == email.lower(), User.deleted_at.is_(None))
        )
        if user is None:
            print(
                f"No user found for {email!r}.\n"
                "Register the account first: POST /api/v1/auth/register",
                file=sys.stderr,
            )
            return 1

        group = await session.scalar(select(Group).where(Group.name == ADMIN_GROUP))
        if group is None:
            print(
                f"No {ADMIN_GROUP!r} group found.\n"
                "Run the seed first: uv run python -m scripts.seed_iam",
                file=sys.stderr,
            )
            return 1

        await iam_service.add_user_to_group(session, user.id, group.id)
        await admin_auth_service.configure_admin_profile(
            session,
            user_id=user.id,
            position=position,
            department_id=department_id,
            college_id=college_id,
        )

        granted = await iam_service.get_effective_permissions(session, user.id)
        print(f"{email} added to {ADMIN_GROUP}.")
        print(f"Effective permissions: {sorted(granted)}")
        return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("email", help="Email of an already-registered user.")
    parser.add_argument(
        "--position",
        type=AdminPosition,
        choices=list(AdminPosition),
        default=AdminPosition.registrar,
    )
    parser.add_argument("--department-id")
    parser.add_argument("--college-id")
    args = parser.parse_args()
    raise SystemExit(
        asyncio.run(
            bootstrap_admin(
                args.email,
                position=args.position,
                department_id=args.department_id,
                college_id=args.college_id,
            )
        )
    )


if __name__ == "__main__":
    main()
