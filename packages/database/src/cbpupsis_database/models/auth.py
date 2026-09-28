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

from sqlalchemy import Boolean, DateTime, Enum, String, Text
from sqlalchemy.dialects.postgresql import INET
from sqlalchemy.orm import Mapped, mapped_column

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


class ActiveSession(UUIDMixin, Base):
    """Server-side session state used for idle-revocable Superadmin tokens."""

    __tablename__ = "user_active_sessions"

    user_id: Mapped[uuid.UUID] = mapped_column(index=True)
    session_token_hash: Mapped[str] = mapped_column(String(64))
    device_info: Mapped[str] = mapped_column(Text)
    ip_address: Mapped[str] = mapped_column(INET().with_variant(String(45), "sqlite"))
    is_current: Mapped[bool] = mapped_column(Boolean, default=True)
    last_activity_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


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
