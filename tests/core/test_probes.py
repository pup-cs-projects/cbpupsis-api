"""Tests for the liveness and readiness probes.

These are two endpoints answering two different questions, and the distinction
is the whole point: ``/health`` says "the process is alive, do not restart me",
``/ready`` says "I can serve traffic, route to me". Conflating them is the
mistake ``/health``'s docstring warns against — a liveness check that queries
the database turns a database blip into a rolling restart of every healthy
container.

So the assertion that matters most here is the negative one: that ``/health``
still does **not** touch the database, no matter what ``/ready`` grew.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.exc import OperationalError

from app.main import app


@pytest.fixture
async def probe_client():
    """A client with no database override, so the probes hit the real engine."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac


class TestLivenessStaysDatabaseFree:
    async def test_health_does_not_touch_the_database(
        self, probe_client: AsyncClient
    ) -> None:
        """The guard on the rule ``/health``'s docstring states.

        Patched to raise: if liveness ever grows a query, this fails rather than
        silently reintroducing the coupling that gets healthy pods killed.
        """
        with patch("app.main._check_database", side_effect=AssertionError("queried")):
            response = await probe_client.get("/health")

        assert response.status_code == 200
        assert response.json()["status"] == "ok"


class TestReadiness:
    async def test_ready_returns_200_when_the_database_answers(
        self, probe_client: AsyncClient
    ) -> None:
        """The success path, with the check stubbed to succeed.

        Stubbed rather than genuinely round-tripping: the suite runs on
        in-memory SQLite and the probe deliberately uses the application engine,
        which points at a Postgres that is not running here. Proving the query
        itself works needs a real database, so that belongs to the HTTP suite
        (``bruno/``), which has one. What is worth pinning in-process is the
        mapping from "check succeeded" to 200/ready.
        """

        async def _answers() -> None:
            return None

        with patch("app.main._check_database", _answers):
            response = await probe_client.get("/ready")

        assert response.status_code == 200, response.text
        assert response.json()["status"] == "ready"

    async def test_ready_returns_503_when_the_database_is_down(
        self, probe_client: AsyncClient
    ) -> None:
        """503, not a 200 with a status field: orchestrators route on the code,
        so a body saying "not ready" behind a 200 keeps traffic arriving."""
        failure = OperationalError("SELECT 1", {}, Exception("connection refused"))
        with patch("app.main._check_database", side_effect=failure):
            response = await probe_client.get("/ready")

        assert response.status_code == 503, response.text
        assert response.json()["status"] == "unavailable"

    async def test_ready_returns_503_when_the_database_hangs(
        self, probe_client: AsyncClient
    ) -> None:
        """A check that hangs is indistinguishable from one that failed, except
        it also ties up a worker — so the timeout must produce the same 503."""
        with patch("app.main.READINESS_TIMEOUT_SECONDS", 0.01):

            async def _never_answers(*args, **kwargs):
                import asyncio

                await asyncio.sleep(5)

            with patch("app.main._check_database", _never_answers):
                response = await probe_client.get("/ready")

        assert response.status_code == 503, response.text
        assert response.json()["status"] == "unavailable"

    async def test_ready_does_not_leak_the_failure_reason(
        self, probe_client: AsyncClient
    ) -> None:
        """The reason names infrastructure, and the probe is unauthenticated."""
        failure = OperationalError(
            "SELECT 1", {}, Exception("password authentication failed for user admin")
        )
        with patch("app.main._check_database", side_effect=failure):
            response = await probe_client.get("/ready")

        assert "password" not in response.text
        assert "admin" not in response.text
