"""Outbound email: an ``EmailSender`` protocol with Resend, console, and
mail-catcher backends.

Three decisions worth stating, because each prevents a specific production bug:

- **Sending never fails the request.** :func:`send_email` catches and logs. A user
  who registered but whose address bounced must still exist; rolling back their
  signup because Resend was briefly unavailable turns a delivery problem into a
  data problem.
- **Resend is called with an async HTTP client**, so a send never blocks the
  event loop for the round-trip, stalling every other request the process is
  serving. The SMTP mail catcher is synchronous and runs on a worker thread.
- **No sender and API key configured means the console backend.** Development
  and tests therefore need no key and cannot email a real person by accident —
  the failure mode of a shared staging database pointed at a real provider.
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

import httpx

from cbpupsis_core.config import settings

logger = logging.getLogger(__name__)

#: The sender address on caught development mail. Never a deliverable domain.
DEV_FROM_EMAIL = "no-reply@cbpupsis.localhost"

#: Resend's send endpoint (https://resend.com/docs/api-reference/emails/send-email).
RESEND_URL = "https://api.resend.com/emails"
#: Long enough for a slow response, short enough that a hung call does not hold
#: a request (or a worker batch) for long. The outbox retries a failed send.
RESEND_TIMEOUT_SECONDS = 10.0


class EmailDeliveryError(Exception):
    """The provider refused or failed a send.

    Carries the HTTP status and the provider's error name only: its message can
    echo the request, and the request carries the one-time link.
    """

    def __init__(self, status: int, name: str | None) -> None:
        super().__init__(f"HTTP {status} {name or ''}".strip())
        self.status = status
        self.name = name


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


class ResendEmailSender:
    """Send through Resend's HTTP API.

    A client per send rather than one per process: the API, the outbox worker,
    and tests each run their own event loop, and an ``httpx.AsyncClient`` is
    bound to the loop it was first used on. Mail volume is far too low for the
    extra connection to matter.
    """

    def __init__(
        self,
        *,
        api_key: str,
        from_email: str,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._api_key = api_key
        self._from_email = from_email
        # Tests pass a mock transport; production uses the network.
        self._transport = transport

    async def send(self, *, to: str, subject: str, body: str) -> None:
        """Deliver one plain-text message; raise EmailDeliveryError on refusal."""
        async with httpx.AsyncClient(
            transport=self._transport, timeout=RESEND_TIMEOUT_SECONDS
        ) as client:
            response = await client.post(
                RESEND_URL,
                headers={"Authorization": f"Bearer {self._api_key}"},
                json={
                    "from": self._from_email,
                    "to": [to],
                    "subject": subject,
                    "text": body,
                },
            )
        if response.is_success:
            return
        try:
            name = response.json().get("name")
        except ValueError:
            name = None
        raise EmailDeliveryError(
            response.status_code, name if isinstance(name, str) else None
        )


@lru_cache
def get_email_sender() -> EmailSender:
    """Return the configured sender, built once per process.

    A development mail catcher, when configured, wins over everything; then
    Resend; then the console backend whenever no sender and key are set.
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
        logger.info("No email_from configured; using the console email backend")
        return ConsoleEmailSender()

    # Narrowed by settings.email_enabled, which is exactly this check, so this
    # only informs the type checker and is never a runtime guard.
    assert settings.email_from is not None  # nosec B101
    assert settings.resend_api_key is not None  # nosec B101
    return ResendEmailSender(
        api_key=settings.resend_api_key.get_secret_value(),
        from_email=settings.email_from,
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
