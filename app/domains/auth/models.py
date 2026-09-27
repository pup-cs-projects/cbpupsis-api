"""SQLAlchemy models for the auth domain.

Two ledgers live here; user identity belongs to the users domain. Access tokens
are deliberately *not* stored — they are short-lived and verified by signature
alone, so a database round-trip per request is not needed.

- :class:`RefreshToken` records issued refresh tokens by ``jti`` so rotation can
  invalidate them.
- :class:`OneTimeToken` backs email verification and password reset: mailed
  secrets that must work exactly once and then expire.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.domains.auth.constants import JTI_LENGTH, TOKEN_HASH_LENGTH
from app.shared.models import TimestampMixin, UUIDMixin


class RefreshToken(UUIDMixin, TimestampMixin, Base):
    """A record of one issued refresh token, enabling revocation.

    Storing only the ``jti`` (not the token itself) is enough to revoke: a
    presented token is accepted only if its id is still live here. This is what
    makes rotation meaningful — each refresh marks the old id used, so replaying
    a stolen token fails and is detectable.
    """

    __tablename__ = "refresh_tokens"

    #: Owning user. No hard FK: the users domain must stay separable.
    user_id: Mapped[uuid.UUID] = mapped_column(index=True)
    #: The token's ``jti`` claim; unique per issued token.
    jti: Mapped[str] = mapped_column(String(JTI_LENGTH), unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    #: Set when the token is rotated or explicitly revoked; NULL means live.
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )


class TokenPurpose(enum.StrEnum):
    """What a :class:`OneTimeToken` authorises.

    The discriminator is what makes one table safe to share: a verification
    token presented to the reset endpoint does not match on purpose and is
    rejected, so the two flows cannot be crossed.
    """

    email_verification = "email_verification"
    password_reset = "password_reset"


class AdminPosition(enum.StrEnum):
    """The data-reach position carried by a single Admin-role session."""

    chairperson = "chairperson"
    dean = "dean"
    registrar = "registrar"


class AuthFailureStep(enum.StrEnum):
    """Which half of sign-in failed; both count toward one lockout window."""

    credentials = "credentials"
    mfa = "mfa"


class OneTimeToken(UUIDMixin, TimestampMixin, Base):
    """A single-use, expiring secret mailed to a user.

    Email verification and password reset share this table because their
    mechanics are identical — issue a high-entropy secret, mail it, accept it
    once before an expiry — and the ``purpose`` column keeps them from being
    interchangeable. Duplicating the table would duplicate the consume logic,
    which is the part that must not have two subtly different versions.

    **Only a SHA-256 digest of the secret is stored.** The raw value exists in
    the email and nowhere else, so a database leak yields nothing replayable.
    A plain digest rather than argon2 is deliberate and safe *here*: the secret
    is 32 random bytes from ``secrets``, so there is no guessable input to slow
    an attacker down over, and reset flows should not pay a KDF's latency.
    """

    __tablename__ = "one_time_tokens"

    #: Owning user. No hard FK: the users domain must stay separable.
    user_id: Mapped[uuid.UUID] = mapped_column(index=True)
    #: SHA-256 hex digest of the mailed secret. Unique, and the sole lookup key.
    token_hash: Mapped[str] = mapped_column(
        String(TOKEN_HASH_LENGTH), unique=True, index=True
    )
    #: Which flow this token belongs to; see :class:`TokenPurpose`.
    purpose: Mapped[TokenPurpose] = mapped_column(
        # native_enum=False stores a plain VARCHAR + CHECK constraint rather
        # than a Postgres ENUM type, so adding a purpose later is an ordinary
        # migration instead of an ALTER TYPE that cannot run in a transaction.
        # create_constraint=True is explicit because SQLAlchemy 2.0 defaults it
        # off, which would leave the column accepting any string at all.
        Enum(
            TokenPurpose,
            native_enum=False,
            length=32,
            create_constraint=True,
            name="ck_one_time_tokens_purpose",
        ),
        index=True,
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    #: Set the moment the token is redeemed; NULL means unused. Together with
    #: the unique ``token_hash`` this is what makes redemption single-use.
    consumed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )


class AdminAuthProfile(TimestampMixin, Base):
    """Administrative position, scope, and encrypted MFA material.

    The primary key is the user id without a cross-domain foreign key, matching
    the refresh-token boundary. Membership in IAM's ``Admins`` group remains
    the source of the role; this row supplies the position and factor only.
    """

    __tablename__ = "admin_auth_profiles"

    user_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    position: Mapped[AdminPosition] = mapped_column(
        Enum(
            AdminPosition,
            native_enum=False,
            length=32,
            create_constraint=True,
            name="ck_admin_auth_profiles_position",
        )
    )
    department_id: Mapped[str | None] = mapped_column(String(100), default=None)
    college_id: Mapped[str | None] = mapped_column(String(100), default=None)
    #: Only the latest password-step challenge may complete MFA. Clearing it
    #: on success makes the signed challenge single-use rather than replayable.
    active_mfa_challenge_jti: Mapped[str | None] = mapped_column(
        String(JTI_LENGTH), default=None
    )

    #: AES-256-GCM envelope (nonce + ciphertext + tag), never a raw TOTP secret.
    totp_secret_encrypted: Mapped[str | None] = mapped_column(String(512), default=None)
    totp_confirmed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    #: Highest accepted TOTP time-step. Persisting it prevents a code that
    #: completed one challenge from being replayed through a fresh challenge.
    totp_last_used_counter: Mapped[int | None] = mapped_column(default=None)

    #: WebAuthn material is encrypted under the same authenticated envelope.
    #: A stable digest preserves uniqueness without storing a lookup identifier
    #: in plaintext (GCM ciphertext is deliberately randomized).
    webauthn_credential_id_hash: Mapped[str | None] = mapped_column(
        String(64), default=None, unique=True, index=True
    )
    webauthn_credential_id_encrypted: Mapped[str | None] = mapped_column(
        String(2048), default=None
    )
    webauthn_public_key_encrypted: Mapped[str | None] = mapped_column(
        Text, default=None
    )
    webauthn_sign_count: Mapped[int] = mapped_column(Integer, default=0)


class AuthenticationFailure(UUIDMixin, Base):
    """One failed credential or MFA attempt in the rolling lockout window."""

    __tablename__ = "authentication_failures"

    user_id: Mapped[uuid.UUID] = mapped_column(index=True)
    step: Mapped[AuthFailureStep] = mapped_column(
        Enum(
            AuthFailureStep,
            native_enum=False,
            length=32,
            create_constraint=True,
            name="ck_authentication_failures_step",
        )
    )
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class AuthenticationLockout(Base):
    """Current account lock, shared by the password and MFA steps."""

    __tablename__ = "authentication_lockouts"

    user_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    locked_until: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
