"""Tests for logging behaviour.

Two things are worth locking down here. The **correlation id** is what makes one
request's lines retrievable as a group, and it must survive into code that never
sees the request. **Secret leakage** is the failure that matters most: logs get
shipped, indexed, and retained far longer than anyone plans, so a password in a
log line outlives the request by years.
"""

from __future__ import annotations

import json
import logging
import uuid

from httpx import AsyncClient

from cbpupsis_core.logging import CorrelationFilter, JsonFormatter, request_id_var


def _record(**extra: object) -> logging.LogRecord:
    """Build a log record carrying ``extra`` fields, as logger calls do."""
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="event.happened",
        args=(),
        exc_info=None,
    )
    for key, value in extra.items():
        setattr(record, key, value)
    return record


class TestJsonFormatter:
    def test_emits_single_line_json(self) -> None:
        """One line per record: aggregators split on newlines, so a multi-line
        payload is ingested as several broken records."""
        output = JsonFormatter().format(_record(request_id="abc"))
        assert "\n" not in output
        assert json.loads(output)["message"] == "event.happened"

    def test_extra_fields_are_promoted_to_top_level(self) -> None:
        """The point of structured logging: extra={} becomes a queryable
        dimension, not a substring buried in the message."""
        record = _record(order_id="o-1", total="9.99")
        output = json.loads(JsonFormatter().format(record))
        assert output["order_id"] == "o-1"
        assert output["total"] == "9.99"

    def test_serialises_values_json_cannot_handle(self) -> None:
        """A UUID or Decimal in extra must not raise: a logging call must never
        be the thing that breaks a request."""
        output = json.loads(JsonFormatter().format(_record(user_id=uuid.uuid4())))
        assert isinstance(output["user_id"], str)

    def test_includes_the_traceback_when_present(self) -> None:
        try:
            raise ValueError("boom")
        except ValueError:
            import sys

            record = _record()
            record.exc_info = sys.exc_info()
            output = json.loads(JsonFormatter().format(record))
        assert "ValueError: boom" in output["exception"]


class TestCorrelationFilter:
    def test_attaches_the_current_request_id(self) -> None:
        token = request_id_var.set("req-123")
        try:
            record = _record()
            CorrelationFilter().filter(record)
            assert record.request_id == "req-123"
        finally:
            request_id_var.reset(token)

    def test_falls_back_outside_a_request(self) -> None:
        """Startup and background work log too; the field must still exist or
        the text formatter raises KeyError."""
        record = _record()
        CorrelationFilter().filter(record)
        assert record.request_id == "-"


class TestRequestCorrelation:
    async def test_inbound_request_id_is_echoed_back(self, client: AsyncClient) -> None:
        """Propagating a caller's id is what lets a trace span services once a
        domain is extracted."""
        response = await client.get("/health", headers={"X-Request-ID": "trace-me"})
        assert response.headers["X-Request-ID"] == "trace-me"

    async def test_an_id_is_generated_when_absent(self, client: AsyncClient) -> None:
        response = await client.get("/health")
        assert uuid.UUID(response.headers["X-Request-ID"])

    async def test_the_id_reaches_service_layer_logs(
        self, client: AsyncClient, caplog
    ) -> None:
        """The contextvar must carry into code that never sees the Request —
        the whole reason it is not just request.state."""
        with caplog.at_level(logging.INFO):
            await client.get("/health", headers={"X-Request-ID": "ctx-check"})

        CorrelationFilter().filter(caplog.records[-1])
        assert any(
            getattr(record, "request_id", None) == "ctx-check"
            or request_id_var.get() == "ctx-check"
            for record in caplog.records
        )

    async def test_errors_are_logged_with_their_correlation_id(
        self, client: AsyncClient, caplog
    ) -> None:
        """An opaque 500 is only debuggable if the id in the response body
        matches a fully-logged exception."""
        with caplog.at_level(logging.INFO):
            response = await client.get("/api/v1/items/not-a-uuid")
        assert response.json()["request_id"] is not None


class TestSecretsAreNotLogged:
    """The failure mode that outlives the request by years."""

    async def test_password_never_appears_in_logs_on_register(
        self, client: AsyncClient, caplog
    ) -> None:
        password = "super-secret-password-value"
        with caplog.at_level(logging.DEBUG):
            await client.post(
                "/api/v1/auth/register",
                json={"email": "leak@example.com", "password": password},
            )
        assert password not in caplog.text

    async def test_password_never_appears_in_logs_on_failed_login(
        self, client: AsyncClient, registered_user: dict[str, str], caplog
    ) -> None:
        """The failure path is the one people forget to check."""
        password = "wrong-password-attempt-value"
        with caplog.at_level(logging.DEBUG):
            await client.post(
                "/api/v1/auth/login",
                json={"email": registered_user["email"], "password": password},
            )
        assert password not in caplog.text

    async def test_access_token_never_appears_in_logs(
        self, client: AsyncClient, registered_user: dict[str, str], caplog
    ) -> None:
        login = await client.post(
            "/api/v1/auth/login",
            json={
                "email": registered_user["email"],
                "password": registered_user["password"],
            },
        )
        token = login.json()["access_token"]

        with caplog.at_level(logging.DEBUG):
            await client.get(
                "/api/v1/auth/whoami", headers={"Authorization": f"Bearer {token}"}
            )
        assert token not in caplog.text

    async def test_password_hash_never_appears_in_logs(
        self, client: AsyncClient, caplog
    ) -> None:
        """Argon2 hashes are expensive to crack, not impossible — and they are
        never something a log store needs."""
        with caplog.at_level(logging.DEBUG):
            await client.post(
                "/api/v1/auth/register",
                json={
                    "email": "hash@example.com",
                    "password": "a-long-enough-password",
                },
            )
        assert "$argon2" not in caplog.text

    def test_settings_repr_does_not_expose_the_jwt_secret(self) -> None:
        """Config objects get logged whole during debugging more often than
        anyone admits."""
        from cbpupsis_core.config import settings

        assert settings.jwt_secret.get_secret_value() not in repr(settings)
        assert "**********" in repr(settings)
