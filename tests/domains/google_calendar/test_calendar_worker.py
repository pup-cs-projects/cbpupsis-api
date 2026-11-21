"""Google worker startup must validate the configured restricted database role."""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from cbpupsis_api_student import calendar_worker as worker


@pytest.fixture
def runtime(monkeypatch):
    db = SimpleNamespace(commit=AsyncMock())

    @asynccontextmanager
    async def session():
        yield db

    engine = SimpleNamespace(dispose=AsyncMock())
    validate = AsyncMock()
    prune = AsyncMock()
    run_due = AsyncMock(return_value=False)
    sessions = Mock(side_effect=session)
    monkeypatch.setattr(worker.settings, "audit_runtime_role", "synthetic_runtime")
    monkeypatch.setattr(worker, "engine", engine)
    monkeypatch.setattr(worker, "validate_runtime_engine", validate)
    monkeypatch.setattr(worker, "AsyncSessionLocal", sessions)
    monkeypatch.setattr(worker, "GoogleProvider", lambda client: object())
    monkeypatch.setattr(worker.service, "require_enabled", lambda: None)
    monkeypatch.setattr(worker.repository, "prune_states", prune)
    monkeypatch.setattr(worker.service, "run_due", run_due)
    return SimpleNamespace(
        db=db,
        engine=engine,
        validate=validate,
        prune=prune,
        run_due=run_due,
        sessions=sessions,
    )


async def test_once_validates_runtime_role_before_polling(runtime):
    async def validate(engine, role):
        assert engine is runtime.engine and role == "synthetic_runtime"
        runtime.sessions.assert_not_called()

    runtime.validate.side_effect = validate
    await worker.run(once=True)
    runtime.validate.assert_awaited_once_with(runtime.engine, "synthetic_runtime")
    runtime.prune.assert_awaited_once()
    runtime.db.commit.assert_awaited_once()
    runtime.run_due.assert_awaited_once()
    runtime.engine.dispose.assert_awaited_once()


async def test_failed_runtime_gate_does_not_poll_and_disposes_engine(runtime):
    runtime.validate.side_effect = RuntimeError("synthetic runtime gate failure")
    with pytest.raises(RuntimeError, match="synthetic runtime gate failure"):
        await worker.run(once=True)
    runtime.sessions.assert_not_called()
    runtime.run_due.assert_not_awaited()
    runtime.engine.dispose.assert_awaited_once()


async def test_poll_failure_disposes_engine(runtime):
    runtime.run_due.side_effect = RuntimeError("synthetic polling failure")
    with pytest.raises(RuntimeError, match="synthetic polling failure"):
        await worker.run(once=True)
    runtime.validate.assert_awaited_once_with(runtime.engine, "synthetic_runtime")
    runtime.engine.dispose.assert_awaited_once()
