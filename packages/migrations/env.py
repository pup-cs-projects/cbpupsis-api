"""Alembic environment for the one migration history every app shares.

Autogenerate only detects tables reachable from ``Base.metadata``. Importing
``cbpupsis_database.models`` registers every model module at once, so a new
table is picked up by adding its module there, not by editing this file.

Migrations run against the DIRECT (non-pooled) Neon URL. The PgBouncer pooler
runs in transaction mode, returning the connection to the pool at every
transaction boundary, so the session state Alembic depends on — SET, session
advisory locks, temp tables — does not survive between statements.
"""

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import CheckConstraint
from sqlalchemy.ext.asyncio import create_async_engine

# Imported for its side effect: registering every table on Base.metadata.
import cbpupsis_database.models  # noqa: F401
from cbpupsis_core.config import settings
from cbpupsis_database.base import Base
from cbpupsis_database.session import to_asyncpg_url

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

# Use the direct URL for migrations; fall back to database_url if unset.
# Same rewrite as the app's engine, and imported rather than repeated: the
# libpq parameters Neon puts in the URL break asyncpg here exactly as they do
# there, and two copies of that list would drift.
migration_url = to_asyncpg_url(settings.direct_database_url or settings.database_url)


#: CHECK constraints an ``Enum(create_constraint=True)`` generates. Autogenerate
#: leaves them out of the model side, so without this it sees the database copy
#: as unmatched and proposes dropping the enum guard (as it did for
#: ``ck_authentication_failures_step``, whose name already matches the model).
TYPE_BOUND_CHECKS = frozenset(
    constraint.name
    for table in target_metadata.tables.values()
    for constraint in table.constraints
    if isinstance(constraint, CheckConstraint)
    and getattr(constraint, "_type_bound", False)
)


def include_object(object, name, type_, reflected, compare_to):
    """Hide what autogenerate must not manage: enum CHECKs and partition defaults."""
    if type_ == "check_constraint" and name in TYPE_BOUND_CHECKS:
        return False
    return not (
        type_ == "table"
        and name
        in {
            "auth_audit_logs_default",
            "admin_audit_trails_default",
            "system_health_logs_default",
        }
    )


def do_run_migrations(connection):
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        include_object=include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations():
    engine = create_async_engine(migration_url)
    async with engine.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await engine.dispose()


def run_migrations_online():
    asyncio.run(run_async_migrations())


run_migrations_online()
