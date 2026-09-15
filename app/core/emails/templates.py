"""Bodies for the transactional emails this application sends.

Kept apart from :mod:`app.core.emails.sender` so that module stays about transport, and
so changing wording never risks touching send logic. Plain text only: it renders
everywhere, cannot carry a tracking pixel, and needs no sanitising.

Every link points at the *frontend*, not the API. A human clicks these, and the
page behind them collects the token and POSTs it to the API — a link that hit the
API directly would have to be a GET, and a GET that consumes a one-time token is
triggered by any mail client that prefetches links.

Tokens are escaped with ``safe=""`` so that ``/``, ``+`` and ``=`` are encoded
too. ``token_urlsafe`` does not emit those today, but relying on that couples
these templates to the generator's alphabet: if it ever changes, an unescaped
character would silently truncate the token at the receiving end.
"""

from __future__ import annotations

from urllib.parse import quote

from app.config import settings

VERIFICATION_SUBJECT = "Verify your email address"
PASSWORD_RESET_SUBJECT = "Reset your password"
PASSWORD_CHANGED_SUBJECT = "Your password was changed"


def verification_email(token: str) -> tuple[str, str]:
    """Return (subject, body) for an email-verification message."""
    link = f"{settings.frontend_base_url}/verify-email?token={quote(token, safe='')}"
    hours = settings.email_verification_ttl_hours
    body = (
        f"Welcome to {settings.app_name}.\n\n"
        f"Confirm your email address by opening this link:\n\n"
        f"{link}\n\n"
        f"The link expires in {hours} hours and can only be used once.\n\n"
        f"If you did not create an account, you can ignore this message."
    )
    return VERIFICATION_SUBJECT, body


def password_reset_email(token: str) -> tuple[str, str]:
    """Return (subject, body) for a password-reset message."""
    link = f"{settings.frontend_base_url}/reset-password?token={quote(token, safe='')}"
    minutes = settings.password_reset_ttl_minutes
    body = (
        f"We received a request to reset your {settings.app_name} password.\n\n"
        f"Choose a new password here:\n\n"
        f"{link}\n\n"
        f"The link expires in {minutes} minutes and can only be used once.\n\n"
        f"If you did not request this, no action is needed — your password has "
        f"not changed."
    )
    return PASSWORD_RESET_SUBJECT, body


def password_changed_email() -> tuple[str, str]:
    """Return (subject, body) for the notice sent after a password change.

    Carries no link or token: its only job is to tell the account owner that a
    change happened, which is how a victim finds out about an account takeover
    they did not perform.
    """
    body = (
        f"Your {settings.app_name} password was just changed.\n\n"
        f"If this was you, nothing further is needed. If it was not, contact "
        f"support immediately — someone else may have access to your account."
    )
    return PASSWORD_CHANGED_SUBJECT, body
