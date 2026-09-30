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
from sqlalchemy.ext.asyncio import create_async_engine

from cbpupsis_core.config import settings

# Imported for its side effect: registering every table on Base.metadata.
from cbpupsis_database import models as _models  # noqa: F401
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
_migration_url = to_asyncpg_url(settings.direct_database_url or settings.database_url)


def include_object(object, name, type_, reflected, compare_to):
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
    engine = create_async_engine(_migration_url)
    async with engine.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await engine.dispose()


def run_migrations_online():
    asyncio.run(run_async_migrations())


run_migrations_online()
