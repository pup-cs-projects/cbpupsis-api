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
    EmailNotVerifiedError,
    ForbiddenError,
    NotFoundError,
    UnauthorizedError,
    ValidationError,
)
from cbpupsis_shared.domains.auth.constants import (
    INVALID_CREDENTIALS_MESSAGE,
    INVALID_TOKEN_MESSAGE,
    PASSWORD_CHANGE_REQUIRED_MESSAGE,
    RESET_TOKEN_EXPIRED_MESSAGE,
    RESET_TOKEN_USED_MESSAGE,
    RESOURCE_NOT_FOUND_MESSAGE,
    SESSION_ENDED_MESSAGE,
    TEMPORARY_PASSWORD_EXPIRED_MESSAGE,
)


class AuthError(AppError):
    """Base for every error raised by the auth domain."""


class SuperadminRoleRequiredError(AuthError, ForbiddenError):
    code = "SUPERADMIN_ROLE_REQUIRED"

    def __init__(self) -> None:
        super().__init__("A live MFA-completed Superadmin session is required")


class AuthSessionExpiredError(AuthError, UnauthorizedError):
    """The server-side session is missing or idle past the timeout (HTTP 401).

    Shares its code and message with :class:`SessionExpiredError`: the client
    does the same thing either way, while the class still says which happened.
    """

    code = "AUTH_SESSION_EXPIRED"

    def __init__(self) -> None:
        super().__init__(SESSION_ENDED_MESSAGE)


class IdentifierSignInFailedError(AuthError, UnauthorizedError):
    """An institutional-identifier sign-in failed (HTTP 401).

    Raised identically for an unknown identifier, a wrong password, a wrong
    birthdate, and an inactive account. Callers pass their role's message
    constant, never a message describing which check failed.
    """

    code = "AUTH_INVALID_CREDENTIALS"

    def __init__(self, message: str) -> None:
        super().__init__(message)


class IdentifierFormatInvalidError(AuthError, ValidationError):
    """An institutional identifier failed its format rule (HTTP 422).

    Raised before any lookup or password hash, so malformed input costs nothing.
    """

    code = "AUTH_ID_FORMAT_INVALID"

    def __init__(self, message: str) -> None:
        super().__init__(message)


class AccountLockedError(AuthError):
    """Too many failed sign-in steps in the rolling window (HTTP 423)."""

    status_code = 423
    code = "AUTH_ACCOUNT_LOCKED"

    def __init__(self, retry_after_seconds: int) -> None:
        retry_after = max(1, retry_after_seconds)
        super().__init__(
            "Account temporarily locked after repeated sign-in failures",
            response_fields={"retry_after_seconds": retry_after},
            headers={"Retry-After": str(retry_after)},
        )


class InvalidMfaError(AuthError, UnauthorizedError):
    code = "AUTH_MFA_INVALID"

    def __init__(self) -> None:
        super().__init__("The security code or hardware-key response is invalid")


class MfaCodeReusedError(AuthError, UnauthorizedError):
    code = "AUTH_MFA_CODE_REUSED"

    def __init__(self) -> None:
        super().__init__(
            "That security code has already been used; enter the current code"
        )


class MfaEnrollmentRequiredError(AuthError, ForbiddenError):
    code = "AUTH_MFA_ENROLLMENT_REQUIRED"

    def __init__(self) -> None:
        super().__init__("Set up multi-factor authentication to continue")


class MfaRequiredError(AuthError, UnauthorizedError):
    code = "AUTH_MFA_REQUIRED"

    def __init__(self) -> None:
        super().__init__("A second factor is required")


class InsufficientRoleError(AuthError, ForbiddenError):
    """The session is not a current MFA-completed session of the needed role."""

    code = "AUTH_INSUFFICIENT_ROLE"

    def __init__(self) -> None:
        super().__init__("This action is not available to your role")


class ResourceNotFoundError(AuthError, NotFoundError):
    """A record is absent or outside the caller's reach (HTTP 404).

    One class for every role's scope check, so "not yours" and "does not exist"
    can never drift into two different bodies.
    """

    code = "RESOURCE_NOT_FOUND"

    def __init__(self) -> None:
        super().__init__(RESOURCE_NOT_FOUND_MESSAGE)


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


class PasswordChangeRequiredError(AuthError, ForbiddenError):
    """The account still holds a system-issued temporary password (HTTP 403).

    Every authenticated route answers this until the user changes it; only the
    password change and the logout routes stay open.
    """

    code = "PASSWORD_CHANGE_REQUIRED"

    def __init__(self) -> None:
        super().__init__(PASSWORD_CHANGE_REQUIRED_MESSAGE)


class TemporaryPasswordExpiredError(AuthError, UnauthorizedError):
    """A correct temporary password used after its expiry (HTTP 401).

    Only raised once the password (and, for students, the birthdate) checked
    out, so it tells nothing to a caller who does not already hold it.
    """

    code = "AUTH_TEMPORARY_PASSWORD_EXPIRED"

    def __init__(self) -> None:
        super().__init__(TEMPORARY_PASSWORD_EXPIRED_MESSAGE)


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
        super().__init__(SESSION_ENDED_MESSAGE)


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
