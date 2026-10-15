"""Errors the auth domain raises.

See ``cbpupsis_api_student.domains.items.exceptions`` for why each domain
owns its errors and why they share a base class.

**Several of these are deliberately identical to the caller.** Invalid
credentials, an unknown account, and a disabled one all produce the same 401,
because a response that varied would let anyone test an email list against the
user base. Naming them separately here keeps that indistinguishability *at the
boundary* while the code still says which case it is — the message is shared, the
class is not, so a future edit cannot make one of them more "helpful" by
accident.
"""

from __future__ import annotations

from cbpupsis_core.exceptions import (
    AppError,
    ConflictError,
    EmailNotVerifiedError,
    UnauthorizedError,
)
from cbpupsis_shared.domains.auth.constants import (
    INVALID_CREDENTIALS_MESSAGE,
    INVALID_TOKEN_MESSAGE,
    RESET_TOKEN_EXPIRED_MESSAGE,
    RESET_TOKEN_USED_MESSAGE,
)


class AuthError(AppError):
    """Base for every error raised by the auth domain."""


class InvalidCredentialsError(AuthError, UnauthorizedError):
    """Login failed (HTTP 401).

    Raised identically for an unknown address, a wrong password, and an inactive
    or deleted account. Do not add a variant that distinguishes them.
    """

    def __init__(self) -> None:
        super().__init__(INVALID_CREDENTIALS_MESSAGE)


class InvalidTokenError(AuthError, UnauthorizedError):
    """A one-time token is unknown, expired, already used, or was issued for a
    different purpose (HTTP 401).

    One message for all four, so the response cannot be used to probe which.
    """

    def __init__(self) -> None:
        super().__init__(INVALID_TOKEN_MESSAGE)


class InvalidAuthTokenError(AuthError, UnauthorizedError):
    """A JWT failed signature, expiry, or token-type validation (HTTP 401)."""

    def __init__(self) -> None:
        super().__init__("Invalid authentication token")


class NotAuthenticatedError(AuthError, UnauthorizedError):
    """No credentials were supplied (HTTP 401).

    Distinct from a *rejected* credential: 401 rather than the 403 Starlette
    raises by default for a missing header, because "who are you?" and "you may
    not" are different answers and clients act on them differently.
    """

    def __init__(self) -> None:
        super().__init__("Not authenticated")


class InactiveUserError(AuthError, UnauthorizedError):
    """The account was deactivated after the token was issued (HTTP 401).

    Checked per request rather than trusted from the token, so a deactivation
    takes effect immediately instead of when the access token expires.
    """

    def __init__(self) -> None:
        super().__init__("User is no longer active")


class RefreshTokenReusedError(AuthError, UnauthorizedError):
    """A rotated refresh token was presented again (HTTP 401).

    Treated as evidence the token leaked: the service revokes the user's entire
    token family in response, so a replay costs the attacker the session too.
    """

    def __init__(self) -> None:
        super().__init__("Refresh token already used")


class IncorrectCurrentPasswordError(AuthError, UnauthorizedError):
    """The current password supplied to a password change did not match
    (HTTP 401)."""

    def __init__(self) -> None:
        super().__init__("Current password is incorrect")


class EmailAlreadyRegisteredError(AuthError, ConflictError):
    """Registration hit the unique constraint on the email column (HTTP 409).

    Raised from the ``IntegrityError``, not from a prior SELECT: checking first
    leaves a race between the check and the insert under concurrent signups.
    """

    def __init__(self) -> None:
        super().__init__("Email already registered")


class UnverifiedEmailError(AuthError, EmailNotVerifiedError):
    """The password was correct but the address is unverified (HTTP 403).

    Inherits ``code = "email_not_verified"`` — the one auth failure a client must
    branch on rather than display, routing to a resend-verification screen.

    Only ever raised *after* the password check passes, which is what keeps it
    from being an enumeration oracle.
    """

    def __init__(self, detail: str) -> None:
        super().__init__(detail)


class ResetTokenExpiredError(AuthError):
    """The password reset link is older than the configured TTL (HTTP 410)."""

    status_code = 410
    code = "AUTH_RESET_TOKEN_EXPIRED"

    def __init__(self) -> None:
        super().__init__(RESET_TOKEN_EXPIRED_MESSAGE)


class ResetTokenUsedError(AuthError):
    """The password reset link has already been consumed (HTTP 410)."""

    status_code = 410
    code = "AUTH_RESET_TOKEN_USED"

    def __init__(self) -> None:
        super().__init__(RESET_TOKEN_USED_MESSAGE)


class PasswordTooWeakError(AuthError):
    """The new password does not satisfy complexity requirements (HTTP 422)."""

    status_code = 422
    code = "AUTH_PASSWORD_TOO_WEAK"

    def __init__(self, unmet_rules: list[str]) -> None:
        rules_str = ", ".join(unmet_rules)
        super().__init__(
            f"Password does not meet complexity requirements: {rules_str}",
            response_fields={"unmet_rules": unmet_rules},
        )


class SessionExpiredError(AuthError, UnauthorizedError):
    """The account's session generation changed (HTTP 401)."""

    code = "AUTH_SESSION_EXPIRED"

    def __init__(self) -> None:
        super().__init__("Your session has ended. Sign in again.")


class InvalidEmailAddressError(AuthError):
    """Malformed email, independent of account existence (HTTP 400)."""

    code = "INVALID_EMAIL_ADDRESS"

    def __init__(self) -> None:
        super().__init__("Enter a valid email address.")


class ResetRateLimitError(AuthError):
    """A bounded address allowance is exhausted without locking the account."""

    status_code = 429
    code = "RATE_LIMIT_EXCEEDED"

    def __init__(self, retry_after: int) -> None:
        super().__init__(
            "Too many requests. Try again later or use a link already sent to you.",
            headers={"Retry-After": str(retry_after)},
        )
