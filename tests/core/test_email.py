"""Tests for the email layer.

Three properties matter more than delivery itself, because each one breaks
something outside email when it is wrong: the console fallback (so tests and dev
never need a provider key), the failure isolation (so a bounce cannot undo a
signup), and the Resend request itself (sender, recipient, and an error that
never echoes the message).
"""

from __future__ import annotations

import logging
import uuid

import httpx
import pytest
from httpx import AsyncClient
from pydantic import SecretStr, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_core.config import Settings
from cbpupsis_core.emails import (
    ConsoleEmailSender,
    EmailDeliveryError,
    EmailSender,
    MailCatcherSender,
    ResendEmailSender,
    get_email_sender,
    send_email,
)
from cbpupsis_shared.domains.users import service as users_service


@pytest.fixture(autouse=True)
def _clear_sender_cache():
    """Reset the lru_cache so each test picks up its own settings."""
    get_email_sender.cache_clear()
    yield
    get_email_sender.cache_clear()


def _resend_configured(monkeypatch, *, api_key: str | None = "re_test_key") -> None:
    monkeypatch.setattr(
        "cbpupsis_core.emails.sender.settings.email_from",
        "CBPUPSIS <no-reply@example.com>",
    )
    monkeypatch.setattr(
        "cbpupsis_core.emails.sender.settings.resend_api_key",
        None if api_key is None else SecretStr(api_key),
    )


class TestSenderSelection:
    def test_console_backend_when_resend_is_unconfigured(self, monkeypatch) -> None:
        """The default in development and tests: no key, no real mail."""
        monkeypatch.setattr("cbpupsis_core.emails.sender.settings.email_from", None)
        monkeypatch.setattr("cbpupsis_core.emails.sender.settings.resend_api_key", None)
        assert isinstance(get_email_sender(), ConsoleEmailSender)

    def test_resend_backend_when_configured(self, monkeypatch) -> None:
        _resend_configured(monkeypatch)
        monkeypatch.setattr("cbpupsis_core.emails.sender.settings.dev_smtp_host", None)
        assert isinstance(get_email_sender(), ResendEmailSender)

    def test_the_sender_is_cached(self, monkeypatch) -> None:
        monkeypatch.setattr("cbpupsis_core.emails.sender.settings.email_from", None)
        monkeypatch.setattr("cbpupsis_core.emails.sender.settings.resend_api_key", None)
        assert get_email_sender() is get_email_sender()

    def test_every_backend_satisfies_the_protocol(self) -> None:
        assert isinstance(ConsoleEmailSender(), EmailSender)
        assert isinstance(MailCatcherSender(host="mailpit", port=1025), EmailSender)
        assert isinstance(
            ResendEmailSender(api_key="re_x", from_email="a@example.com"), EmailSender
        )

    def test_the_mail_catcher_wins_over_resend(self, monkeypatch) -> None:
        """Development with a catcher must never reach a real inbox."""
        _resend_configured(monkeypatch)
        monkeypatch.setattr(
            "cbpupsis_core.emails.sender.settings.dev_smtp_host", "mailpit"
        )
        assert isinstance(get_email_sender(), MailCatcherSender)


class TestResendSettings:
    def test_sender_without_a_key_is_refused(self) -> None:
        """Half a configuration would silently drop every email."""
        with pytest.raises(ValidationError, match="go together"):
            Settings(email_from="no-reply@example.com", resend_api_key=None)

    def test_key_without_a_sender_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="go together"):
            Settings(email_from=None, resend_api_key="re_test_key")

    def test_blank_values_mean_unset(self) -> None:
        """EMAIL_FROM= in an env file switches delivery off, it does not half-set it."""
        settings = Settings(email_from="", resend_api_key="")
        assert settings.email_enabled is False


class TestMailCatcher:
    """Development only: full messages go to Mailpit, never to the logs."""

    async def test_delivers_the_whole_message_over_smtp(self, monkeypatch) -> None:
        sent: dict[str, object] = {}

        class FakeSMTP:
            def __init__(self, host: str, port: int, timeout: float) -> None:
                sent["address"] = (host, port)

            def __enter__(self):
                return self

            def __exit__(self, *exc_info) -> None:
                return None

            def send_message(self, message) -> None:
                sent["message"] = message

        monkeypatch.setattr("cbpupsis_core.emails.sender.smtplib.SMTP", FakeSMTP)
        await MailCatcherSender(host="mailpit", port=1025).send(
            to="user@example.com", subject="Your account is ready", body="Pw: x7#Q"
        )
        message = sent["message"]
        assert sent["address"] == ("mailpit", 1025)
        assert message["To"] == "user@example.com"
        assert message["Subject"] == "Your account is ready"
        assert "Pw: x7#Q" in message.get_content()

    @pytest.mark.parametrize("environment", ["staging", "production"])
    def test_settings_refuse_it_outside_development(self, environment: str) -> None:
        with pytest.raises(ValidationError, match="development only"):
            Settings(
                environment=environment,
                dev_smtp_host="mailpit",
                rate_limit_storage_uri="redis://redis:6379",
            )

    def test_a_blank_value_switches_it_off(self) -> None:
        assert Settings(dev_smtp_host="").dev_smtp_host is None
        assert Settings(dev_smtp_host="mailpit").dev_smtp_host == "mailpit"


class TestConsoleSender:
    async def test_logs_rather_than_sends(self, caplog) -> None:
        with caplog.at_level(logging.INFO, logger="cbpupsis_core.emails.sender"):
            await ConsoleEmailSender().send(
                to="someone@example.com", subject="Hi", body="Body text"
            )
        assert "someone@example.com" in caplog.text
        assert "Body text" not in caplog.text


class TestSendEmailNeverRaises:
    async def test_a_failing_sender_returns_false(self, monkeypatch, caplog) -> None:
        """A user who registered but whose email bounced must still exist."""

        class ExplodingSender:
            async def send(self, *, to: str, subject: str, body: str) -> None:
                raise RuntimeError("Resend is down")

        monkeypatch.setattr(
            "cbpupsis_core.emails.sender.get_email_sender", lambda: ExplodingSender()
        )

        with caplog.at_level(logging.ERROR, logger="cbpupsis_core.emails.sender"):
            result = await send_email(to="a@example.com", subject="S", body="B")

        assert result is False
        assert "Failed to send email" in caplog.text

    async def test_a_working_sender_returns_true(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "cbpupsis_core.emails.sender.get_email_sender", lambda: ConsoleEmailSender()
        )
        assert await send_email(to="a@example.com", subject="S", body="B") is True

    async def test_the_body_is_not_logged_by_send_email(
        self, monkeypatch, caplog
    ) -> None:
        """The body carries the one-time token, whose whole value is that it
        appears nowhere but the inbox."""

        class ExplodingSender:
            async def send(self, *, to: str, subject: str, body: str) -> None:
                raise RuntimeError("Resend is down")

        monkeypatch.setattr(
            "cbpupsis_core.emails.sender.get_email_sender", lambda: ExplodingSender()
        )
        with caplog.at_level(logging.ERROR, logger="cbpupsis_core.emails.sender"):
            await send_email(to="a@example.com", subject="S", body="SECRET-TOKEN-VALUE")
        assert "SECRET-TOKEN-VALUE" not in caplog.text


class TestResendDelivery:
    @staticmethod
    def _sender(handler) -> ResendEmailSender:
        return ResendEmailSender(
            api_key="re_test_key",
            from_email="CBPUPSIS <no-reply@example.com>",
            transport=httpx.MockTransport(handler),
        )

    async def test_posts_the_expected_payload(self) -> None:
        captured: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["auth"] = request.headers["Authorization"]
            captured["json"] = __import__("json").loads(request.content)
            return httpx.Response(200, json={"id": "email-1"})

        await self._sender(handler).send(
            to="user@example.com", subject="Subject", body="Body"
        )

        assert captured["url"] == "https://api.resend.com/emails"
        assert captured["auth"] == "Bearer re_test_key"
        assert captured["json"] == {
            "from": "CBPUPSIS <no-reply@example.com>",
            "to": ["user@example.com"],
            "subject": "Subject",
            "text": "Body",
        }

    async def test_a_refusal_raises_with_status_and_name_only(self) -> None:
        """Resend's message can echo the request, which carries the one-time link."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                422,
                json={
                    "statusCode": 422,
                    "name": "validation_error",
                    "message": "Invalid body: SECRET-TOKEN-VALUE",
                },
            )

        with pytest.raises(EmailDeliveryError) as raised:
            await self._sender(handler).send(to="a@example.com", subject="S", body="B")
        assert raised.value.status == 422
        assert raised.value.name == "validation_error"
        assert "SECRET-TOKEN-VALUE" not in str(raised.value)

    async def test_a_non_json_error_still_raises(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(502, text="Bad Gateway")

        with pytest.raises(EmailDeliveryError) as raised:
            await self._sender(handler).send(to="a@example.com", subject="S", body="B")
        assert raised.value.status == 502
        assert raised.value.name is None

    async def test_send_email_reports_a_refusal_as_false(self, monkeypatch) -> None:
        """send_email never raises; the outbox turns False into a retry."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(429, json={"name": "rate_limit_exceeded"})

        monkeypatch.setattr(
            "cbpupsis_core.emails.sender.get_email_sender",
            lambda: self._sender(handler),
        )
        assert await send_email(to="a@example.com", subject="S", body="B") is False


class TestEmailFailureDoesNotBreakTheRequest:
    async def test_registration_succeeds_when_email_delivery_fails(
        self, console_account, client: AsyncClient, db: AsyncSession, monkeypatch
    ) -> None:
        """The headline rule: a bounced verification email must not roll back a
        signup, or a delivery problem becomes a data problem."""

        class ExplodingSender:
            async def send(self, *, to: str, subject: str, body: str) -> None:
                raise RuntimeError("Resend is down")

        monkeypatch.setattr(
            "cbpupsis_core.emails.sender.get_email_sender", lambda: ExplodingSender()
        )

        response = await console_account("bounce@example.com", "a-long-enough-password")

        # The account exists and is usable. Verification never arrived, since
        # the send blew up, so mark it here: the property under test is that the
        # signup survived a delivery failure, not that login skips the gate.
        await users_service.mark_email_verified(db, uuid.UUID(response["id"]))

        login = await client.post(
            "/api/v1/auth/login",
            json={"email": "bounce@example.com", "password": "a-long-enough-password"},
        )
        assert login.status_code == 200

    async def test_forgot_password_accepts_when_delivery_fails(
        self,
        client: AsyncClient,
        registered_user: dict[str, str],
        monkeypatch,
        drain_outbox,
    ) -> None:
        class ExplodingSender:
            async def send(self, *, to: str, subject: str, body: str) -> None:
                raise RuntimeError("Resend is down")

        monkeypatch.setattr(
            "cbpupsis_core.emails.sender.get_email_sender", lambda: ExplodingSender()
        )

        response = await client.post(
            "/api/v1/auth/forgot-password", json={"email": registered_user["email"]}
        )
        assert response.status_code == 202
        await drain_outbox()


class TestTemplates:
    def test_verification_link_points_at_the_frontend(self) -> None:
        """A human clicks these; the API has no page to render."""
        from cbpupsis_core import emails as email_templates

        subject, body = email_templates.verification_email("abc123", role="student")
        assert subject
        assert "/verify-email?token=abc123" in body

    def test_reset_link_points_at_the_frontend(self) -> None:
        from cbpupsis_core import emails as email_templates

        _subject, body = email_templates.password_reset_email("xyz789", role="student")
        assert "/reset-password?token=xyz789" in body

    def test_tokens_are_url_escaped(self) -> None:
        from cbpupsis_core import emails as email_templates

        _subject, body = email_templates.verification_email("a+b/c=d", role="student")
        assert "a+b/c=d" not in body
        assert "a%2Bb%2Fc%3Dd" in body

    def test_password_changed_notice_carries_no_token(self) -> None:
        from cbpupsis_core import emails as email_templates

        _subject, body = email_templates.password_changed_email()
        assert "token=" not in body
