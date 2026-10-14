"""AC-004.6: opt-in checks against a freshly migrated disposable PostgreSQL.

SUPERADMIN_POSTGRES_URL must identify the restricted runtime login on localhost.
No environment DATABASE_URL is ever used by these tests.
"""

from __future__ import annotations

import os
from urllib.parse import urlsplit

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from cbpupsis_database.runtime import validate_runtime_engine
from cbpupsis_database.session import to_asyncpg_url
from scripts.check_runtime_database import inspect_runtime

RUNTIME_URL = os.environ.get("SUPERADMIN_POSTGRES_URL", "")
OWNER_URL = os.environ.get("SUPERADMIN_POSTGRES_OWNER_URL", "")

pytestmark = pytest.mark.skipif(
    not RUNTIME_URL, reason="Requires explicit disposable SUPERADMIN_POSTGRES_URL"
)


@pytest_asyncio.fixture
async def runtime_engine():
    parts = urlsplit(RUNTIME_URL)
    assert parts.hostname in {"localhost", "127.0.0.1", "::1"}, (
        "Integration checks are limited to disposable local PostgreSQL"
    )
    engine = create_async_engine(to_asyncpg_url(RUNTIME_URL))
    yield engine
    await engine.dispose()


async def test_runtime_role_gate(runtime_engine):
    """AC-004.6: real login identity, memberships, partitions, grants, triggers."""
    role = urlsplit(RUNTIME_URL).username
    async with runtime_engine.connect() as connection, connection.begin():
        await connection.execute(text("SET TRANSACTION READ ONLY"))
        assert await inspect_runtime(connection, role) == []


@pytest.mark.parametrize(
    "table", ["audit_entries", "admin_audit_trails", "admin_audit_trails_default"]
)
@pytest.mark.parametrize("operation", ["UPDATE", "DELETE", "TRUNCATE"])
async def test_runtime_cannot_mutate_ledgers(runtime_engine, table, operation):
    """AC-004.5: actual denied statements, including direct partition access."""
    statement = (
        f"UPDATE {table} SET id = id WHERE false"
        if operation == "UPDATE"
        else f"DELETE FROM {table} WHERE false"
        if operation == "DELETE"
        else f"TRUNCATE {table}"
    )
    async with runtime_engine.connect() as connection:
        transaction = await connection.begin()
        try:
            with pytest.raises(DBAPIError) as error:
                await connection.execute(text(statement))
            assert getattr(error.value.orig, "sqlstate", None) == "42501"
        finally:
            await transaction.rollback()


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE alembic_version SET version_num = version_num WHERE false",
        "DELETE FROM alembic_version WHERE false",
        "INSERT INTO alembic_version (version_num) VALUES ('forbidden')",
        "TRUNCATE alembic_version",
    ],
)
async def test_runtime_cannot_rewrite_migration_history(runtime_engine, statement):
    async with runtime_engine.connect() as connection:
        transaction = await connection.begin()
        try:
            with pytest.raises(DBAPIError) as error:
                await connection.execute(text(statement))
            assert getattr(error.value.orig, "sqlstate", None) == "42501"
        finally:
            await transaction.rollback()


async def test_gate_rejects_wrong_identity(runtime_engine):
    async with runtime_engine.connect() as connection, connection.begin():
        assert await inspect_runtime(connection, "not_the_login_role") == [
            "Actual connection identity must equal AUDIT_RUNTIME_ROLE"
        ]


async def test_startup_engine_gate_is_fail_closed(runtime_engine):
    await validate_runtime_engine(runtime_engine, urlsplit(RUNTIME_URL).username)
    with pytest.raises(RuntimeError, match="AUDIT_RUNTIME_ROLE"):
        await validate_runtime_engine(runtime_engine, None)
    with pytest.raises(RuntimeError, match="Actual connection identity"):
        await validate_runtime_engine(runtime_engine, "not_the_login_role")


@pytest.mark.skipif(not OWNER_URL, reason="Requires explicit disposable owner URL")
@pytest.mark.parametrize(
    "grant_source", ["public", "noinherit", "partition", "schema_owner"]
)
async def test_gate_detects_effective_grant_bypasses(grant_source):
    """AC-004.6: PUBLIC, SET ROLE and direct partition grants cannot bypass ACLs.

    All adversarial DDL and authorization changes roll back in one transaction.
    Neither rows nor grants survive the test.
    """
    owner_parts, runtime_parts = urlsplit(OWNER_URL), urlsplit(RUNTIME_URL)
    assert owner_parts.hostname in {"localhost", "127.0.0.1", "::1"}
    assert (owner_parts.hostname, owner_parts.port, owner_parts.path) == (
        runtime_parts.hostname,
        runtime_parts.port,
        runtime_parts.path,
    )
    engine = create_async_engine(to_asyncpg_url(OWNER_URL))
    role = runtime_parts.username
    quote = engine.dialect.identifier_preparer.quote_identifier
    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            try:
                if grant_source == "public":
                    await connection.execute(
                        text("GRANT UPDATE ON audit_entries TO PUBLIC")
                    )
                elif grant_source == "partition":
                    await connection.execute(
                        text(
                            "GRANT UPDATE ON admin_audit_trails_default "
                            f"TO {quote(role)}"
                        )
                    )
                elif grant_source == "schema_owner":
                    await connection.execute(
                        text(f"ALTER SCHEMA public OWNER TO {quote(role)}")
                    )
                else:
                    await connection.execute(
                        text("CREATE ROLE scratch_gate_inherited NOLOGIN")
                    )
                    await connection.execute(
                        text("GRANT UPDATE ON audit_entries TO scratch_gate_inherited")
                    )
                    await connection.execute(
                        text(
                            f"GRANT scratch_gate_inherited TO {quote(role)} "
                            "WITH INHERIT FALSE"
                        )
                    )
                await connection.execute(
                    text(f"SET SESSION AUTHORIZATION {quote(role)}")
                )
                failures = await inspect_runtime(connection, role)
                expected = (
                    "schema/database owner"
                    if grant_source == "schema_owner"
                    else "Effective UPDATE must be denied"
                )
                assert any(expected in f for f in failures)
            finally:
                await transaction.rollback()
    finally:
        await engine.dispose()
