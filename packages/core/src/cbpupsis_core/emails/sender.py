"""Outbound email: an ``EmailSender`` protocol with SES, console, and mail-catcher
backends.

Three decisions worth stating, because each prevents a specific production bug:

- **Sending never fails the request.** :func:`send_email` catches and logs. A user
  who registered but whose address bounced must still exist; rolling back their
  signup because SES was briefly unavailable turns a delivery problem into a data
  problem.
- **boto3 is synchronous**, so every call is pushed onto a worker thread with
  ``asyncio.to_thread``. Calling it directly from an async endpoint would block
  the event loop for the whole round-trip to SES, stalling every other request
  the process is serving.
- **No sender identity configured means the console backend.** Development and
  tests therefore need no AWS credentials and cannot email a real person by
  accident — the failure mode of a shared staging database pointed at real SES.
- **A development mail catcher wins over both.** With ``DEV_SMTP_HOST`` set
  (development only, enforced by settings), every message goes to Mailpit,
  where a developer reads it whole. The console backend never prints bodies,
  so this is the one place a local temporary password or link can be read.

The message bodies live in :mod:`cbpupsis_core.emails.templates` so this module stays
about transport.
"""

from __future__ import annotations

import asyncio
import logging
import smtplib
from email.message import EmailMessage
from functools import lru_cache
from typing import Protocol, runtime_checkable

from cbpupsis_core.config import settings

logger = logging.getLogger(__name__)

#: The sender address on caught development mail. Never a deliverable domain.
DEV_FROM_EMAIL = "no-reply@cbpupsis.localhost"


@runtime_checkable
class EmailSender(Protocol):
    """Transport for one outbound message.

    Implementations must be safe to call from an async context — anything doing
    blocking I/O is responsible for moving it off the event loop itself.
    """

    async def send(self, *, to: str, subject: str, body: str) -> None:
        """Deliver one plain-text message to a single recipient."""
        ...


class ConsoleEmailSender:
    """Development delivery stub; never log bodies containing credentials."""

    async def send(self, *, to: str, subject: str, body: str) -> None:
        """Record transport metadata without the password-reset link."""
        logger.info("Email (console backend, not sent) to=%s subject=%s", to, subject)


class MailCatcherSender:
    """Deliver to a local SMTP mail catcher (Mailpit) for reading in a browser.

    Plain SMTP without TLS or credentials: the catcher runs beside the app on
    the compose network, and settings refuse this backend outside development.
    """

    def __init__(self, *, host: str, port: int) -> None:
        self._host = host
        self._port = port

    def send_blocking(self, to: str, subject: str, body: str) -> None:
        """Blocking SMTP exchange, run on a worker thread by :meth:`send`."""
        message = EmailMessage()
        message["From"] = DEV_FROM_EMAIL
        message["To"] = to
        message["Subject"] = subject
        message.set_content(body)
        with smtplib.SMTP(self._host, self._port, timeout=10) as client:
            client.send_message(message)

    async def send(self, *, to: str, subject: str, body: str) -> None:
        await asyncio.to_thread(self.send_blocking, to, subject, body)


class SESEmailSender:
    """Send through AWS SES using boto3.

    The client is built once and reused: boto3 client construction resolves
    credentials and loads service models, which is far too expensive to repeat
    per message.
    """

    def __init__(
        self,
        *,
        from_email: str,
        region: str,
        configuration_set: str | None = None,
    ) -> None:
        # Imported lazily so the dependency is only required when SES is
        # actually configured — tests and local runs never import boto3.
        import boto3  # noqa: PLC0415 - see the comment above

        self._client = boto3.client("ses", region_name=region)
        self._from_email = from_email
        self._configuration_set = configuration_set

    def send_blocking(self, to: str, subject: str, body: str) -> None:
        """Blocking SES call, run on a worker thread by :meth:`send`."""
        kwargs = {
            "Source": self._from_email,
            "Destination": {"ToAddresses": [to]},
            "Message": {
                "Subject": {"Data": subject, "Charset": "UTF-8"},
                "Body": {"Text": {"Data": body, "Charset": "UTF-8"}},
            },
        }
        if self._configuration_set is not None:
            kwargs["ConfigurationSetName"] = self._configuration_set
        self._client.send_email(**kwargs)

    async def send(self, *, to: str, subject: str, body: str) -> None:
        """Deliver via SES without blocking the event loop."""
        await asyncio.to_thread(self.send_blocking, to, subject, body)


@lru_cache
def get_email_sender() -> EmailSender:
    """Return the configured sender, built once per process.

    A development mail catcher, when configured, wins over everything; then SES;
    then the console backend whenever no verified SES identity is set.
    """
    if settings.dev_smtp_host is not None:
        logger.info(
            "Using the development mail catcher at %s:%s",
            settings.dev_smtp_host,
            settings.dev_smtp_port,
        )
        return MailCatcherSender(
            host=settings.dev_smtp_host, port=settings.dev_smtp_port
        )
    if not settings.email_enabled:
        logger.info("No ses_from_email configured; using the console email backend")
        return ConsoleEmailSender()

    # Narrowed by settings.email_enabled, which is exactly this check, so this
    # only informs the type checker and is never a runtime guard.
    assert settings.ses_from_email is not None  # nosec B101
    return SESEmailSender(
        from_email=settings.ses_from_email,
        region=settings.aws_region,
        configuration_set=settings.ses_configuration_set,
    )


async def send_email(*, to: str, subject: str, body: str) -> bool:
    """Best-effort send. Returns whether it succeeded; never raises.

    Callers deliberately cannot distinguish *why* a send failed, because there
    is no useful branch on it: the account was still created, the reset token
    was still issued, and surfacing the difference to an HTTP caller would leak
    whether the address exists. The exception is logged in full for operators.

    The recipient is logged; the body is not, because it carries the one-time
    token whose whole value is that it appears nowhere but the inbox.
    """
    sender = get_email_sender()
    try:
        await sender.send(to=to, subject=subject, body=body)
    except Exception as exc:
        # A provider exception can echo the secret-bearing message.
        logger.error(
            "Failed to send email to=%s subject=%s error_type=%s",
            to,
            subject,
            type(exc).__name__,
        )
        return False
    return True
