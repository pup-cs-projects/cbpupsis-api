"""Fixed values for the auth domain.

The two failure messages are constants because the security property is that
they are *identical* across call sites — two that drift apart become an oracle
telling an attacker which half of a credential was wrong.
"""

from __future__ import annotations

INVALID_TOKEN_MESSAGE = "Invalid or expired token"
INVALID_CREDENTIALS_MESSAGE = "Incorrect email or password"

FORGOT_PASSWORD_SUCCESS_MESSAGE = (
    "If that address is registered, a reset link has been sent."
)
RESET_TOKEN_EXPIRED_MESSAGE = "The password reset link has expired; request a new one"
RESET_TOKEN_USED_MESSAGE = (
    "The password reset link has already been used; request a new one"
)

RESET_PASSWORD_MIN_LENGTH = 8
BCRYPT_MAX_PASSWORD_BYTES = 72
PASSWORD_MIN_LENGTH = 12
PASSWORD_MAX_LENGTH = 128

#: SHA-256 hex digest of a mailed secret.
ONE_TIME_TOKEN_MAX_LENGTH = 64
