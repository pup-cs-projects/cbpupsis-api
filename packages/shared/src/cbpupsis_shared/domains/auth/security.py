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

The second-factor primitives every role shares live here too: MFA challenge
tokens, AES-GCM protection of factor secrets, RFC 6238 TOTP, recovery codes, and
the keyed lockout pseudonym for identifiers that match no account.
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

import bcrypt
import jwt
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from passlib.context import CryptContext
from passlib.exc import UnknownHashError

from cbpupsis_core.config import settings
from cbpupsis_shared.domains.auth.constants import (
    BCRYPT_MAX_PASSWORD_BYTES,
    PASSWORD_MAX_LENGTH,
    RECOVERY_CODE_BYTES,
    RECOVERY_CODE_COUNT,
    RESET_PASSWORD_MIN_LENGTH,
    TEMPORARY_PASSWORD_ALPHABETS,
    TEMPORARY_PASSWORD_LENGTH,
    TOTP_ISSUER,
)
from cbpupsis_shared.domains.auth.exceptions import (
    InvalidAuthTokenError,
    PasswordTooWeakError,
    SessionExpiredError,
)

TokenType = Literal["access", "refresh"]

#: Verifies hashes written before the move to bcrypt; never used to hash.
legacy_argon2_context = CryptContext(schemes=["argon2"], deprecated="auto")


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


def generate_temporary_password() -> str:
    """Return a random password that satisfies :func:`check_password_complexity`.

    One character from each class, the rest from all of them, then shuffled, so
    no position is predictable from the construction.
    """
    everything = "".join(TEMPORARY_PASSWORD_ALPHABETS)
    chars = [secrets.choice(alphabet) for alphabet in TEMPORARY_PASSWORD_ALPHABETS]
    chars += [
        secrets.choice(everything)
        for _ in range(TEMPORARY_PASSWORD_LENGTH - len(chars))
    ]
    secrets.SystemRandom().shuffle(chars)
    return "".join(chars)


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

    Supports both bcrypt and argon2 hashes.
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
        return legacy_argon2_context.verify(raw_password, password_hash)
    except (UnknownHashError, ValueError, UnicodeError):
        return False


def encode_token(
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
    token, _, _ = encode_token(
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
    return encode_token(
        user_id,
        "refresh",
        timedelta(days=settings.refresh_token_ttl_days),
        claims,
    )


def decode_token(
    token: str, expected_type: TokenType, *, allow_expired: bool = False
) -> dict[str, Any]:
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
            options={"verify_exp": not allow_expired},
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


#: Labels for each mailed token's delivery copy. ``reset-email`` is the label
#: reset jobs were already encrypted under, so it must never change.
RESET_EMAIL = "reset-email"
VERIFICATION_EMAIL = "verification-email"
TEMPORARY_PASSWORD_EMAIL = "temporary-password-email"


def mailed_token_key(label: str) -> bytes:
    """Derive the AES key for one kind of mailed token from the JWT secret."""
    return hmac.new(
        settings.jwt_secret.get_secret_value().encode(),
        f"cbpupsis:{label}:v1".encode(),
        hashlib.sha256,
    ).digest()


def encrypt_mailed_token(
    raw_token: str, *, label: str, user_id: uuid.UUID, token_hash: str
) -> str:
    """Protect a token's delivery copy; the ledger stores only its digest.

    Bound by ``label``, recipient, and digest, so a staged job cannot be
    replayed for another purpose or person.
    """
    nonce = secrets.token_bytes(12)
    aad = f"{label}:v1:{user_id}:{token_hash}".encode()
    encrypted = AESGCM(mailed_token_key(label)).encrypt(nonce, raw_token.encode(), aad)
    return base64.urlsafe_b64encode(nonce + encrypted).decode()


def decrypt_mailed_token(
    encrypted: str, *, label: str, user_id: uuid.UUID, token_hash: str
) -> str:
    """Decrypt only for the purpose, recipient, and digest the sender staged."""
    data = base64.urlsafe_b64decode(encrypted)
    aad = f"{label}:v1:{user_id}:{token_hash}".encode()
    return AESGCM(mailed_token_key(label)).decrypt(data[:12], data[12:], aad).decode()


#: A valid bcrypt digest of a discarded random value. Verifying against it when
#: no account matches makes "unknown identifier" cost the same as "wrong
#: password", so response time does not reveal which accounts exist.
DUMMY_PASSWORD_HASH = "$2b$12$j73fQt.SeW2YsA7T0E8zAem7i6oW4H8/bDn27ipKDdwQ86DZe.lEq"


def encode_base64url(value: bytes) -> str:
    """Base64url without padding, as WebAuthn and the MFA envelopes use."""
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def decode_base64url(value: str) -> bytes:
    """Reverse :func:`encode_base64url`, restoring the stripped padding."""
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def create_challenge_token(
    user_id: uuid.UUID,
    *,
    role: str,
    enrollment_required: bool,
    session_version: int = 0,
) -> tuple[str, str, str]:
    """Issue a signed, short-lived MFA challenge that carries no API privilege.

    Returns the token, a random WebAuthn challenge bound into it, and its ``jti``
    (which the caller stores so only the latest challenge is accepted).
    """
    now = datetime.now(UTC)
    challenge = encode_base64url(secrets.token_bytes(32))
    jti = str(uuid.uuid4())
    token = jwt.encode(
        {
            "sub": str(user_id),
            "type": f"{role}_mfa_challenge",
            "iat": now,
            "exp": now + timedelta(minutes=settings.mfa_challenge_ttl_minutes),
            "jti": jti,
            "challenge": challenge,
            "enrollment_required": enrollment_required,
            "session_version": session_version,
        },
        settings.jwt_secret.get_secret_value(),
        algorithm=settings.jwt_algorithm,
    )
    return token, challenge, jti


def decode_challenge_token(token: str, *, role: str | None = None) -> dict[str, Any]:
    """Validate an MFA challenge token without ever accepting it as a session."""
    try:
        claims: dict[str, Any] = jwt.decode(
            token,
            settings.jwt_secret.get_secret_value(),
            algorithms=[settings.jwt_algorithm],
        )
    except jwt.InvalidTokenError as exc:
        raise InvalidAuthTokenError from exc
    token_type = str(claims.get("type", ""))
    if role is not None:
        if token_type != f"{role}_mfa_challenge":
            raise InvalidAuthTokenError
    elif not token_type.endswith("_mfa_challenge"):
        raise InvalidAuthTokenError
    return claims


def mfa_key() -> bytes:
    """Derive the key that protects MFA factors and keys their digests.

    The "admin-mfa" label predates the other roles; changing it would make
    every stored Admin and Superadmin factor undecryptable.
    """
    configured = settings.mfa_encryption_key
    material = (
        configured.get_secret_value()
        if configured is not None
        else settings.jwt_secret.get_secret_value()
    )
    return hashlib.sha256(f"cbpupsis:admin-mfa:v1:{material}".encode()).digest()


#: Associated data bound into every MFA envelope; changing it breaks decryption.
MFA_AAD = b"cbpupsis-admin-mfa-v1"


def encrypt_secret(secret: str) -> str:
    """Encrypt factor material (TOTP seeds, WebAuthn ids and keys) with AES-GCM."""
    nonce = secrets.token_bytes(12)
    ciphertext = AESGCM(mfa_key()).encrypt(nonce, secret.encode("utf-8"), MFA_AAD)
    return encode_base64url(nonce + ciphertext)


def decrypt_secret(envelope: str) -> str:
    """Authenticate and decrypt an MFA envelope."""
    raw = decode_base64url(envelope)
    return AESGCM(mfa_key()).decrypt(raw[:12], raw[12:], MFA_AAD).decode("utf-8")


def credential_id_digest(credential_id: str) -> str:
    """Return a stable keyed digest for cross-account credential uniqueness."""
    return hmac.new(
        mfa_key(), f"credential-id:{credential_id}".encode(), hashlib.sha256
    ).hexdigest()


def generate_totp_secret() -> str:
    """Return a 160-bit RFC 6238 Base32 seed."""
    return base64.b32encode(secrets.token_bytes(20)).rstrip(b"=").decode("ascii")


def totp_provisioning_uri(secret: str, account_name: str) -> str:
    """Build an otpauth URI accepted by Google Authenticator."""
    label = quote(f"{TOTP_ISSUER}:{account_name}")
    return (
        f"otpauth://totp/{label}?secret={secret}&issuer={quote(TOTP_ISSUER)}"
        "&algorithm=SHA1&digits=6&period=30"
    )


def totp_at(secret: str, counter: int) -> str:
    """Return the six-digit code for one 30-second step."""
    padding = "=" * (-len(secret) % 8)
    key = base64.b32decode(secret + padding, casefold=True)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return f"{value % 1_000_000:06d}"


def matching_totp_counter(
    secret: str, code: str, *, at: datetime | None = None
) -> int | None:
    """Return the matching 30-second step, allowing one step of clock skew."""
    if len(code) != 6 or not code.isascii() or not code.isdigit():
        return None
    counter = int((at or datetime.now(UTC)).timestamp()) // 30
    for skew in (-1, 0, 1):
        candidate = counter + skew
        if hmac.compare_digest(totp_at(secret, candidate), code):
            return candidate
    return None


def generate_recovery_codes() -> list[str]:
    """Return fresh single-use recovery codes, 80 random bits each.

    Grouped ``XXXX-XXXX-XXXX-XXXX`` in Base32 so they can be read aloud and typed.
    """
    codes = []
    for _ in range(RECOVERY_CODE_COUNT):
        raw = base64.b32encode(secrets.token_bytes(RECOVERY_CODE_BYTES)).decode()
        codes.append("-".join(raw[i : i + 4] for i in range(0, len(raw), 4)))
    return codes


def hash_recovery_code(code: str) -> str:
    """Return the keyed digest stored for a recovery code.

    Keyed (HMAC under the MFA key) rather than a bare hash, so a leaked table
    cannot be brute-forced offline without the key as well.
    """
    normalized = code.strip().upper().replace("-", "").replace(" ", "")
    return hmac.new(
        mfa_key(), f"recovery-code:{normalized}".encode(), hashlib.sha256
    ).hexdigest()


def lockout_subject(identifier: str) -> uuid.UUID:
    """Return the lockout key for an identifier that matches no account.

    A keyed pseudonym, so unknown identifiers lock exactly like real accounts
    (no enumeration through the lock) without storing the identifier itself.
    """
    digest = hmac.new(
        mfa_key(), f"lockout:{identifier.strip().lower()}".encode(), hashlib.sha256
    ).digest()
    return uuid.UUID(bytes=digest[:16])
