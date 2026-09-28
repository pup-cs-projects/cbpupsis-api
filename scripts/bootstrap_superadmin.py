"""Assign an existing verified account the Superadmin role outside the API."""

from __future__ import annotations

import argparse
import asyncio
import sys

from sqlalchemy import select

from cbpupsis_database.models.iam import Group
from cbpupsis_database.models.users import User
from cbpupsis_database.session import AsyncSessionLocal
from cbpupsis_shared.domains.iam import service as iam_service
from cbpupsis_shared.domains.iam.constants import ADMIN_GROUP, SUPERADMIN_GROUP


async def bootstrap_superadmin(email: str) -> int:
    async with AsyncSessionLocal() as db:
        user = await db.scalar(select(User).where(User.email == email.lower()))
        if user is None or not user.is_active or user.deleted_at is not None:
            print("An active existing account is required.", file=sys.stderr)
            return 1
        if user.email_verified_at is None:
            print("Verify the account email before promotion.", file=sys.stderr)
            return 1
        if await iam_service.is_user_in_group(db, user.id, ADMIN_GROUP):
            print("Admin accounts cannot also be Superadmins.", file=sys.stderr)
            return 1
        group = await db.scalar(select(Group).where(Group.name == SUPERADMIN_GROUP))
        if group is None:
            print("Run scripts.seed_iam first.", file=sys.stderr)
            return 1
        await iam_service.add_user_to_group(db, user.id, group.id)
        print(f"{email} added to {SUPERADMIN_GROUP}.")
        return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("email")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(bootstrap_superadmin(args.email)))


if __name__ == "__main__":
    main()
