"""Promote an existing active, verified account after seed_iam. Never sets passwords."""

from __future__ import annotations

import argparse
import asyncio

from sqlalchemy import select

from cbpupsis_database.models.iam import Group
from cbpupsis_database.session import AsyncSessionLocal
from cbpupsis_shared.domains.iam import service as iam_service
from cbpupsis_shared.domains.iam.constants import ADMIN_GROUP, SUPERADMIN_GROUP
from cbpupsis_shared.domains.users import service as users_service


async def bootstrap_superadmin(email: str) -> int:
    async with AsyncSessionLocal() as db:
        user = await users_service.get_by_email(db, email.lower())
        if user is None or not user.is_active or user.email_verified_at is None:
            print("An existing active, verified account is required.")
            return 1
        if await iam_service.is_user_in_group(db, user.id, ADMIN_GROUP):
            print("Remove Admin membership before assigning Superadmin.")
            return 1
        group = await db.scalar(select(Group).where(Group.name == SUPERADMIN_GROUP))
        if group is None:
            print("Run scripts.seed_iam first.")
            return 1
        await iam_service.add_user_to_group(db, user.id, group.id)
        print("Superadmin membership configured; complete TOTP enrollment at sign-in.")
        return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("email")
    raise SystemExit(asyncio.run(bootstrap_superadmin(parser.parse_args().email)))


if __name__ == "__main__":
    main()
