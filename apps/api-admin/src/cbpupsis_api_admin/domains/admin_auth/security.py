"""Cryptographic primitives specific to Admin MFA."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import struct
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote

import jwt
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from webauthn import (
    base64url_to_bytes,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers.exceptions import InvalidAuthenticationResponse, WebAuthnException
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    UserVerificationRequirement,
)

from cbpupsis_core.config import settings
from cbpupsis_shared.domains.auth.exceptions import InvalidAuthTokenError


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode_base64url(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def create_challenge_token(
    user_id: uuid.UUID, *, enrollment_required: bool
) -> tuple[str, str, str]:
    """Issue a signed, short-lived token that carries no API privilege."""
    now = datetime.now(UTC)
    challenge = _base64url(secrets.token_bytes(32))
    jti = str(uuid.uuid4())
    token = jwt.encode(
        {
            "sub": str(user_id),
            "type": "admin_mfa_challenge",
            "iat": now,
            "exp": now + timedelta(minutes=settings.mfa_challenge_ttl_minutes),
            "jti": jti,
            "challenge": challenge,
            "enrollment_required": enrollment_required,
        },
        settings.jwt_secret.get_secret_value(),
        algorithm=settings.jwt_algorithm,
    )
    return token, challenge, jti


def decode_challenge_token(token: str) -> dict[str, Any]:
    """Validate an Admin challenge without accepting it as a session."""
    try:
        claims: dict[str, Any] = jwt.decode(
            token,
            settings.jwt_secret.get_secret_value(),
            algorithms=[settings.jwt_algorithm],
        )
    except jwt.InvalidTokenError as exc:
        raise InvalidAuthTokenError from exc
    if claims.get("type") != "admin_mfa_challenge":
        raise InvalidAuthTokenError
    return claims


def _encryption_key() -> bytes:
    configured = settings.mfa_encryption_key
    material = (
        configured.get_secret_value()
        if configured is not None
        else settings.jwt_secret.get_secret_value()
    )
    return hashlib.sha256(f"cbpupsis:admin-mfa:v1:{material}".encode()).digest()


def encrypt_secret(secret: str) -> str:
    """Encrypt factor material with AES-256-GCM."""
    nonce = secrets.token_bytes(12)
    ciphertext = AESGCM(_encryption_key()).encrypt(
        nonce, secret.encode("utf-8"), b"cbpupsis-admin-mfa-v1"
    )
    return _base64url(nonce + ciphertext)


def decrypt_secret(envelope: str) -> str:
    """Authenticate and decrypt an MFA envelope."""
    raw = _decode_base64url(envelope)
    plaintext = AESGCM(_encryption_key()).decrypt(
        raw[:12], raw[12:], b"cbpupsis-admin-mfa-v1"
    )
    return plaintext.decode("utf-8")


def credential_id_digest(credential_id: str) -> str:
    """Return a stable keyed digest for cross-account credential uniqueness."""
    return hmac.new(
        _encryption_key(),
        f"credential-id:{credential_id}".encode(),
        hashlib.sha256,
    ).hexdigest()


def generate_totp_secret() -> str:
    """Return a 160-bit RFC 6238 Base32 seed."""
    return base64.b32encode(secrets.token_bytes(20)).rstrip(b"=").decode("ascii")


def totp_provisioning_uri(secret: str, email: str) -> str:
    """Build an otpauth URI accepted by Google Authenticator."""
    issuer = "CBPUPSIS"
    label = quote(f"{issuer}:{email}")
    return (
        f"otpauth://totp/{label}?secret={secret}&issuer={quote(issuer)}"
        "&algorithm=SHA1&digits=6&period=30"
    )


def totp_at(secret: str, counter: int) -> str:
    padding = "=" * (-len(secret) % 8)
    key = base64.b32decode(secret + padding, casefold=True)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return f"{value % 1_000_000:06d}"


def matching_totp_counter(
    secret: str, code: str, *, at: datetime | None = None
) -> int | None:
    """Return the matching 30-second counter with one period of clock skew."""
    if len(code) != 6 or not code.isascii() or not code.isdigit():
        return None
    counter = int((at or datetime.now(UTC)).timestamp()) // 30
    for skew in (-1, 0, 1):
        candidate = counter + skew
        if hmac.compare_digest(totp_at(secret, candidate), code):
            return candidate
    return None


def webauthn_registration_options(
    *, user_id: uuid.UUID, email: str, challenge: str
) -> dict[str, Any]:
    """Build browser registration options bound to the password challenge."""
    options = generate_registration_options(
        rp_id=settings.webauthn_rp_id,
        rp_name="CBPUPSIS",
        user_id=user_id.bytes,
        user_name=email,
        challenge=base64url_to_bytes(challenge),
        authenticator_selection=AuthenticatorSelectionCriteria(
            user_verification=UserVerificationRequirement.REQUIRED
        ),
    )
    return json.loads(options_to_json(options))


def verify_webauthn_registration(
    *, credential: dict[str, Any], challenge: str
) -> tuple[str, str, int] | None:
    """Verify browser registration before any credential material is stored."""
    try:
        verified = verify_registration_response(
            credential=credential,
            expected_challenge=base64url_to_bytes(challenge),
            expected_rp_id=settings.webauthn_rp_id,
            expected_origin=settings.webauthn_origin,
            require_user_verification=True,
        )
    except (WebAuthnException, ValueError, TypeError, KeyError):
        return None
    return (
        _base64url(verified.credential_id),
        _base64url(verified.credential_public_key),
        verified.sign_count,
    )


def verify_webauthn_assertion(
    *,
    assertion: dict[str, Any],
    challenge: str,
    credential_public_key: str,
    current_sign_count: int,
) -> int | None:
    """Verify a hardware-key assertion and return its next sign counter."""
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
