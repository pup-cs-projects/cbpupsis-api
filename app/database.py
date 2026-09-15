"""Async SQLAlchemy engine + session, configured for Neon.

Notes tied to Neon's quirks:
- Uses the POOLED connection string for the app.
- pool_pre_ping handles Neon's scale-to-zero: a stale connection (dropped after
  ~5 min idle) is detected and replaced instead of erroring on first request.
"""

from collections.abc import AsyncGenerator
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.config import settings

#: libpq query parameters that asyncpg does not accept. Neon hands you a URL
#: carrying these, and psycopg would take them, so it is easy to assume any
#: driver will. asyncpg raises ``TypeError: connect() got an unexpected keyword
#: argument 'sslmode'`` instead — at connect time, not at parse time, so the
#: failure surfaces on the first request rather than at startup.
_LIBPQ_ONLY_PARAMS = frozenset(
    {
        "sslmode",
        "channel_binding",
        "target_session_attrs",
        "connect_timeout",
        "application_name",
        "options",
    }
)


def to_asyncpg_url(url: str) -> str:
    """Rewrite a libpq-style Postgres URL for the asyncpg driver.

    Two changes, both required to use a connection string copied from Neon (or
    any other provider that hands out libpq URLs) unmodified:

    1. ``postgresql://`` -> ``postgresql+asyncpg://``, so SQLAlchemy selects the
       async driver.
    2. Strip libpq-only query parameters that asyncpg rejects.

    **Dropping ``sslmode=require`` does not disable TLS.** asyncpg negotiates
    SSL by default and Neon requires it server-side, so the connection is still
    encrypted; the parameter is redundant to asyncpg rather than meaningful to
    it. If you need to pin certificate verification, pass an ``ssl.SSLContext``
    through ``connect_args`` — that is asyncpg's equivalent, and it is stricter
    than ``sslmode`` ever was.
    """
    parts = urlsplit(url)
    kept = [(k, v) for k, v in parse_qsl(parts.query) if k not in _LIBPQ_ONLY_PARAMS]
    scheme = (
        "postgresql+asyncpg"
        if parts.scheme in ("postgresql", "postgres")
        else parts.scheme
    )
    return urlunsplit(
        (scheme, parts.netloc, parts.path, urlencode(kept), parts.fragment)
    )


_async_url = to_asyncpg_url(settings.database_url)

engine = create_async_engine(
    _async_url,
    pool_pre_ping=True,  # survive Neon scale-to-zero dropped connections
    # Sized explicitly rather than left to the default, because the ceiling is
    # per worker: N workers hold up to N x (pool_size + max_overflow)
    # connections, and exceeding the database limit fails at connect time under
    # load. env.example carries the arithmetic.
    pool_size=settings.db_pool_size,
    max_overflow=settings.db_max_overflow,
    # pool_pre_ping catches a dead connection at checkout; recycling retires one
    # before it dies. Both, because pre_ping costs a round trip it can only
    # spend after the fact.
    pool_recycle=settings.db_pool_recycle_seconds,
    echo=settings.debug,
)

AsyncSessionLocal = async_sessionmaker(
    engine, class_=AsyncSession, expire_on_commit=False
)


class Base(DeclarativeBase):
    """Single declarative base. Every domain's models inherit from this so
    Alembic autogenerate sees them all (see migrations/env.py)."""


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency: yields a session, closes it after the request."""
    async with AsyncSessionLocal() as session:
        yield session
