"""Alembic environment. CRITICAL for the nested-domain layout:

Alembic autogenerate only detects models it can SEE via Base.metadata. With
models scattered across domains/*/models.py, we must import every domain's models
here (see the import block) or autogenerate silently skips tables — a classic
modular-monolith footgun.

Also: migrations run against the DIRECT (non-pooled) Neon URL. The PgBouncer
pooler runs in transaction mode, returning the connection to the pool at every
transaction boundary, so the session state Alembic depends on — SET, session
advisory locks, temp tables — does not survive between statements.
"""

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import settings

# --- Import ALL domain models so Base.metadata is complete ---
# Autogenerate only detects tables reachable from Base.metadata, so every
# domain's models module must be imported here or its tables are silently
# skipped. Add a line for each new domain that defines models.
from app.core.outbox import models as _outbox  # noqa: F401
from app.database import Base, to_asyncpg_url
from app.domains.audit import models as _audit  # noqa: F401
from app.domains.auth import models as _auth  # noqa: F401
from app.domains.iam import models as _iam  # noqa: F401
from app.domains.items import models as _items  # noqa: F401
from app.domains.notifications import models as _notifications  # noqa: F401
from app.domains.users import models as _users  # noqa: F401

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

# Use the direct URL for migrations; fall back to database_url if unset.
# Same rewrite as the app's engine, and imported rather than repeated: the
# libpq parameters Neon puts in the URL break asyncpg here exactly as they do
# there, and two copies of that list would drift.
_migration_url = to_asyncpg_url(settings.direct_database_url or settings.database_url)


def do_run_migrations(connection):
    context.configure(connection=connection, target_metadata=target_metadata)
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
