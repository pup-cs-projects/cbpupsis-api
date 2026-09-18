"""Fixed values for the auth domain.

The two failure messages are constants because the security property is that
they are *identical* across call sites — two that drift apart become an oracle
telling an attacker which half of a credential was wrong.
"""

from __future__ import annotations

INVALID_TOKEN_MESSAGE = "Invalid or expired token"
INVALID_CREDENTIALS_MESSAGE = "Incorrect email or password"

PASSWORD_MIN_LENGTH = 12
PASSWORD_MAX_LENGTH = 128

#: SHA-256 hex digest of a mailed secret.
ONE_TIME_TOKEN_MAX_LENGTH = 64
TOKEN_HASH_LENGTH = 64
#: A UUID4 string.
JTI_LENGTH = 36
