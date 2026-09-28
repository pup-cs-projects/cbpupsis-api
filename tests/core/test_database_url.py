"""Tests for the libpq -> asyncpg URL rewrite.

A connection string copied straight from the Neon console carries
``?sslmode=require&channel_binding=require``. psycopg accepts those; asyncpg does
not, and it fails at *connect* time rather than at parse time — so the app starts
happily and dies on the first request with ``TypeError: connect() got an
unexpected keyword argument 'sslmode'``, which names neither the URL nor the
driver.

That is a bad failure to rediscover by hand, and it is invisible to the rest of
the suite: every other test runs on SQLite and never touches this path.
"""

from __future__ import annotations

import pytest

from cbpupsis_database.session import to_asyncpg_url


class TestSchemeRewrite:
    def test_postgresql_becomes_asyncpg(self) -> None:
        assert to_asyncpg_url("postgresql://u:p@h/db").startswith(
            "postgresql+asyncpg://"
        )

    def test_the_postgres_alias_is_also_rewritten(self) -> None:
        """Some providers hand out ``postgres://``; SQLAlchemy needs the driver
        either way."""
        assert to_asyncpg_url("postgres://u:p@h/db").startswith("postgresql+asyncpg://")

    def test_an_explicit_driver_is_left_alone(self) -> None:
        """Someone who already pinned a driver meant it."""
        url = "postgresql+psycopg://u:p@h/db"
        assert to_asyncpg_url(url).startswith("postgresql+psycopg://")


class TestLibpqParametersAreStripped:
    @pytest.mark.parametrize(
        "param",
        [
            "sslmode=require",
            "channel_binding=require",
            "target_session_attrs=read-write",
            "connect_timeout=10",
            "application_name=whatever",
        ],
    )
    def test_asyncpg_rejects_these_so_they_go(self, param: str) -> None:
        name = param.split("=")[0]
        assert name not in to_asyncpg_url(f"postgresql://u:p@h/db?{param}")

    def test_a_real_neon_url_survives_intact_apart_from_the_params(self) -> None:
        """The shape the console actually gives you."""
        result = to_asyncpg_url(
            "postgresql://owner:secret@ep-x-pooler.c-3.ap-southeast-1.aws.neon.tech"
            "/neondb?sslmode=require&channel_binding=require"
        )
        assert result == (
            "postgresql+asyncpg://owner:secret@"
            "ep-x-pooler.c-3.ap-southeast-1.aws.neon.tech/neondb"
        )

    def test_a_local_url_is_unchanged_apart_from_the_scheme(self) -> None:
        assert (
            to_asyncpg_url("postgresql://postgres:postgres@db:5432/app")
            == "postgresql+asyncpg://postgres:postgres@db:5432/app"
        )

    def test_credentials_and_host_are_not_mangled(self) -> None:
        """The rewrite goes through urlsplit/urlunsplit rather than string
        surgery, so a password containing URL-ish characters survives."""
        url = "postgresql://user:np-g_A1%2Bb@host.example.com:5432/db?sslmode=require"
        result = to_asyncpg_url(url)
        assert "np-g_A1%2Bb" in result
        assert "host.example.com:5432" in result
        assert result.endswith("/db")

    def test_a_parameter_asyncpg_understands_is_kept(self) -> None:
        """Only the libpq-only names are dropped; this is not a blanket wipe of
        the query string."""
        assert "server_settings" in to_asyncpg_url(
            "postgresql://u:p@h/db?server_settings=x&sslmode=require"
        )
