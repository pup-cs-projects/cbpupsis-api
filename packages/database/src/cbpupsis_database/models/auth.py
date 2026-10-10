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
from typing import Any

import sqlalchemy as sa
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from cbpupsis_database.base import Base, TimestampMixin, UUIDMixin

#: SHA-256 hex digest of a refresh or one-time token.
TOKEN_HASH_LENGTH = 64
#: A UUID4 string.
JTI_LENGTH = 36


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
            create_constraint=False,
        ),
        index=True,
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    #: Set the moment the token is redeemed; NULL means unused. Together with
    #: the unique ``token_hash`` this is what makes redemption single-use.
    consumed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )

    __table_args__ = (
        CheckConstraint(
            "purpose IN ('email_verification', 'password_reset')",
            name="ck_one_time_tokens_purpose",
        ),
    )


class UserMfaCredential(UUIDMixin, Base):
    """MFA factors registered by a user (TOTP, WebAuthn, FIDO2)."""

    __tablename__ = "user_mfa_credentials"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE")
    )
    mfa_type: Mapped[str] = mapped_column(String(20))
    totp_secret: Mapped[str | None] = mapped_column(String(255), default=None)
    credential_id: Mapped[str | None] = mapped_column(Text, default=None)
    credential_id_digest: Mapped[str | None] = mapped_column(String(64), default=None)
    public_key: Mapped[str | None] = mapped_column(Text, default=None)
    sign_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    is_enrolled: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false"
    )
    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        Index("idx_fk_user_mfa_credentials_user_id", "user_id"),
        Index(
            "ix_user_mfa_credential_id_digest",
            "credential_id_digest",
            unique=True,
            postgresql_where=sa.text("credential_id_digest IS NOT NULL"),
        ),
    )


class UserMfaRecoveryCode(UUIDMixin, Base):
    """One-time recovery backup codes for MFA."""

    __tablename__ = "user_mfa_recovery_codes"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE")
    )
    code_hash: Mapped[str] = mapped_column(String(64))
    is_used: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false"
    )
    used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (Index("idx_fk_user_mfa_recovery_codes_user_id", "user_id"),)


_INET_TYPE = postgresql.INET().with_variant(String(45), "sqlite")
_JSON_TYPE = postgresql.JSONB(astext_type=Text()).with_variant(JSON(), "sqlite")


class UserActiveSession(UUIDMixin, Base):
    """Tracks active login sessions for concurrency controls and device tracking."""

    __tablename__ = "user_active_sessions"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE")
    )
    session_token_hash: Mapped[str] = mapped_column(String(64))
    device_info: Mapped[str] = mapped_column(Text)
    ip_address: Mapped[str] = mapped_column(_INET_TYPE)
    is_current: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false"
    )
    last_activity_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (Index("idx_fk_user_active_sessions_user_id", "user_id"),)


# Existing Superadmin session call sites use this name for the canonical table.
ActiveSession = UserActiveSession


class IdempotencyKey(UUIDMixin, Base):
    """Prevents duplicate execution of critical financial and academic requests."""

    __tablename__ = "idempotency_keys"

    idempotency_key: Mapped[str] = mapped_column(String(255))
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), default=None
    )
    request_method: Mapped[str] = mapped_column(String(10))
    request_path: Mapped[str] = mapped_column(String(255))
    request_payload_hash: Mapped[str | None] = mapped_column(String(64), default=None)
    response_code: Mapped[int | None] = mapped_column(Integer, default=None)
    response_body: Mapped[dict[str, Any] | None] = mapped_column(
        _JSON_TYPE, default=None
    )
    status: Mapped[str] = mapped_column(
        String(30), default="processing", server_default="processing"
    )
    locked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp()
    )

    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_idempotency_key"),
        Index(
            "ix_idempotency_keys_key_user",
            "idempotency_key",
            "user_id",
            unique=True,
        ),
        Index("ix_idempotency_keys_expires_at", "expires_at", unique=False),
    )


class AuthFailureStep(enum.StrEnum):
    """Both halves of Admin sign-in share one failure window."""

    credentials = "credentials"
    mfa = "mfa"


class AuthenticationFailure(UUIDMixin, Base):
    """One failed password or MFA attempt in the rolling lockout window."""

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
    """The current temporary Admin account lock, if any."""

    __tablename__ = "authentication_lockouts"

    user_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    locked_until: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class PasswordResetThrottle(Base):
    """Address allowance persisted across API processes and Lambda invocations."""

    __tablename__ = "password_reset_throttles"

    email_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    window_started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    requests: Mapped[int] = mapped_column(Integer)
