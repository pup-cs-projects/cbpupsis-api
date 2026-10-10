"""Optional PostgreSQL checks for Superadmin row locks and ledger grants."""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from cbpupsis_api_admin.domains.superadmin_ops import service as superadmin_service
from cbpupsis_api_admin.domains.superadmin_ops.exceptions import (
    RestoreRequestConflictError,
)
from cbpupsis_database.models.iam import Group
from cbpupsis_database.models.superadmin_ops import SystemBackup
from cbpupsis_database.models.users import User
from cbpupsis_database.session import to_asyncpg_url
from cbpupsis_shared.domains.iam import service as iam_service
from cbpupsis_shared.domains.iam.constants import SUPERADMIN_GROUP

RUNTIME_URL = os.environ.get("ISSUE70_POSTGRES_URL")
OWNER_URL = os.environ.get("ISSUE70_POSTGRES_OWNER_URL")


@pytest.mark.skipif(not RUNTIME_URL, reason="ISSUE70_POSTGRES_URL not configured")
async def test_AC0046_concurrent_distinct_approvals_only_one_wins() -> None:
    engine = create_async_engine(to_asyncpg_url(RUNTIME_URL))
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with sessions() as db:
            group = await db.scalar(select(Group).where(Group.name == SUPERADMIN_GROUP))
            if group is None:
                group = await iam_service.create_group(db, name=SUPERADMIN_GROUP)
            users = [
                User(
                    email=f"issue70-{uuid.uuid4()}@example.com",
                    password_hash="unusable-test-fixture",
                    email_verified_at=datetime.now(UTC),
                )
                for _ in range(3)
            ]
            db.add_all(users)
            await db.commit()
            for user in users:
                await iam_service.add_user_to_group(db, user.id, group.id)
            backup = SystemBackup(
                backup_type="full",
                byte_size=1,
                status="completed",
                storage_location="test://issue70-concurrent",
            )
            db.add(backup)
            await db.commit()
            await superadmin_service.request_restore(
                db, actor_id=users[0].id, backup_id=backup.id
            )

        barrier = asyncio.Barrier(2)

        async def approve(actor_id: uuid.UUID) -> str:
            async with sessions() as db:
                await barrier.wait()
                try:
                    await superadmin_service.approve_restore(
                        db, actor_id=actor_id, backup_id=backup.id
                    )
                except RestoreRequestConflictError:
                    await db.rollback()
                    return "conflict"
                return "approved"

        outcomes = await asyncio.gather(approve(users[1].id), approve(users[2].id))
        assert sorted(outcomes) == ["approved", "conflict"]
    finally:
        await engine.dispose()


@pytest.mark.skipif(
    not (RUNTIME_URL and OWNER_URL),
    reason="Both PostgreSQL role URLs are required",
)
async def test_AC0045_runtime_role_has_append_only_ledger_grants() -> None:
    runtime = create_async_engine(to_asyncpg_url(RUNTIME_URL))
    owner = create_async_engine(to_asyncpg_url(OWNER_URL))
    try:
        async with runtime.connect() as connection:
            runtime_name = await connection.scalar(text("SELECT current_user"))
            for table in (
                "audit_entries",
                "admin_audit_trails",
                "admin_audit_trails_default",
            ):
                privileges = await connection.execute(
                    text(
                        "SELECT has_table_privilege(current_user, :table, 'SELECT'), "
                        "has_table_privilege(current_user, :table, 'INSERT'), "
                        "has_table_privilege(current_user, :table, 'UPDATE'), "
                        "has_table_privilege(current_user, :table, 'DELETE'), "
                        "has_table_privilege(current_user, :table, 'TRUNCATE')"
                    ),
                    {"table": table},
                )
                assert privileges.one() == (True, True, False, False, False)
            await connection.rollback()
            for statement in (
                "UPDATE audit_entries SET action = 'forbidden' WHERE id = :id",
                "DELETE FROM audit_entries WHERE id = :id",
            ):
                entry_id = uuid.uuid4()
                transaction = await connection.begin()
                await connection.execute(
                    text(
                        "INSERT INTO audit_entries(id, action, payload, occurred_at) "
                        "VALUES (:id, 'validation.insert', '{}', now())"
                    ),
                    {"id": entry_id},
                )
                with pytest.raises(DBAPIError):
                    await connection.execute(text(statement), {"id": entry_id})
                await transaction.rollback()
                assert (
                    await connection.scalar(
                        text("SELECT count(*) FROM audit_entries WHERE id = :id"),
                        {"id": entry_id},
                    )
                ) == 0
                await connection.rollback()
        async with owner.connect() as connection:
            owner_name = await connection.scalar(text("SELECT current_user"))
            assert owner_name != runtime_name
            for trigger in (
                "audit_entries_append_only",
                "admin_audit_trails_append_only",
            ):
                count = await connection.scalar(
                    text(
                        "SELECT count(*) FROM pg_trigger "
                        "WHERE tgname = :trigger AND NOT tgisinternal"
                    ),
                    {"trigger": trigger},
                )
                assert count >= 1
    finally:
        await runtime.dispose()
        await owner.dispose()


@pytest.mark.skipif(
    not (RUNTIME_URL and OWNER_URL),
    reason="Both PostgreSQL role URLs are required",
)
async def test_AC0045_runtime_role_cannot_write_alembic_version() -> None:
    runtime = create_async_engine(to_asyncpg_url(RUNTIME_URL))
    owner = create_async_engine(to_asyncpg_url(OWNER_URL))
    try:
        async with runtime.connect() as connection:
            runtime_name = await connection.scalar(text("SELECT current_user"))
            privileges = await connection.execute(
                text(
                    "SELECT has_table_privilege(current_user, "
                    "'public.alembic_version', 'SELECT'), "
                    "has_table_privilege(current_user, "
                    "'public.alembic_version', 'INSERT'), "
                    "has_table_privilege(current_user, "
                    "'public.alembic_version', 'UPDATE'), "
                    "has_table_privilege(current_user, "
                    "'public.alembic_version', 'DELETE'), "
                    "has_table_privilege(current_user, "
                    "'public.alembic_version', 'TRUNCATE')"
                )
            )
            assert privileges.one() == (True, False, False, False, False)
            with pytest.raises(DBAPIError):
                await connection.execute(
                    text(
                        "UPDATE public.alembic_version "
                        "SET version_num = version_num WHERE false"
                    )
                )
            await connection.rollback()

        async with owner.connect() as connection:
            owner_name = await connection.scalar(
                text(
                    "SELECT pg_get_userbyid(relowner) FROM pg_class "
                    "WHERE oid = 'public.alembic_version'::regclass"
                )
            )
            assert runtime_name != owner_name
            assert not await connection.scalar(
                text("SELECT pg_has_role(:runtime, :owner, 'member')"),
                {"runtime": runtime_name, "owner": owner_name},
            )
            role_flags = await connection.execute(
                text(
                    "SELECT rolcanlogin, rolsuper, rolcreaterole "
                    "FROM pg_roles WHERE rolname = :runtime"
                ),
                {"runtime": runtime_name},
            )
            assert role_flags.one() == (True, False, False)
            public_write = await connection.scalar(
                text(
                    "SELECT EXISTS ("
                    "  SELECT 1 FROM pg_class c "
                    "  CROSS JOIN LATERAL aclexplode("
                    "    COALESCE(c.relacl, acldefault('r', c.relowner))"
                    "  ) acl "
                    "  WHERE c.oid = 'public.alembic_version'::regclass "
                    "    AND acl.grantee = 0 "
                    "    AND acl.privilege_type IN ("
                    "      'INSERT', 'UPDATE', 'DELETE', 'TRUNCATE')"
                    ")"
                )
            )
            assert not public_write
            settable_write = await connection.scalar(
                text(
                    "SELECT EXISTS ("
                    "  SELECT 1 FROM pg_class c "
                    "  CROSS JOIN LATERAL aclexplode("
                    "    COALESCE(c.relacl, acldefault('r', c.relowner))"
                    "  ) acl "
                    "  JOIN pg_roles grantee ON grantee.oid = acl.grantee "
                    "  WHERE c.oid = 'public.alembic_version'::regclass "
                    "    AND acl.privilege_type IN ("
                    "      'INSERT', 'UPDATE', 'DELETE', 'TRUNCATE') "
                    "    AND pg_has_role(:runtime, grantee.rolname, 'member')"
                    ")"
                ),
                {"runtime": runtime_name},
            )
            assert not settable_write
    finally:
        await runtime.dispose()
        await owner.dispose()
