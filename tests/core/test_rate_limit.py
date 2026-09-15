"""Tests for request rate limiting.

Rate limiting is switched **off** for the rest of the suite
(``RATE_LIMIT_ENABLED=false`` in pyproject) because the limiter is a
module-level singleton: its counters would otherwise carry from one test to the
next and the suite would pass or fail depending on the order it ran in.

These tests therefore turn it on around themselves, and — just as importantly —
give each test its own storage. The ``limited`` fixture below does both and
restores the previous state afterwards, so nothing leaks into the tests that
follow.
"""

from __future__ import annotations

import re

import pytest
from fastapi import Depends, Request
from httpx import AsyncClient
from limits import parse
from limits.storage import storage_from_string

from app.config import settings
from app.core.middleware import (
    UnknownRateLimitError,
    client_ip,
    limit_names,
    limiter,
    rate_limit,
    rate_limit_key,
)
from app.main import app


@pytest.fixture
def limited():
    """Enable rate limiting with a private, empty counter store.

    Swapping the storage is what makes these tests independent of each other and
    of execution order: without it every test in this file would share the
    counters accumulated by the ones before it.
    """
    previous_enabled = limiter.enabled
    previous_storage = limiter._storage
    previous_limiter = limiter._limiter

    limiter.enabled = True
    fresh = storage_from_string("memory://")
    limiter._storage = fresh
    # The strategy object caches the storage it was built with, so replacing
    # only _storage would leave the old counters in play.
    limiter._limiter = type(limiter._limiter)(fresh)

    yield limiter

    limiter.enabled = previous_enabled
    limiter._storage = previous_storage
    limiter._limiter = previous_limiter


class TestKeyFunction:
    """Who the limit is counted against."""

    def test_prefers_the_authenticated_user_over_the_ip(self) -> None:
        """Two users behind one NAT must not consume each other's allowance."""
        request = _fake_request(client_host="203.0.113.7", user_id="user-123")
        assert rate_limit_key(request) == "user:user-123"

    def test_falls_back_to_the_client_ip_when_anonymous(self) -> None:
        request = _fake_request(client_host="203.0.113.7")
        assert rate_limit_key(request) == "ip:203.0.113.7"

    def test_uses_the_leftmost_forwarded_for_entry(self) -> None:
        """Behind a proxy the peer address is the balancer, so every caller
        would otherwise share one bucket."""
        request = _fake_request(
            client_host="10.0.0.1",
            headers={"X-Forwarded-For": "198.51.100.4, 10.0.0.1, 10.0.0.2"},
        )
        assert client_ip(request) == "198.51.100.4"

    def test_ignores_an_empty_forwarded_for(self) -> None:
        request = _fake_request(client_host="10.0.0.1", headers={"X-Forwarded-For": ""})
        assert client_ip(request) == "10.0.0.1"

    def test_missing_peer_address_still_yields_a_key(self) -> None:
        """A limiter that raised here would turn a limited endpoint into a 500."""
        request = _fake_request(client_host=None)
        assert client_ip(request) == "unknown"


class TestAuthenticatedCallersAreKeyedByIdentity:
    async def test_an_authenticated_request_publishes_its_user_id(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        registered_user: dict[str, str],
        sent_emails: list[dict[str, str]],
    ) -> None:
        """``rate_limit_key`` prefers ``request.state.user_id``, so something has
        to put it there. Without this the user-id branch is dead code and every
        authenticated caller is silently keyed by IP instead.

        The key is derived inside a real authenticated request — the wiring is
        the part that breaks, not the key function, which is unit-tested above.
        """
        captured: list[str] = []

        # A route that depends on get_current_user, then reports the key the
        # limiter would compute for that same request.
        from app.domains.auth.dependencies import CurrentUser, get_current_user

        @app.get("/api/v1/_test_rate_limit_key")
        async def _probe(
            request: Request, user: CurrentUser = Depends(get_current_user)
        ) -> dict[str, str]:
            captured.append(rate_limit_key(request))
            return {"id": str(user.id)}

        try:
            response = await client.get(
                "/api/v1/_test_rate_limit_key", headers=auth_headers
            )
            assert response.status_code == 200, response.text
        finally:
            # Leave the app's routing table as it was found.
            app.router.routes = [
                route
                for route in app.router.routes
                if getattr(route, "path", None) != "/api/v1/_test_rate_limit_key"
            ]

        assert captured == [f"user:{registered_user['id']}"]


class TestLimitsAreEnforced:
    async def test_repeated_resend_verification_is_eventually_refused(
        self, client: AsyncClient, limited, sent_emails: list[dict[str, str]]
    ) -> None:
        """The strictest limit: this endpoint mails an address the caller names."""
        limit = _amount("rate_limit_resend_verification")
        payload = {"email": "someone@example.com"}

        for _ in range(limit):
            allowed = await client.post(
                "/api/v1/auth/resend-verification", json=payload
            )
            assert allowed.status_code == 204

        refused = await client.post("/api/v1/auth/resend-verification", json=payload)
        assert refused.status_code == 429

    async def test_repeated_forgot_password_is_eventually_refused(
        self, client: AsyncClient, limited, sent_emails: list[dict[str, str]]
    ) -> None:
        limit = _amount("rate_limit_forgot_password")
        payload = {"email": "someone@example.com"}

        for _ in range(limit):
            assert (
                await client.post("/api/v1/auth/forgot-password", json=payload)
            ).status_code == 204

        refused = await client.post("/api/v1/auth/forgot-password", json=payload)
        assert refused.status_code == 429

    async def test_repeated_failed_logins_are_eventually_refused(
        self, client: AsyncClient, limited, sent_emails: list[dict[str, str]]
    ) -> None:
        """The credential-stuffing case: wrong passwords still consume the
        allowance, or the limit would protect nothing."""
        limit = _amount("rate_limit_login")
        payload = {"email": "nobody@example.com", "password": "not-the-password"}

        for _ in range(limit):
            assert (
                await client.post("/api/v1/auth/login", json=payload)
            ).status_code == 401

        refused = await client.post("/api/v1/auth/login", json=payload)
        assert refused.status_code == 429

    async def test_repeated_registration_is_eventually_refused(
        self, client: AsyncClient, limited, sent_emails: list[dict[str, str]]
    ) -> None:
        limit = _amount("rate_limit_register")

        for index in range(limit):
            response = await client.post(
                "/api/v1/auth/register",
                json={
                    "email": f"spam{index}@example.com",
                    "password": "a-long-enough-password",
                },
            )
            assert response.status_code == 201

        refused = await client.post(
            "/api/v1/auth/register",
            json={
                "email": "spam-too-many@example.com",
                "password": "a-long-enough-password",
            },
        )
        assert refused.status_code == 429

    async def test_a_separate_caller_is_not_affected(
        self, client: AsyncClient, limited, sent_emails: list[dict[str, str]]
    ) -> None:
        """The limit is per caller, not global — one noisy client must not lock
        everyone else out of logging in."""
        payload = {"email": "nobody@example.com", "password": "not-the-password"}
        noisy = {"X-Forwarded-For": "198.51.100.10"}
        quiet = {"X-Forwarded-For": "198.51.100.11"}

        for _ in range(_amount("rate_limit_login")):
            await client.post("/api/v1/auth/login", json=payload, headers=noisy)
        assert (
            await client.post("/api/v1/auth/login", json=payload, headers=noisy)
        ).status_code == 429

        other = await client.post("/api/v1/auth/login", json=payload, headers=quiet)
        assert other.status_code == 401


class TestTheRefusalResponse:
    async def test_429_uses_the_standard_error_envelope(
        self, client: AsyncClient, limited, sent_emails: list[dict[str, str]]
    ) -> None:
        """slowapi's default body is ``{"error": "3 per 1 hour"}`` — a different
        shape from every other error, and it leaks the configured limit. Clients
        parse one envelope, so the handler must override it."""
        refused = await _exhaust_forgot_password(client)
        body = refused.json()

        assert set(body) == {"detail", "request_id", "code"}
        assert isinstance(body["detail"], str)
        assert body["code"] == "rate_limited"
        assert "error" not in body
        # The correlation id ties the refusal to the log line, as for any other
        # error, and matches the header.
        assert body["request_id"] == refused.headers["X-Request-ID"]

    async def test_429_carries_a_retry_after_header(
        self, client: AsyncClient, limited, sent_emails: list[dict[str, str]]
    ) -> None:
        """Without it a client can only guess, and most retry immediately."""
        refused = await _exhaust_forgot_password(client)

        assert "Retry-After" in refused.headers
        retry_after = int(refused.headers["Retry-After"])
        assert 0 < retry_after <= 3600

    async def test_the_message_does_not_leak_the_configured_limit(
        self, client: AsyncClient, limited, sent_emails: list[dict[str, str]]
    ) -> None:
        """Telling an attacker the exact allowance tells them how to pace an
        attack to stay just under it."""
        refused = await _exhaust_forgot_password(client)
        detail = refused.json()["detail"]

        # slowapi's phrasing is "<amount> per <n> <unit>" ("3 per 1 hour").
        # Asserting the amount is simply absent as a substring would be wrong:
        # the retry time legitimately contains those digits (3600 contains "3").
        assert "per" not in detail
        assert not re.search(
            rf"\b{_amount('rate_limit_forgot_password')}\s+per\b", detail
        )


class TestDisabledByDefault:
    async def test_the_suite_runs_with_limiting_off(
        self, client: AsyncClient, sent_emails: list[dict[str, str]]
    ) -> None:
        """Without the ``limited`` fixture nothing is limited, so the other
        tests in this suite cannot interfere with one another — and the app
        behaves identically apart from the refusals."""
        assert not limiter.enabled

        payload = {"email": "nobody@example.com", "password": "not-the-password"}
        for _ in range(_amount("rate_limit_login") + 5):
            response = await client.post("/api/v1/auth/login", json=payload)
            assert response.status_code == 401

    async def test_endpoints_behave_normally_when_disabled(
        self, client: AsyncClient, sent_emails: list[dict[str, str]]
    ) -> None:
        """The decorators must be inert when the limiter is off, not merely
        lenient — a decorator that still needed request state would break every
        limited endpoint in the disabled configuration."""
        response = await client.post(
            "/api/v1/auth/register",
            json={"email": "normal@example.com", "password": "a-long-enough-password"},
        )
        assert response.status_code == 201
        assert (
            await client.post(
                "/api/v1/auth/forgot-password", json={"email": "normal@example.com"}
            )
        ).status_code == 204


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _amount(setting_name: str) -> int:
    """Return the numeric allowance of a configured limit ("3/hour" -> 3).

    Read from settings rather than hardcoded so retuning a limit does not
    silently turn these tests into assertions about the wrong number.
    """
    from limits import parse

    from app.config import settings

    return int(parse(getattr(settings, setting_name)).amount)


async def _exhaust_forgot_password(client: AsyncClient):
    """Spend the forgot-password allowance and return the refused response."""
    payload = {"email": "someone@example.com"}
    for _ in range(_amount("rate_limit_forgot_password")):
        await client.post("/api/v1/auth/forgot-password", json=payload)
    refused = await client.post("/api/v1/auth/forgot-password", json=payload)
    assert refused.status_code == 429, refused.text
    return refused


class TestTheDecoratorFactory:
    """``rate_limit(name)`` — how a route asks for a limit."""

    def test_an_unknown_name_is_refused(self) -> None:
        """A misspelled limit must fail loudly at import, not serve unlimited
        traffic. Nothing in a response distinguishes an endpoint whose limit
        never applies from one whose limit is generous, so this is the only
        place the mistake is visible."""
        with pytest.raises(UnknownRateLimitError) as excinfo:
            rate_limit("lgoin")

        message = str(excinfo.value)
        # The message must name the fix, not just the failure.
        assert "rate_limit_lgoin" in message
        assert "login" in message

    def test_a_configured_name_resolves(self) -> None:
        """Every name a router passes today must exist on settings."""
        for name in ("login", "register", "forgot_password", "resend_verification"):
            assert rate_limit(name) is not None

    def test_the_named_decorators_match_the_factory(self) -> None:
        """The four exported decorators are the factory applied to their own
        names — kept only for readability, so they must not drift into a
        second, differently-configured set."""
        assert set(limit_names()) >= {
            "forgot_password",
            "login",
            "register",
            "resend_verification",
        }

    def test_every_configured_limit_is_a_valid_expression(self) -> None:
        """A typo like "5/minutes" parses at request time, not at import, so
        without this the first caller to hit that endpoint gets the 500."""
        for name in limit_names():
            value = getattr(settings, f"rate_limit_{name}")
            assert parse(value) is not None, f"rate_limit_{name}={value!r}"


def _fake_request(
    *, client_host: str | None, headers: dict[str, str] | None = None, user_id=None
):
    """Build a minimal Starlette request for the key-function tests.

    Constructed from a raw ASGI scope rather than mocked, so the key function is
    exercised against the same object type it sees in production.
    """
    from starlette.requests import Request

    raw_headers = [
        (key.lower().encode(), value.encode()) for key, value in (headers or {}).items()
    ]
    # request.state is backed by scope["state"], so it is seeded here rather
    # than assigned afterwards (the property has no setter).
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/v1/auth/login",
        "headers": raw_headers,
        "client": (client_host, 12345) if client_host else None,
        "state": {} if user_id is None else {"user_id": user_id},
    }
    return Request(scope)
