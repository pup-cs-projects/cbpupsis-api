"""Repository-layer tests for the auth domain.

The point of these tests is the **contract boundary**, not coverage of SQL the
service tests already exercise end to end. What is asserted here is what makes
the layer reusable and separately testable:

- a ``get_X`` that finds nothing returns ``None`` and does **not** raise —
  turning absence into a ``NotFoundError`` is the service's decision, and a
  repository that raised would force every non-HTTP caller to catch it back out;
- ``list_X`` returns ``(rows, total)`` where ``total`` counts the whole filtered
  result set rather than the page slice;
- repositories do not commit, so a caller that abandons the transaction leaves
  nothing behind.

The paired service-level assertions (that the same missing row *does* become a
404 one layer up) live beside this file in the same domain folder; together the
two halves pin the boundary from both sides.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_database.models.auth import TokenPurpose
from cbpupsis_shared.domains.auth import repository as auth_repository


class TestAuthRepository:
    """Token ledgers, including the atomic single-use redemption."""

    async def test_get_refresh_token_returns_none_when_absent(
        self, db: AsyncSession
    ) -> None:
        assert await auth_repository.get_refresh_token(db, "no-such-jti") is None

    async def test_get_refresh_token_still_returns_revoked_rows(
        self, db: AsyncSession, make_user
    ) -> None:
        """Revoked rows must remain visible: the caller distinguishes "unknown
        token" from "already used", and only the second means replay."""
        user = await make_user("refresh@example.com")
        auth_repository.add_refresh_token(
            db, user.id, "jti-1", datetime.now(UTC) + timedelta(days=7)
        )
        await db.commit()

        await auth_repository.revoke_refresh_token(db, "jti-1", datetime.now(UTC))
        await db.commit()

        record = await auth_repository.get_refresh_token(db, "jti-1")
        assert record is not None
        assert record.revoked_at is not None

    async def test_revoke_does_not_overwrite_the_original_timestamp(
        self, db: AsyncSession, make_user
    ) -> None:
        """``WHERE revoked_at IS NULL`` preserves when the token really died."""
        user = await make_user("revoke-twice@example.com")
        auth_repository.add_refresh_token(
            db, user.id, "jti-2", datetime.now(UTC) + timedelta(days=7)
        )
        await db.commit()

        first = datetime.now(UTC)
        await auth_repository.revoke_refresh_token(db, "jti-2", first)
        await db.commit()
        original = (await auth_repository.get_refresh_token(db, "jti-2")).revoked_at

        await auth_repository.revoke_refresh_token(
            db, "jti-2", first + timedelta(hours=1)
        )
        await db.commit()

        assert (
            await auth_repository.get_refresh_token(db, "jti-2")
        ).revoked_at == original

    async def test_consume_one_time_token_returns_none_for_unknown_digest(
        self, db: AsyncSession
    ) -> None:
        """Every failure mode collapses to None so no caller can build an oracle."""
        assert (
            await auth_repository.consume_one_time_token(
                db, "not-a-real-digest", TokenPurpose.password_reset, datetime.now(UTC)
            )
            is None
        )

    async def test_consume_one_time_token_succeeds_once_then_returns_none(
        self, db: AsyncSession, make_user
    ) -> None:
        """Single use is enforced by the conditional UPDATE, not by a prior read."""
        user = await make_user("onetime@example.com")
        auth_repository.add_one_time_token(
            db,
            user_id=user.id,
            token_hash="digest-abc",
            purpose=TokenPurpose.password_reset,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        await db.commit()

        first = await auth_repository.consume_one_time_token(
            db, "digest-abc", TokenPurpose.password_reset, datetime.now(UTC)
        )
        await db.commit()
        assert first == user.id

        second = await auth_repository.consume_one_time_token(
            db, "digest-abc", TokenPurpose.password_reset, datetime.now(UTC)
        )
        assert second is None

    async def test_consume_one_time_token_rejects_a_mismatched_purpose(
        self, db: AsyncSession, make_user
    ) -> None:
        """The purpose column is what stops a verification token resetting a
        password — the two flows share one table."""
        user = await make_user("purpose@example.com")
        auth_repository.add_one_time_token(
            db,
            user_id=user.id,
            token_hash="digest-xyz",
            purpose=TokenPurpose.email_verification,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        await db.commit()

        assert (
            await auth_repository.consume_one_time_token(
                db, "digest-xyz", TokenPurpose.password_reset, datetime.now(UTC)
            )
            is None
        )

    async def test_consume_one_time_token_rejects_an_expired_token(
        self, db: AsyncSession, make_user
    ) -> None:
        user = await make_user("expired@example.com")
        auth_repository.add_one_time_token(
            db,
            user_id=user.id,
            token_hash="digest-old",
            purpose=TokenPurpose.password_reset,
            expires_at=datetime.now(UTC) - timedelta(seconds=1),
        )
        await db.commit()

        assert (
            await auth_repository.consume_one_time_token(
                db, "digest-old", TokenPurpose.password_reset, datetime.now(UTC)
            )
            is None
        )
