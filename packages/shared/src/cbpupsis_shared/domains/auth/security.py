"""Password hashing and JWT issue/verify — the only module that knows crypto.

Two deliberate choices here:

- **bcrypt for passwords.** Cost 12 or higher, with legacy Argon2 verification.
- **Typed, short-lived tokens.** Access tokens expire quickly and carry only
  identity — never permissions, which must stay revocable (see
  ``cbpupsis_shared.domains.iam.service.get_effective_permissions``). Refresh tokens are
  long-lived, rotated on every use, and tagged with a ``type`` claim so one kind
  can never be replayed as the other.

Each token carries a ``jti`` (JWT ID). For refresh tokens that id is recorded in
the database, which is what makes a single token revocable — see
``cbpupsis_shared.domains.auth.service``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import bcrypt
import jwt
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from passlib.context import CryptContext
from passlib.exc import UnknownHashError

from cbpupsis_core.config import settings
from cbpupsis_shared.domains.auth.constants import (
    BCRYPT_MAX_PASSWORD_BYTES,
    PASSWORD_MAX_LENGTH,
    RESET_PASSWORD_MIN_LENGTH,
)
from cbpupsis_shared.domains.auth.exceptions import (
    InvalidAuthTokenError,
    PasswordTooWeakError,
    SessionExpiredError,
)

TokenType = Literal["access", "refresh"]

_pwd_context = CryptContext(schemes=["argon2"], deprecated="auto")


def check_password_complexity(raw_password: str) -> list[str]:
    """Return a list of unmet complexity rule names for raw_password.

    Requires:
    - Minimum length (RESET_PASSWORD_MIN_LENGTH)
    - At least one uppercase letter
    - At least one lowercase letter
    - At least one digit
    - At least one symbol
    """
    unmet: list[str] = []
    if len(raw_password) < RESET_PASSWORD_MIN_LENGTH:
        unmet.append(f"at least {RESET_PASSWORD_MIN_LENGTH} characters")
    if not any(c.isupper() for c in raw_password):
        unmet.append("uppercase")
    if not any(c.islower() for c in raw_password):
        unmet.append("lowercase")
    if not any(c.isdigit() for c in raw_password):
        unmet.append("digit")
    if not any(not c.isalnum() and not c.isspace() for c in raw_password):
        unmet.append("symbol")
    if len(raw_password) > PASSWORD_MAX_LENGTH:
        unmet.append(f"at most {PASSWORD_MAX_LENGTH} characters")
    if len(raw_password.encode("utf-8")) > BCRYPT_MAX_PASSWORD_BYTES:
        unmet.append(f"at most {BCRYPT_MAX_PASSWORD_BYTES} UTF-8 bytes")
    return unmet


def hash_password(raw_password: str) -> str:
    """Return a bcrypt digest; reject oversize inputs rather than truncating."""
    encoded = raw_password.encode("utf-8")
    if len(encoded) > BCRYPT_MAX_PASSWORD_BYTES:
        raise PasswordTooWeakError([f"at most {BCRYPT_MAX_PASSWORD_BYTES} UTF-8 bytes"])
    return bcrypt.hashpw(
        encoded, bcrypt.gensalt(rounds=settings.bcrypt_rounds)
    ).decode()


def verify_password(raw_password: str, password_hash: str) -> bool:
    """Return whether ``raw_password`` matches ``password_hash``.

    A stored value that is not a recognisable hash returns ``False`` rather than
    raising. Deleted accounts deliberately hold an unusable marker instead of a
    real digest (see ``users.service.soft_delete``), and passlib raises
    :class:`~passlib.exc.UnknownHashError` on anything it cannot parse — which
    would turn an ordinary failed login against a deleted account into an
    unhandled 500, and a 500 where every other account gives 401 is exactly the
    kind of difference that identifies deleted accounts to an attacker.
    """
    try:
        if password_hash.startswith(("$2a$", "$2b$", "$2y$")):
            encoded = raw_password.encode("utf-8")
            return len(encoded) <= BCRYPT_MAX_PASSWORD_BYTES and bcrypt.checkpw(
                encoded, password_hash.encode("ascii")
            )
        return _pwd_context.verify(raw_password, password_hash)
    except (UnknownHashError, ValueError, UnicodeError):
        return False


def _encode(
    subject: uuid.UUID,
    token_type: TokenType,
    ttl: timedelta,
    claims: dict[str, Any] | None = None,
) -> tuple[str, str, datetime]:
    """Encode a signed JWT.

    Returns the token, its ``jti``, and its expiry so callers can persist the
    id of a refresh token without decoding what they just created.
    """
    now = datetime.now(UTC)
    expires_at = now + ttl
    jti = str(uuid.uuid4())
    payload: dict[str, Any] = {
        "sub": str(subject),
        # Distinguishes the two token kinds so a refresh token presented as a
        # bearer credential is rejected rather than granting API access.
        "type": token_type,
        "iat": now,
        "exp": expires_at,
        "jti": jti,
    }
    payload.update(claims or {})
    token = jwt.encode(
        payload,
        settings.jwt_secret.get_secret_value(),
        algorithm=settings.jwt_algorithm,
    )
    return token, jti, expires_at


def create_access_token(
    user_id: uuid.UUID, *, claims: dict[str, Any] | None = None
) -> str:
    """Issue a short-lived access token for ``user_id``."""
    token, _, _ = _encode(
        user_id,
        "access",
        timedelta(minutes=settings.access_token_ttl_minutes),
        claims,
    )
    return token


def create_refresh_token(
    user_id: uuid.UUID, *, claims: dict[str, Any] | None = None
) -> tuple[str, str, datetime]:
    """Issue a refresh token, returning it with its ``jti`` and expiry.

    The caller persists the ``jti`` so the token can be revoked on rotation.
    """
    return _encode(
        user_id,
        "refresh",
        timedelta(days=settings.refresh_token_ttl_days),
        claims,
    )


def decode_token(token: str, expected_type: TokenType) -> dict[str, Any]:
    """Decode and validate a token, returning its claims.

    Every failure — bad signature, expiry, or a token of the wrong type — raises
    :class:`UnauthorizedError` with an identical message, so a caller cannot
    probe which specific check failed.
    """
    try:
        claims: dict[str, Any] = jwt.decode(
            token,
            settings.jwt_secret.get_secret_value(),
            algorithms=[settings.jwt_algorithm],
        )
    except jwt.InvalidTokenError as exc:
        raise InvalidAuthTokenError from exc

    if claims.get("type") != expected_type:
        raise InvalidAuthTokenError
    return claims


def generate_one_time_token() -> tuple[str, str]:
    """Mint a mailed secret, returning ``(raw_token, token_hash)``.

    The raw value goes in the email; only the digest is persisted, so a dump of
    the database cannot be replayed against the verify or reset endpoints.

    ``token_urlsafe(32)`` is 256 bits from the OS CSPRNG — unguessable, and safe
    to paste into a URL without escaping. Never use ``random`` here: it is
    seeded predictably and its output is reconstructable from a few samples.
    """
    raw = secrets.token_urlsafe(32)
    return raw, hash_one_time_token(raw)


def hash_one_time_token(raw_token: str) -> str:
    """Return the SHA-256 hex digest used to look a one-time token up.

    A plain digest is correct for this input, unlike for a password: the token
    carries full entropy, so there is no dictionary to run against it and a slow
    KDF would only add latency to every verification attempt.
    """
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def require_session_version(claims: dict[str, Any], current_version: int) -> None:
    """Reject every token predating a committed credential reset."""
    if claims.get("session_version", 0) != current_version:
        raise SessionExpiredError


def reset_address_hash(email: str) -> str:
    """Key rate limits without persisting an enumerable email address."""
    return hmac.new(
        settings.jwt_secret.get_secret_value().encode(),
        b"reset-address:" + email.lower().encode(),
        hashlib.sha256,
    ).hexdigest()


def _reset_delivery_key() -> bytes:
    return hmac.new(
        settings.jwt_secret.get_secret_value().encode(),
        b"cbpupsis:reset-email:v1",
        hashlib.sha256,
    ).digest()


def encrypt_reset_token(raw_token: str, *, user_id: uuid.UUID, token_hash: str) -> str:
    """Protect the delivery copy; the redemption ledger stores only its digest."""
    nonce = secrets.token_bytes(12)
    aad = f"reset-email:v1:{user_id}:{token_hash}".encode()
    encrypted = AESGCM(_reset_delivery_key()).encrypt(nonce, raw_token.encode(), aad)
    return base64.urlsafe_b64encode(nonce + encrypted).decode()


def decrypt_reset_token(encrypted: str, *, user_id: uuid.UUID, token_hash: str) -> str:
    """Decrypt only for the recipient and digest the sender staged."""
    data = base64.urlsafe_b64decode(encrypted)
    aad = f"reset-email:v1:{user_id}:{token_hash}".encode()
    return AESGCM(_reset_delivery_key()).decrypt(data[:12], data[12:], aad).decode()
