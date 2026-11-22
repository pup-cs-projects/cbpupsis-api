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


async def test_once_returns_processed_count(runtime):
    runtime.run_due.side_effect = [True, True, False]
    assert await worker.run(once=True) == 2


async def test_passed_deadline_claims_nothing(runtime):
    assert await worker.run(once=True, deadline=0.0) == 0
    runtime.sessions.assert_not_called()
    runtime.engine.dispose.assert_awaited_once()


def lambda_context(remaining_ms):
    return SimpleNamespace(get_remaining_time_in_millis=lambda: remaining_ms)


def test_lambda_handler_is_a_no_op_when_disabled(monkeypatch):
    monkeypatch.setattr(worker.settings, "google_calendar_enabled", False)
    run = AsyncMock()
    monkeypatch.setattr(worker, "run", run)
    assert worker.lambda_handler({}, lambda_context(300_000)) == {"processed": 0}
    run.assert_not_called()


def test_lambda_handler_runs_once_within_the_time_limit(monkeypatch):
    monkeypatch.setattr(worker.settings, "google_calendar_enabled", True)
    monkeypatch.setattr(worker, "time", SimpleNamespace(monotonic=lambda: 1000.0))
    run = AsyncMock(return_value=3)
    monkeypatch.setattr(worker, "run", run)
    assert worker.lambda_handler({}, lambda_context(300_000)) == {"processed": 3}
    run.assert_awaited_once_with(
        once=True, deadline=1000.0 + 300.0 - worker.LAMBDA_MARGIN_SECONDS
    )
