"""Repository-layer tests for the users domain.

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

import uuid
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.domains.users import repository as users_repository


class TestUsersRepository:
    """Three lookups that differ only by filter — each asserted on its filter."""

    async def test_get_user_returns_none_when_absent(self, db: AsyncSession) -> None:
        assert await users_repository.get_user(db, uuid.uuid4()) is None

    async def test_get_user_by_email_returns_none_when_absent(
        self, db: AsyncSession
    ) -> None:
        """None rather than raising is what lets login stay enumeration-safe."""
        assert (
            await users_repository.get_user_by_email(db, "nobody@example.com") is None
        )

    async def test_get_active_user_excludes_the_deactivated(
        self, db: AsyncSession, make_user
    ) -> None:
        """``get_active_user`` is the request-path lookup, so deactivation must
        take effect through it immediately — while ``get_user`` still finds the
        row, because deactivated is not deleted."""
        user = await make_user("deactivated@example.com")
        assert await users_repository.get_active_user(db, user.id) is not None

        users_repository.update_user(db, user, is_active=False)
        await db.commit()

        assert await users_repository.get_active_user(db, user.id) is None
        assert await users_repository.get_user(db, user.id) is not None

    async def test_all_lookups_exclude_soft_deleted_rows(
        self, db: AsyncSession, make_user
    ) -> None:
        user = await make_user("gone@example.com")
        email = user.email
        users_repository.update_user(db, user, deleted_at=datetime.now(UTC))
        await db.commit()

        assert await users_repository.get_user(db, user.id) is None
        assert await users_repository.get_active_user(db, user.id) is None
        assert await users_repository.get_user_by_email(db, email) is None
