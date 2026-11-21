"""Development tolerates a shared database head from another FR branch."""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cbpupsis_database import runtime


class _Connection:
    execute = AsyncMock()

    @asynccontextmanager
    async def begin(self):
        yield self


@asynccontextmanager
async def _connect():
    yield _Connection()


def _engine():
    return SimpleNamespace(dialect=SimpleNamespace(name="postgresql"), connect=_connect)


def _failures(*extra: str):
    async def inspect(connection, role):
        return [f"Expected sole Alembic head {runtime.EXPECTED_HEAD}", *extra]

    return inspect


async def test_development_continues_on_head_drift(monkeypatch):
    monkeypatch.setattr(runtime.settings, "environment", "development")
    monkeypatch.setattr(runtime, "inspect_runtime", _failures())
    await runtime.validate_runtime_engine(_engine(), "app_runtime")


async def test_development_still_fails_other_invariants(monkeypatch):
    monkeypatch.setattr(runtime.settings, "environment", "development")
    monkeypatch.setattr(runtime, "inspect_runtime", _failures("Missing grant"))
    with pytest.raises(RuntimeError, match="Missing grant"):
        await runtime.validate_runtime_engine(_engine(), "app_runtime")


@pytest.mark.parametrize("environment", ["staging", "production"])
async def test_deployed_environments_keep_strict_head(monkeypatch, environment):
    monkeypatch.setattr(runtime.settings, "environment", environment)
    monkeypatch.setattr(runtime, "inspect_runtime", _failures())
    with pytest.raises(RuntimeError, match="Expected sole Alembic head"):
        await runtime.validate_runtime_engine(_engine(), "app_runtime")
