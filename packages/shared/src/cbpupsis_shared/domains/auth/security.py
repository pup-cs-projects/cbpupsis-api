"""Password hashing and JWT issue/verify — the only module that knows crypto.

Two deliberate choices here:

- **Argon2 for passwords.** A slow, memory-hard KDF; the cost parameters make
  offline brute-forcing of a leaked hash expensive. Never a fast hash (MD5/SHA).
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

import hashlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import jwt
from passlib.context import CryptContext
from passlib.exc import UnknownHashError

from cbpupsis_core.config import settings
from cbpupsis_shared.domains.auth.exceptions import InvalidAuthTokenError

TokenType = Literal["access", "refresh"]

_pwd_context = CryptContext(schemes=["argon2"], deprecated="auto")


def hash_password(raw_password: str) -> str:
    """Return an argon2 hash of ``raw_password``, safe to store."""
    return _pwd_context.hash(raw_password)


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
        return _pwd_context.verify(raw_password, password_hash)
    except UnknownHashError:
        return False


def _encode(
    subject: uuid.UUID,
    token_type: TokenType,
    ttl: timedelta,
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
    token = jwt.encode(
        payload,
        settings.jwt_secret.get_secret_value(),
        algorithm=settings.jwt_algorithm,
    )
    return token, jti, expires_at


def create_access_token(user_id: uuid.UUID) -> str:
    """Issue a short-lived access token for ``user_id``."""
    token, _, _ = _encode(
        user_id, "access", timedelta(minutes=settings.access_token_ttl_minutes)
    )
    return token


def create_refresh_token(user_id: uuid.UUID) -> tuple[str, str, datetime]:
    """Issue a refresh token, returning it with its ``jti`` and expiry.

    The caller persists the ``jti`` so the token can be revoked on rotation.
    """
    return _encode(user_id, "refresh", timedelta(days=settings.refresh_token_ttl_days))


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
