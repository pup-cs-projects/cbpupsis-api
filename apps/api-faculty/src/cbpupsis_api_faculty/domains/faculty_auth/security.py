"""Cryptographic and token primitives for faculty authentication and MFA."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote

import jwt
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from cbpupsis_core.config import settings
from cbpupsis_shared.domains.auth.exceptions import InvalidAuthTokenError

CHALLENGE_TTL_MINUTES = 5
AES_NONCE_BYTES = 12


def _get_aes_key() -> bytes:
    """Derive a 256-bit AES key from system jwt_secret."""
    return hashlib.sha256(
        settings.jwt_secret.get_secret_value().encode("utf-8")
    ).digest()


def encrypt_totp_secret(secret: str) -> str:
    """Encrypt a TOTP secret using AES-256-GCM before storing in database."""
    aesgcm = AESGCM(_get_aes_key())
    nonce = secrets.token_bytes(AES_NONCE_BYTES)
    ciphertext = aesgcm.encrypt(nonce, secret.encode("utf-8"), None)
    return base64.b64encode(nonce + ciphertext).decode("ascii")


def decrypt_totp_secret(encrypted_secret: str) -> str:
    """Decrypt an AES-256-GCM encrypted TOTP secret from storage."""
    try:
        raw = base64.b64decode(encrypted_secret.encode("ascii"))
        if len(raw) < AES_NONCE_BYTES:
            raise ValueError("Encrypted secret payload too short.")
        nonce = raw[:AES_NONCE_BYTES]
        ciphertext = raw[AES_NONCE_BYTES:]
        aesgcm = AESGCM(_get_aes_key())
        decrypted = aesgcm.decrypt(nonce, ciphertext, None)
        return decrypted.decode("utf-8")
    except Exception as exc:
        raise ValueError("Could not decrypt TOTP secret.") from exc


def create_faculty_challenge_token(user_id: uuid.UUID, identifier: str) -> str:
    """Issue a signed, short-lived JWT challenge token for faculty MFA."""
    now = datetime.now(UTC)
    payload = {
        "sub": str(user_id),
        "identifier": identifier,
        "type": "faculty_mfa_challenge",
        "role": "FACULTY",
        "jti": str(uuid.uuid4()),
        "iat": now,
        "exp": now + timedelta(minutes=CHALLENGE_TTL_MINUTES),
    }
    return jwt.encode(
        payload,
        settings.jwt_secret.get_secret_value(),
        algorithm=settings.jwt_algorithm,
    )


def decode_faculty_challenge_token(token: str) -> dict[str, Any]:
    """Validate and decode a faculty MFA challenge token."""
    try:
        claims: dict[str, Any] = jwt.decode(
            token,
            settings.jwt_secret.get_secret_value(),
            algorithms=[settings.jwt_algorithm],
        )
    except jwt.InvalidTokenError as exc:
        raise InvalidAuthTokenError from exc

    if claims.get("type") != "faculty_mfa_challenge":
        raise InvalidAuthTokenError
    return claims


def generate_totp_secret() -> str:
    """Return a 160-bit RFC 6238 Base32 seed for Google Authenticator."""
    return base64.b32encode(secrets.token_bytes(20)).rstrip(b"=").decode("ascii")


def totp_provisioning_uri(
    secret: str, account_name: str, issuer: str = "PUP CBPUPSIS"
) -> str:
    """Build an otpauth URI accepted by Google Authenticator."""
    label = quote(f"{issuer}:{account_name}")
    return (
        f"otpauth://totp/{label}?secret={secret}&issuer={quote(issuer)}"
        "&algorithm=SHA1&digits=6&period=30"
    )


def totp_at(secret: str, counter: int) -> str:
    """Calculate 6-digit TOTP code for a specific 30-second interval."""
    padding = "=" * (-len(secret) % 8)
    key = base64.b32decode(secret + padding, casefold=True)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return f"{value % 1_000_000:06d}"


def matching_totp_counter(
    secret: str,
    code: str,
    *,
    at: datetime | None = None,
    drift_steps: int = 1,
) -> int | None:
    """Verify a 6-digit code against secret with narrow tolerance (+/- 30s).

    Rejects drift beyond 30 seconds (such as 90s in past or 90s ahead).
    """
    if len(code) != 6 or not code.isascii() or not code.isdigit():
        return None
    counter = int((at or datetime.now(UTC)).timestamp()) // 30
    for skew in range(-drift_steps, drift_steps + 1):
        candidate = counter + skew
        if hmac.compare_digest(totp_at(secret, candidate), code):
            return candidate
    return None


def is_recently_expired_totp(
    secret: str,
    code: str,
    *,
    at: datetime | None = None,
) -> bool:
    """Check if code was valid 60-90s ago, enabling informative usability feedback."""
    if len(code) != 6 or not code.isascii() or not code.isdigit():
        return False
    counter = int((at or datetime.now(UTC)).timestamp()) // 30
    for skew in (-2, -3):
        if hmac.compare_digest(totp_at(secret, counter + skew), code):
            return True
    return False


def create_faculty_access_token(
    user_id: uuid.UUID,
    identifier: str,
    *,
    ttl_minutes: int | None = None,
) -> str:
    """Issue a scoped JWT access token for faculty role."""
    now = datetime.now(UTC)
    ttl = timedelta(minutes=ttl_minutes or settings.access_token_ttl_minutes)
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "identifier": identifier,
        "type": "access",
        "role": "faculty",
        "iat": now,
        "exp": now + ttl,
        "jti": str(uuid.uuid4()),
    }
    return jwt.encode(
        payload,
        settings.jwt_secret.get_secret_value(),
        algorithm=settings.jwt_algorithm,
    )


def create_faculty_refresh_token(user_id: uuid.UUID) -> tuple[str, str, datetime]:
    """Issue a long-lived JWT refresh token with jti and expiry."""
    now = datetime.now(UTC)
    ttl = timedelta(days=settings.refresh_token_ttl_days)
    expires_at = now + ttl
    jti = str(uuid.uuid4())
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "type": "refresh",
        "role": "faculty",
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


def generate_recovery_codes(count: int = 8) -> list[str]:
    """Generate human-readable alphanumeric recovery backup codes."""
    codes: list[str] = []
    for _ in range(count):
        part1 = secrets.token_hex(2).upper()
        part2 = secrets.token_hex(2).upper()
        codes.append(f"{part1}-{part2}")
    return codes


def hash_recovery_code(code: str) -> str:
    """Return SHA-256 hash of a normalized recovery code for storage."""
    normalized = code.strip().upper().replace(" ", "").replace("-", "")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()
