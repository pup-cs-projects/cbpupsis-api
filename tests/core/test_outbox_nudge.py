"""Tests for nudging the worker Lambda after a commit that staged outbox messages.

The nudge is an optimisation: the message is durable before it runs, and the
EventBridge schedule sweeps up anything a lost nudge missed. So the properties
pinned here are about cost and safety, not delivery:

- one invoke per request that committed a message, however many it staged;
- no invoke for a rolled-back transaction or a request that staged nothing;
- a failing invoke never fails the request;
- no AWS call at all where the worker function is not configured.

Not covered in-process: the real Lambda invoke and its IAM permission. Those are
checked on staging after deploy (cbpupsis-infra#3).
"""

from __future__ import annotations

import logging

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_core.config import settings
from cbpupsis_core.events import Event
from cbpupsis_database import session as session_module
from cbpupsis_database.session import REQUEST_FINISHED_HOOKS, get_db
from cbpupsis_shared.outbox import nudge, nudge_worker, publish_transactional

WORKER = "worker-lambda-test"


@pytest.fixture
def invokes(monkeypatch) -> list[str]:
    """Record invokes instead of calling AWS, with the worker configured."""
    calls: list[str] = []
    monkeypatch.setattr(settings, "outbox_worker_function_name", WORKER)
    monkeypatch.setattr(nudge, "_invoke", calls.append)
    return calls


def _stage(db: AsyncSession) -> None:
    publish_transactional(db, Event(name="test.event", payload={}))


class TestWhenToNudge:
    async def test_a_committed_message_nudges_once(
        self, db: AsyncSession, invokes: list[str]
    ) -> None:
        _stage(db)
        await db.commit()

        await nudge_worker(db)

        assert invokes == [WORKER]

    async def test_many_messages_in_one_request_nudge_once(
        self, db: AsyncSession, invokes: list[str]
    ) -> None:
        """One drain claims every due message, so a second invoke only wakes a
        worker that finds nothing."""
        _stage(db)
        _stage(db)
        await db.commit()
        _stage(db)
        await db.commit()

        await nudge_worker(db)
        await nudge_worker(db)

        assert invokes == [WORKER]

    async def test_a_rolled_back_message_does_not_nudge(
        self, db: AsyncSession, invokes: list[str]
    ) -> None:
        _stage(db)
        await db.rollback()

        await nudge_worker(db)

        assert invokes == []

    async def test_a_rollback_after_the_commit_still_nudges(
        self, db: AsyncSession, invokes: list[str]
    ) -> None:
        """The committed message is real; a later failed statement in the same
        request does not undo it."""
        _stage(db)
        await db.commit()
        await db.rollback()

        await nudge_worker(db)

        assert invokes == [WORKER]

    async def test_a_request_without_messages_does_not_nudge(
        self, db: AsyncSession, invokes: list[str]
    ) -> None:
        await db.commit()

        await nudge_worker(db)

        assert invokes == []

    async def test_an_unconfigured_worker_is_never_invoked(
        self, db: AsyncSession, invokes: list[str], monkeypatch
    ) -> None:
        """Local, test, and CI runs have no worker Lambda."""
        monkeypatch.setattr(settings, "outbox_worker_function_name", None)
        _stage(db)
        await db.commit()

        await nudge_worker(db)

        assert invokes == []


class TestNudgeIsBestEffort:
    async def test_a_failing_invoke_does_not_raise(
        self, db: AsyncSession, monkeypatch, caplog
    ) -> None:
        """The request already committed. Raising would report a failure for
        work that succeeded, while the sweep delivers the message anyway."""

        def _boom(_name: str) -> None:
            raise RuntimeError("lambda unavailable")

        monkeypatch.setattr(settings, "outbox_worker_function_name", WORKER)
        monkeypatch.setattr(nudge, "_invoke", _boom)
        _stage(db)
        await db.commit()

        with caplog.at_level(logging.WARNING, logger=nudge.__name__):
            await nudge_worker(db)

        assert "outbox.nudge.failed" in caplog.text


class TestInvoke:
    def test_invokes_asynchronously_with_an_empty_payload(self, monkeypatch) -> None:
        """``Event`` returns as soon as Lambda queues it, so the request waits
        for the hand-off, not for the drain."""
        calls: list[dict] = []

        class _Client:
            def invoke(self, **kwargs) -> None:
                calls.append(kwargs)

        monkeypatch.setattr(nudge, "_lambda_client", lambda: _Client())

        nudge._invoke(WORKER)

        assert calls == [
            {"FunctionName": WORKER, "InvocationType": "Event", "Payload": b"{}"}
        ]


class TestRequestHook:
    def test_the_nudge_is_registered_once(self) -> None:
        assert REQUEST_FINISHED_HOOKS.count(nudge_worker) == 1

    async def test_get_db_runs_the_hooks_with_the_request_session(
        self, monkeypatch
    ) -> None:
        seen: list[AsyncSession] = []

        async def _record(session: AsyncSession) -> None:
            seen.append(session)

        monkeypatch.setattr(session_module, "REQUEST_FINISHED_HOOKS", [_record])

        dependency = get_db()
        session = await anext(dependency)
        await dependency.aclose()

        assert seen == [session]
