"""Read-only deployment gate for each API/worker's actual database identity.

Run after owner-run migrations, with the same DATABASE_URL as the process being
deployed. Never substitute the migration-owner URL for this check.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from cbpupsis_database.session import to_asyncpg_url

#: The migration head every deployed process must find. A constant, not read
#: from the migrations folder, because the student and faculty images do not
#: ship it. ``tests/core/test_runtime_head.py`` fails when a new revision lands
#: without updating this.
EXPECTED_HEAD = "g096_google_calendar"


async def inspect_runtime(
    connection: AsyncConnection, role: str, *, expected_head: str = EXPECTED_HEAD
) -> list[str]:
    """Return failed invariants without attempting any database repair."""
    failures: list[str] = []
    identity = (
        (
            await connection.execute(
                text("""
        SELECT current_user AS current_role, session_user AS login_role,
               rolsuper, rolcreaterole, rolcreatedb, rolbypassrls, rolreplication
        FROM pg_roles WHERE rolname = current_user
    """)
            )
        )
        .mappings()
        .one()
    )
    if not role or identity["current_role"] != role or identity["login_role"] != role:
        failures.append("Actual connection identity must equal AUDIT_RUNTIME_ROLE")
        return failures
    if any(
        identity[key]
        for key in (
            "rolsuper",
            "rolcreaterole",
            "rolcreatedb",
            "rolbypassrls",
            "rolreplication",
        )
    ):
        failures.append("Runtime login has administrative role attributes")
    unsafe_roles = (
        (
            await connection.execute(
                text("""
        SELECT r.rolname FROM pg_roles r
        WHERE pg_has_role(current_user, r.oid, 'MEMBER')
          AND (r.rolsuper OR r.rolcreaterole OR r.rolcreatedb
               OR r.rolbypassrls OR r.rolreplication)
    """)
            )
        )
        .scalars()
        .all()
    )
    if unsafe_roles:
        failures.append("Runtime can reach an administrative role")
    owner = await connection.scalar(
        text("""
        SELECT EXISTS (
            SELECT 1 FROM pg_database d WHERE d.datname = current_database()
              AND pg_has_role(current_user, d.datdba, 'MEMBER')
        ) OR EXISTS (
            SELECT 1 FROM pg_namespace n WHERE n.nspname = 'public'
              AND pg_has_role(current_user, n.nspowner, 'MEMBER')
        )
    """)
    )
    if owner:
        failures.append("Runtime can reach schema/database owner")
    tables = (
        (
            await connection.execute(
                text("""
        WITH RECURSIVE protected(oid, kind) AS (
            SELECT c.oid, CASE WHEN c.relname = 'alembic_version' THEN 'metadata'
                              ELSE 'ledger' END
            FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'public'
              AND c.relname IN (
                  'audit_entries', 'admin_audit_trails', 'alembic_version')
            UNION ALL
            SELECT i.inhrelid, p.kind
            FROM pg_inherits i JOIN protected p ON i.inhparent = p.oid
        )
        SELECT c.oid, n.nspname, c.relname, p.kind,
               pg_has_role(current_user, c.relowner, 'MEMBER') AS owns_table
        FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        LEFT JOIN protected p ON p.oid = c.oid
        WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p')
    """)
            )
        )
        .mappings()
        .all()
    )
    found = {row["relname"] for row in tables}
    for name in ("audit_entries", "admin_audit_trails", "alembic_version"):
        if name not in found:
            failures.append(f"Missing required table: {name}")
    for table in tables:
        name = f"{table['nspname']}.{table['relname']}"
        if table["owns_table"]:
            failures.append(f"Runtime can reach table owner: {name}")
        required = (
            ("SELECT",)
            if table["kind"] == "metadata"
            else (
                ("SELECT", "INSERT")
                if table["kind"] == "ledger"
                else ("SELECT", "INSERT", "UPDATE", "DELETE")
            )
        )
        forbidden = (
            ("INSERT", "UPDATE", "DELETE", "TRUNCATE")
            if table["kind"] == "metadata"
            else ("UPDATE", "DELETE", "TRUNCATE")
            if table["kind"] == "ledger"
            else ()
        )
        for privilege in required:
            allowed = await connection.scalar(
                text(
                    "SELECT has_table_privilege("
                    "current_user, CAST(:oid AS oid), :privilege)"
                ),
                {"oid": table["oid"], "privilege": privilege},
            )
            if not allowed:
                failures.append(f"Missing {privilege}: {name}")
        for privilege in forbidden:
            allowed = await connection.scalar(
                text("""
                SELECT EXISTS (
                    SELECT 1 FROM pg_roles r
                    WHERE pg_has_role(current_user, r.oid, 'MEMBER')
                      AND has_table_privilege(r.oid, CAST(:oid AS oid), :privilege)
                )
            """),
                {"oid": table["oid"], "privilege": privilege},
            )
            if allowed:
                failures.append(f"Effective {privilege} must be denied: {name}")
        if table["kind"] == "ledger":
            guarded = await connection.scalar(
                text("""
                SELECT EXISTS (
                    SELECT 1 FROM pg_trigger t JOIN pg_proc p ON p.oid = t.tgfoid
                    WHERE t.tgrelid = :oid AND t.tgenabled IN ('O', 'A')
                      AND (t.tgtype & 27) = 27
                      AND p.proname = 'refuse_audit_entry_mutation'
                )
            """),
                {"oid": table["oid"]},
            )
            if not guarded:
                failures.append(f"Missing enabled append-only trigger: {name}")
    if "alembic_version" in found:
        revisions = (
            (
                await connection.execute(
                    text("SELECT version_num FROM public.alembic_version")
                )
            )
            .scalars()
            .all()
        )
        if revisions != [expected_head]:
            failures.append(f"Expected sole Alembic head {expected_head}")
    sequences = (
        (
            await connection.execute(
                text("""
        SELECT c.oid, c.relname,
               pg_has_role(current_user, c.relowner, 'MEMBER') AS owns_sequence
        FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relkind = 'S'
    """)
            )
        )
        .mappings()
        .all()
    )
    for sequence in sequences:
        if sequence["owns_sequence"]:
            failures.append(f"Runtime can reach sequence owner: {sequence['relname']}")
        for privilege in ("USAGE", "SELECT"):
            allowed = await connection.scalar(
                text(
                    "SELECT has_sequence_privilege("
                    "current_user, CAST(:oid AS oid), :privilege)"
                ),
                {"oid": sequence["oid"], "privilege": privilege},
            )
            if not allowed:
                failures.append(f"Missing sequence {privilege}: {sequence['relname']}")
    return failures


async def check_database(url: str, role: str) -> list[str]:
    engine = create_async_engine(to_asyncpg_url(url))
    try:
        async with engine.connect() as connection, connection.begin():
            await connection.execute(text("SET TRANSACTION READ ONLY"))
            return await inspect_runtime(connection, role)
    finally:
        await engine.dispose()


async def validate_runtime_engine(engine: AsyncEngine, role: str | None) -> None:
    """Fail API/worker startup unless its actual engine uses restricted grants."""
    if not role or not role.strip():
        raise RuntimeError("AUDIT_RUNTIME_ROLE must identify a restricted runtime role")
    if engine.dialect.name != "postgresql":
        raise RuntimeError(
            "Restricted runtime database verification requires PostgreSQL"
        )
    try:
        async with engine.connect() as connection, connection.begin():
            await connection.execute(text("SET TRANSACTION READ ONLY"))
            failures = await inspect_runtime(connection, role)
    except Exception as exc:
        raise RuntimeError(
            f"Runtime database verification failed ({type(exc).__name__})"
        ) from None
    if failures:
        raise RuntimeError(
            "Runtime database verification failed: " + "; ".join(failures)
        )
