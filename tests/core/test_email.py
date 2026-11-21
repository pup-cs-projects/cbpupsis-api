"""Tests for the email layer.

Three properties matter more than delivery itself, because each one breaks
something outside email when it is wrong: the console fallback (so tests and dev
never need AWS credentials), the failure isolation (so a bounce cannot undo a
signup), and the off-thread SES call (so a slow SES cannot stall the event loop).
"""

from __future__ import annotations

import asyncio
import logging
import uuid

import pytest
from httpx import AsyncClient
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_core.config import Settings
from cbpupsis_core.emails import (
    ConsoleEmailSender,
    EmailSender,
    MailCatcherSender,
    SESEmailSender,
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


class TestSenderSelection:
    def test_console_backend_when_ses_is_unconfigured(self, monkeypatch) -> None:
        """The default in development and tests: no credentials, no real mail."""
        monkeypatch.setattr("cbpupsis_core.emails.sender.settings.ses_from_email", None)
        assert isinstance(get_email_sender(), ConsoleEmailSender)

    def test_ses_backend_when_configured(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "cbpupsis_core.emails.sender.settings.ses_from_email",
            "no-reply@example.com",
        )
        monkeypatch.setattr(
            "cbpupsis_core.emails.sender.settings.aws_region", "us-east-1"
        )

        constructed: dict[str, object] = {}

        class FakeBoto3:
            @staticmethod
            def client(service: str, region_name: str):
                constructed["service"] = service
                constructed["region"] = region_name
                return object()

        monkeypatch.setitem(__import__("sys").modules, "boto3", FakeBoto3)

        sender = get_email_sender()
        assert isinstance(sender, SESEmailSender)
        assert constructed == {"service": "ses", "region": "us-east-1"}

    def test_the_sender_is_cached(self, monkeypatch) -> None:
        """boto3 client construction resolves credentials and loads service
        models — far too expensive to repeat per message."""
        monkeypatch.setattr("cbpupsis_core.emails.sender.settings.ses_from_email", None)
        assert get_email_sender() is get_email_sender()

    def test_both_backends_satisfy_the_protocol(self) -> None:
        assert isinstance(ConsoleEmailSender(), EmailSender)
        assert isinstance(MailCatcherSender(host="mailpit", port=1025), EmailSender)

    def test_the_mail_catcher_wins_over_ses(self, monkeypatch) -> None:
        """Development with a catcher must never reach a real inbox."""
        monkeypatch.setattr(
            "cbpupsis_core.emails.sender.settings.ses_from_email",
            "no-reply@example.com",
        )
        monkeypatch.setattr(
            "cbpupsis_core.emails.sender.settings.dev_smtp_host", "mailpit"
        )
        assert isinstance(get_email_sender(), MailCatcherSender)


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
                raise RuntimeError("SES is down")

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
                raise RuntimeError("SES is down")

        monkeypatch.setattr(
            "cbpupsis_core.emails.sender.get_email_sender", lambda: ExplodingSender()
        )
        with caplog.at_level(logging.ERROR, logger="cbpupsis_core.emails.sender"):
            await send_email(to="a@example.com", subject="S", body="SECRET-TOKEN-VALUE")
        assert "SECRET-TOKEN-VALUE" not in caplog.text


class TestSESDoesNotBlockTheEventLoop:
    async def test_the_blocking_call_runs_off_the_loop(self, monkeypatch) -> None:
        """boto3 is synchronous. Called directly from an async endpoint it would
        block every other request in the process for the whole SES round-trip,
        so it must go through asyncio.to_thread."""
        loop_thread = asyncio.get_running_loop().run_in_executor
        assert loop_thread is not None

        import threading

        calling_thread: dict[str, int] = {}

        class FakeClient:
            def send_email(self, **kwargs):
                calling_thread["id"] = threading.get_ident()

        class FakeBoto3:
            @staticmethod
            def client(service: str, region_name: str):
                return FakeClient()

        monkeypatch.setitem(__import__("sys").modules, "boto3", FakeBoto3)

        sender = SESEmailSender(from_email="a@example.com", region="us-east-1")
        await sender.send(to="b@example.com", subject="S", body="B")

        assert calling_thread["id"] != threading.get_ident(), (
            "SES was called on the event loop thread; it must run via asyncio.to_thread"
        )

    async def test_passes_the_expected_ses_payload(self, monkeypatch) -> None:
        captured: dict[str, object] = {}

        class FakeClient:
            def send_email(self, **kwargs):
                captured.update(kwargs)

        class FakeBoto3:
            @staticmethod
            def client(service: str, region_name: str):
                return FakeClient()

        monkeypatch.setitem(__import__("sys").modules, "boto3", FakeBoto3)

        sender = SESEmailSender(
            from_email="no-reply@example.com",
            region="us-east-1",
            configuration_set="my-config-set",
        )
        await sender.send(to="user@example.com", subject="Subject", body="Body")

        assert captured["Source"] == "no-reply@example.com"
        assert captured["Destination"] == {"ToAddresses": ["user@example.com"]}
        assert captured["ConfigurationSetName"] == "my-config-set"
        assert captured["Message"]["Subject"]["Data"] == "Subject"
        assert captured["Message"]["Body"]["Text"]["Data"] == "Body"

    async def test_configuration_set_is_omitted_when_unset(self, monkeypatch) -> None:
        """SES rejects an empty ConfigurationSetName, so the key must be absent
        rather than present-and-None."""
        captured: dict[str, object] = {}

        class FakeClient:
            def send_email(self, **kwargs):
                captured.update(kwargs)

        class FakeBoto3:
            @staticmethod
            def client(service: str, region_name: str):
                return FakeClient()

        monkeypatch.setitem(__import__("sys").modules, "boto3", FakeBoto3)

        sender = SESEmailSender(from_email="no-reply@example.com", region="us-east-1")
        await sender.send(to="user@example.com", subject="S", body="B")

        assert "ConfigurationSetName" not in captured


class TestEmailFailureDoesNotBreakTheRequest:
    async def test_registration_succeeds_when_email_delivery_fails(
        self, console_account, client: AsyncClient, db: AsyncSession, monkeypatch
    ) -> None:
        """The headline rule: a bounced verification email must not roll back a
        signup, or a delivery problem becomes a data problem."""

        class ExplodingSender:
            async def send(self, *, to: str, subject: str, body: str) -> None:
                raise RuntimeError("SES is down")

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
                raise RuntimeError("SES is down")

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
