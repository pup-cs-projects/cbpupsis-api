"""Tests for the scheduled digest job.

Two properties matter, and both fail silently when wrong:

- **The timezone arithmetic.** ``local_hour == digest_hour`` looks obviously
  right and is obviously wrong: it never matches a fractional offset, and it
  double-counts or skips across a DST transition. Those users simply never get a
  digest, and nothing errors.
- **Idempotency.** EventBridge retries a failed invocation, so a run that got
  halfway must not re-mail the users it already reached.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.outbox import OutboxMessage
from app.domains.notifications.models import DigestRun
from scripts.send_digests import DIGEST_LOCAL_HOUR, is_due, stage_digests


def _window(hour: int, day: int = 1, month: int = 6) -> tuple[datetime, datetime]:
    start = datetime(2026, month, day, hour, tzinfo=UTC)
    return start, start + timedelta(hours=1)


class TestIsDue:
    def test_a_whole_hour_offset_matches(self) -> None:
        """Asia/Manila is UTC+8, so local 09:00 is UTC 01:00."""
        assert is_due("Asia/Manila", *_window(1))

    def test_a_whole_hour_offset_does_not_match_the_wrong_window(self) -> None:
        assert not is_due("Asia/Manila", *_window(5))

    def test_a_fractional_offset_matches(self) -> None:
        """The case that breaks ``local_hour == digest_hour`` outright.

        Asia/Kolkata is UTC+5:30, so local 09:00 is UTC 03:30 — inside the
        03:00-04:00 window, but never equal to an hour boundary. Compared by
        hour number, these users never receive a digest at all.
        """
        assert is_due("Asia/Kolkata", *_window(3))

    def test_a_forty_five_minute_offset_matches(self) -> None:
        """Asia/Kathmandu is UTC+5:45 — local 09:00 is UTC 03:15."""
        assert is_due("Asia/Kathmandu", *_window(3))

    def test_utc_matches_its_own_hour(self) -> None:
        assert is_due("UTC", *_window(DIGEST_LOCAL_HOUR))

    def test_an_unknown_timezone_falls_back_to_utc(self) -> None:
        """A corrupt value should still get a digest at a defensible hour rather
        than crashing the run for everyone else."""
        assert is_due("Not/AZone", *_window(DIGEST_LOCAL_HOUR))

    def test_a_missing_timezone_falls_back_to_utc(self) -> None:
        assert is_due(None, *_window(DIGEST_LOCAL_HOUR))

    def test_each_zone_is_due_exactly_once_a_day(self) -> None:
        """The property that matters most: across a full day of hourly windows,
        every zone fires exactly once. A comparison that fired twice would
        double-send; one that never fired would silently skip the user."""
        for zone in ("UTC", "Asia/Manila", "Asia/Kolkata", "Asia/Kathmandu"):
            hits = sum(1 for hour in range(24) if is_due(zone, *_window(hour)))
            assert hits == 1, f"{zone} fired {hits} times in 24 hours"

    def test_a_dst_boundary_does_not_double_send(self) -> None:
        """America/New_York springs forward on 2026-03-08. Across that day's
        windows the zone must still fire exactly once — the case where two local
        hours collapse onto one UTC instant."""
        hits = sum(
            1
            for hour in range(24)
            if is_due("America/New_York", *_window(hour, day=8, month=3))
        )
        assert hits == 1, f"fired {hits} times across the DST boundary"


class TestStaging:
    async def test_stages_a_message_for_a_due_user(
        self, db: AsyncSession, make_user
    ) -> None:
        user = await make_user("due@example.com")
        user.timezone = "UTC"
        await db.commit()

        start, end = _window(DIGEST_LOCAL_HOUR)
        staged = await stage_digests(db, window_start=start, window_end=end)

        assert staged == 1
        messages = (await db.execute(select(OutboxMessage))).scalars().all()
        assert [m.event_name for m in messages] == ["notifications.digest"]

    async def test_skips_a_user_whose_hour_has_not_arrived(
        self, db: AsyncSession, make_user
    ) -> None:
        user = await make_user("later@example.com")
        user.timezone = "UTC"
        await db.commit()
        start, end = _window(DIGEST_LOCAL_HOUR + 3)

        assert await stage_digests(db, window_start=start, window_end=end) == 0

    async def test_running_twice_stages_one_digest(
        self, db: AsyncSession, make_user
    ) -> None:
        """The retry guarantee. EventBridge re-invokes a failed run, so without
        the digest_runs constraint every user it already reached would be
        mailed a second time."""
        user = await make_user("once@example.com")
        user.timezone = "UTC"
        await db.commit()
        start, end = _window(DIGEST_LOCAL_HOUR)

        first = await stage_digests(db, window_start=start, window_end=end)
        second = await stage_digests(db, window_start=start, window_end=end)

        assert (first, second) == (1, 0)
        messages = (await db.execute(select(OutboxMessage))).scalars().all()
        assert len(messages) == 1

    async def test_a_later_period_stages_again(
        self, db: AsyncSession, make_user
    ) -> None:
        """Idempotency is per period, not forever — tomorrow's digest must still
        go out."""
        user = await make_user("daily@example.com")
        user.timezone = "UTC"
        await db.commit()
        start, end = _window(DIGEST_LOCAL_HOUR)

        await stage_digests(db, window_start=start, window_end=end)
        await stage_digests(
            db,
            window_start=start + timedelta(days=1),
            window_end=end + timedelta(days=1),
        )

        runs = (await db.execute(select(DigestRun))).scalars().all()
        assert len(runs) == 2

    async def test_a_deleted_user_gets_nothing(
        self, db: AsyncSession, make_user
    ) -> None:
        from datetime import datetime as _dt

        user = await make_user("gone@example.com")
        user.timezone = "UTC"
        user.deleted_at = _dt.now(UTC)
        await db.commit()
        start, end = _window(DIGEST_LOCAL_HOUR)

        assert await stage_digests(db, window_start=start, window_end=end) == 0


class TestPaging:
    """The digest loads users in pages rather than all at once.

    The unpaged version works until it does not: it holds the whole users table
    in memory and, with a commit per user, spends one network round trip each.
    Neither shows up in a test suite with three users, which is exactly why
    these assert the mechanism rather than the outcome.
    """

    async def test_stages_every_user_across_multiple_pages(
        self, db: AsyncSession, make_user, monkeypatch
    ) -> None:
        """With the page size forced below the user count, a naive loop that
        forgot to advance its cursor would stage only the first page — or spin
        forever on it."""
        import scripts.send_digests as digests

        monkeypatch.setattr(digests, "USER_PAGE_SIZE", 2)
        for index in range(5):
            user = await make_user(f"paged{index}@example.com")
            user.timezone = "UTC"
        await db.commit()
        start, end = _window(DIGEST_LOCAL_HOUR)

        staged = await digests.stage_digests(db, window_start=start, window_end=end)

        assert staged == 5

    async def test_paging_is_keyset_not_offset(self) -> None:
        """An OFFSET scan re-reads and discards every preceding row, so page N
        costs O(N) and the job degrades quadratically on precisely the table
        that grew large enough to need paging. Ordering by the primary key and
        asking for rows after the last id seen stays flat."""
        import inspect

        import scripts.send_digests as digests

        source = inspect.getsource(digests.stage_digests)
        assert ".offset(" not in source, "use keyset paging, not OFFSET"
        assert "User.id > after" in source

    async def test_a_retried_run_across_pages_stages_nothing_new(
        self, db: AsyncSession, make_user, monkeypatch
    ) -> None:
        """The page-level savepoint has to survive a collision on every page,
        not just the first."""
        import scripts.send_digests as digests

        monkeypatch.setattr(digests, "USER_PAGE_SIZE", 2)
        for index in range(5):
            user = await make_user(f"retry{index}@example.com")
            user.timezone = "UTC"
        await db.commit()
        start, end = _window(DIGEST_LOCAL_HOUR)

        first = await digests.stage_digests(db, window_start=start, window_end=end)
        second = await digests.stage_digests(db, window_start=start, window_end=end)

        assert (first, second) == (5, 0)


class TestTheWindowIsHonoured:
    """The job must use the window it was given, not one it invents.

    A regression test. An earlier refactor had ``_stage_page`` recompute the
    window as a fixed hour, so ``--window-hours 24`` silently staged only the
    users whose local send-hour fell in the first hour of it — and reported
    "Staged 0" as though nothing were due. Nothing errored, and every existing
    test passed, because they all used a one-hour window.
    """

    async def test_a_multi_hour_window_stages_users_across_all_of_it(
        self, db: AsyncSession, make_user
    ) -> None:
        from datetime import timedelta

        import scripts.send_digests as digests

        user = await make_user("wide@example.com")
        user.timezone = "UTC"
        await db.commit()

        # A 24-hour window: the user's 09:00 local falls inside it, but NOT in
        # its first hour. A job that recomputed the window would find nobody.
        start = datetime(2026, 6, 1, 0, tzinfo=UTC)
        staged = await digests.stage_digests(
            db, window_start=start, window_end=start + timedelta(hours=24)
        )

        assert staged == 1
