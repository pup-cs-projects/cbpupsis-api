"""Password hashing and JWT issue/verify — the only module that knows crypto.

Two deliberate choices here:

- **Argon2 for passwords.** A slow, memory-hard KDF; the cost parameters make
  offline brute-forcing of a leaked hash expensive. Never a fast hash (MD5/SHA).
- **Typed, short-lived tokens.** Access tokens expire quickly and carry only
  identity — never permissions, which must stay revocable (see
  ``app.domains.iam.service.get_effective_permissions``). Refresh tokens are
  long-lived, rotated on every use, and tagged with a ``type`` claim so one kind
  can never be replayed as the other.

Each token carries a ``jti`` (JWT ID). For refresh tokens that id is recorded in
the database, which is what makes a single token revocable — see
``app.domains.auth.service``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from urllib.parse import quote

import jwt
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from passlib.context import CryptContext
from passlib.exc import UnknownHashError
from webauthn import base64url_to_bytes, verify_authentication_response
from webauthn.helpers.exceptions import InvalidAuthenticationResponse

from app.config import settings
from app.domains.auth.exceptions import InvalidAuthTokenError

TokenType = Literal["access", "refresh", "mfa_challenge"]

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


def create_mfa_challenge_token(
    user_id: uuid.UUID, *, enrollment_required: bool
) -> tuple[str, str, str]:
    """Issue a short-lived, non-privileged MFA challenge token.

    The WebAuthn challenge is bound into the signed token. Nothing is stored
    server-side, and changing either the user or challenge invalidates the
    signature.
    """
    challenge = secrets.token_bytes(32)
    encoded_challenge = _base64url(challenge)
    token, jti, _ = _encode(
        user_id,
        "mfa_challenge",
        timedelta(minutes=settings.mfa_challenge_ttl_minutes),
        {
            "challenge": encoded_challenge,
            "enrollment_required": enrollment_required,
        },
    )
    return token, encoded_challenge, jti


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


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _mfa_encryption_key() -> bytes:
    """Derive the dedicated 256-bit key used only for MFA secret envelopes."""
    configured = settings.mfa_encryption_key
    configured_value = configured.get_secret_value() if configured is not None else ""
    material = (
        configured_value if configured_value else settings.jwt_secret.get_secret_value()
    )
    return hashlib.sha256(f"cbpupsis:mfa:v1:{material}".encode()).digest()


def encrypt_mfa_secret(secret: str) -> str:
    """Encrypt a factor secret with AES-256-GCM and a fresh 96-bit nonce."""
    nonce = secrets.token_bytes(12)
    ciphertext = AESGCM(_mfa_encryption_key()).encrypt(
        nonce, secret.encode("ascii"), b"cbpupsis-admin-mfa-v1"
    )
    return _base64url(nonce + ciphertext)


def decrypt_mfa_secret(envelope: str) -> str:
    """Decrypt an MFA secret envelope after authenticating its GCM tag."""
    raw = base64url_to_bytes(envelope)
    plaintext = AESGCM(_mfa_encryption_key()).decrypt(
        raw[:12], raw[12:], b"cbpupsis-admin-mfa-v1"
    )
    return plaintext.decode("ascii")


def hash_mfa_identifier(value: str) -> str:
    """Return the stable digest used to identify encrypted credentials."""
    return hmac.new(
        _mfa_encryption_key(), value.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def verify_mfa_identifier(value: str, expected_digest: str) -> bool:
    """Compare an encrypted credential's presented id in constant time."""
    return hmac.compare_digest(hash_mfa_identifier(value), expected_digest)


def generate_totp_secret() -> str:
    """Return a 160-bit Base32 TOTP seed without padding."""
    return base64.b32encode(secrets.token_bytes(20)).rstrip(b"=").decode("ascii")


def totp_provisioning_uri(secret: str, email: str) -> str:
    """Build the standard otpauth URI understood by authenticator apps."""
    issuer = settings.app_name
    label = quote(f"{issuer}:{email}")
    return (
        f"otpauth://totp/{label}?secret={secret}&issuer={quote(issuer)}"
        "&algorithm=SHA1&digits=6&period=30"
    )


def _totp_at(secret: str, counter: int) -> str:
    padding = "=" * (-len(secret) % 8)
    key = base64.b32decode(secret + padding, casefold=True)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return f"{value % 1_000_000:06d}"


def matching_totp_counter(
    secret: str, code: str, *, at: datetime | None = None
) -> int | None:
    """Return the matching time-step, with one period of clock skew either way."""
    if len(code) != 6 or not code.isascii() or not code.isdigit():
        return None
    instant = at or datetime.now(UTC)
    counter = int(instant.timestamp()) // 30
    for skew in (-1, 0, 1):
        candidate = counter + skew
        if hmac.compare_digest(_totp_at(secret, candidate), code):
            return candidate
    return None


def verify_totp(secret: str, code: str, *, at: datetime | None = None) -> bool:
    """Verify a six-digit TOTP code with one period of clock skew either way."""
    return matching_totp_counter(secret, code, at=at) is not None


def verify_webauthn_assertion(
    *,
    assertion: dict[str, Any],
    challenge: str,
    credential_public_key: str,
    current_sign_count: int,
) -> int | None:
    """Verify a hardware-key assertion, returning its next sign counter.

    ``None`` deliberately collapses all malformed, origin, challenge, RP,
    signature, and replay failures into the same authentication result.
    """
    try:
        verification = verify_authentication_response(
            credential=assertion,
            expected_challenge=base64url_to_bytes(challenge),
            expected_rp_id=settings.webauthn_rp_id,
            expected_origin=settings.webauthn_origin,
            credential_public_key=base64url_to_bytes(credential_public_key),
            credential_current_sign_count=current_sign_count,
            require_user_verification=True,
        )
    except (InvalidAuthenticationResponse, ValueError, TypeError):
        return None
    return verification.new_sign_count
