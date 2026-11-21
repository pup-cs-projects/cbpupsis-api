"""Stage the periodic digest for every user whose local send-hour has arrived.

Run on a schedule — EventBridge Scheduler firing a one-off ECS task, matching how
every other job here is invoked::

    uv run python -m scripts.send_digests

**Why a scheduler and not a loop in the worker.** An in-process schedule fires
once per replica: scale the worker to two tasks and everyone gets two digests.
Fixing that needs leader election — an advisory lock or an election row — which
is real code, easy to get subtly wrong, and written to replace something the
platform already provides. EventBridge launches exactly one task and has its own
retry policy and dead-letter queue.

**This job stages; it does not send.** Every message goes onto the outbox and is
delivered by the same relay as everything else, so "must not be lost" is proven
once rather than per producer.

**Timezone arithmetic, and why the obvious version is wrong.** Comparing
``local_hour == digest_hour`` on an hourly UTC cron fails two ways, both verified
against real zones:

- **Fractional offsets never match.** At UTC 03:00 it is 08:30 in Asia/Kolkata
  and 08:45 in Asia/Kathmandu, so a comparison against hour 9 is never true and
  those users silently never receive a digest. Nothing errors.
- **DST collapses two local hours onto one instant.** In America/New_York on
  2026-03-08, local 02:00 and 03:00 both map to UTC 07:00 — so depending which
  side is tested, users get two digests or none.

So this computes the UTC instant of each candidate's local send-hour with
``zoneinfo`` (which knows both) and selects users whose instant falls inside the
run's window. The ``digest_runs`` UNIQUE constraint covers the fall-back case
where an hour repeats; the window arithmetic covers spring-forward, where one
disappears.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_core.events import Event
from cbpupsis_core.logging import configure_logging
from cbpupsis_database.models.notifications import DigestRun
from cbpupsis_database.models.users import User
from cbpupsis_database.session import AsyncSessionLocal
from cbpupsis_shared.outbox import notify_wakeup, publish_transactional

configure_logging()
logger = logging.getLogger(__name__)

#: The local hour a digest is sent at. A constant rather than a setting because
#: changing it changes which window each run covers, and a half-applied change
#: across replicas would double-send or skip.
DIGEST_LOCAL_HOUR = 9

#: Fallback for a user who has set no timezone. UTC rather than a guess: a
#: digest at the wrong local hour is a minor annoyance, and inventing a
#: plausible-looking zone would hide the fact that it is unknown.
DEFAULT_TIMEZONE = "UTC"

#: Users loaded per page. Large enough that the round trips are amortised, small
#: enough that one page fits comfortably in memory and one commit stays short.
USER_PAGE_SIZE = 1000

#: Ceiling on pages per run. At the default page size that is 10M users, which
#: no schedule should reach — so hitting it means a cursor bug, not scale.
MAX_PAGES = 10_000


def is_due(
    timezone_name: str | None, window_start: datetime, window_end: datetime
) -> bool:
    """Return whether ``timezone_name``'s local send-hour falls in this window.

    Resolves the local hour to an absolute instant rather than comparing hour
    numbers, which is what makes fractional offsets and DST work. See the module
    docstring for the two failures the naive version has.

    An unknown zone name falls back to UTC rather than raising: a user with a
    corrupt timezone should still get their digest, at a defensible hour.
    """
    try:
        zone = ZoneInfo(timezone_name or DEFAULT_TIMEZONE)
    except (ZoneInfoNotFoundError, ValueError):
        zone = ZoneInfo(DEFAULT_TIMEZONE)

    local = window_start.astimezone(zone)
    # Candidate instants for the send-hour on the local days this window could
    # touch. Both are checked because a window can straddle local midnight.
    for day_offset in (-1, 0, 1):
        day = (local + timedelta(days=day_offset)).date()
        try:
            candidate = datetime(
                day.year, day.month, day.day, DIGEST_LOCAL_HOUR, tzinfo=zone
            ).astimezone(UTC)
        except ValueError:  # a local time that does not exist on this date
            continue
        if window_start <= candidate < window_end:
            return True
    return False


async def stage_digests(
    db: AsyncSession, *, window_start: datetime, window_end: datetime
) -> int:
    """Stage a digest for every due user. Returns how many were staged.

    **Paged, and committed per page.** The obvious version — load every user,
    then commit per user — has two cliffs that only appear once the product
    works: it holds the whole table in memory, and it does one round trip per
    user, so 100k users is 100k commits at network latency each.

    Paging is **keyset**, not ``OFFSET``: an offset scan re-reads and discards
    every preceding row, so page N costs O(N) and the job degrades quadratically
    on exactly the table that grew big enough to matter. Ordering by the primary
    key and asking for "the next page after this id" stays flat.

    Idempotent: a user already recorded in ``digest_runs`` for this period is
    skipped, so a retried invocation finishes the users it missed without
    re-mailing the ones it reached. The skip is enforced by the UNIQUE
    constraint rather than a prior SELECT — two concurrent runs would both see
    "not yet staged" before either wrote.
    """
    staged = 0
    after: uuid.UUID | None = None
    # A ceiling on pages, so a cursor bug fails loudly instead of looping
    # forever. Without it, forgetting to advance `after` produces a job that
    # runs until something kills it and re-stages the first page each pass —
    # a hang, which is the hardest failure to diagnose from a scheduler log.
    pages_remaining = MAX_PAGES

    while pages_remaining > 0:
        pages_remaining -= 1
        query = select(User).where(User.deleted_at.is_(None))
        if after is not None:
            query = query.where(User.id > after)
        users = (
            (await db.execute(query.order_by(User.id).limit(USER_PAGE_SIZE)))
            .scalars()
            .all()
        )
        if not users:
            break
        after = users[-1].id

        staged += await _stage_page(db, users, window_start, window_end)
    else:
        logger.warning(
            "digest.page_limit_reached",
            extra={"pages": MAX_PAGES, "staged": staged},
        )

    if staged:
        await notify_wakeup(db)
    return staged


async def _stage_page(
    db: AsyncSession,
    users: Sequence[User],
    window_start: datetime,
    window_end: datetime,
) -> int:
    """Stage one page of users, committing once. Returns how many were staged.

    One commit per page rather than per user: the same work in a fraction of the
    round trips. The cost is that a single duplicate would roll back the page,
    so the retry path below re-stages that page one user at a time — paying the
    slow path only on the rare run that actually collides.
    """
    due = [u for u in users if is_due(u.timezone, window_start, window_end)]
    if not due:
        return 0

    try:
        # A SAVEPOINT around the whole page. On success one commit covers every
        # user in it; on a collision the savepoint rolls back cleanly and the
        # session stays usable, which a bare flush-and-rollback does not give —
        # the failed objects would remain pending and re-raise on the next
        # flush.
        async with db.begin_nested():
            for user in due:
                _stage_one(db, user, window_start)
    except IntegrityError:
        # A retried run overlapping an earlier one. Fall back to per-user
        # savepoints so the users that were NOT already staged still get theirs.
        return await _stage_individually(db, due, window_start)

    await db.commit()
    return len(due)


async def _stage_individually(
    db: AsyncSession, users: Sequence[User], window_start: datetime
) -> int:
    """Re-stage a collided page one user at a time. The slow, correct path.

    Flushes per user rather than relying on the commit to surface the
    collision: SQLAlchemy emits the INSERT at flush time, so a duplicate raises
    there — a try/except around ``commit()`` alone never sees it, and the
    session is left in a state that makes the next statement fail with
    ``MissingGreenlet`` instead of anything that names the real cause.
    """
    staged = 0
    for user in users:
        # A SAVEPOINT per user: a collision rolls back only this user's two
        # rows, leaving the session usable for the next one. Without it the
        # failed INSERT stays pending on the session and re-raises on the next
        # flush, so one duplicate would abort the whole fallback.
        try:
            async with db.begin_nested():
                _stage_one(db, user, window_start)
        except IntegrityError:
            continue  # already staged for this period
        staged += 1

    await db.commit()
    return staged


def _stage_one(db: AsyncSession, user: User, window_start: datetime) -> None:
    """Stage one user's digest run and its outbox message, without committing.

    Both on the caller's session, so the ``digest_runs`` row and the message it
    guards commit together: a run recorded without its message would silently
    skip that user forever.
    """
    db.add(DigestRun(user_id=user.id, period_start=window_start))
    publish_transactional(
        db,
        Event(
            name="notifications.digest",
            payload={
                "user_id": str(user.id),
                "period_start": window_start.isoformat(),
            },
        ),
    )


async def main(window_hours: int = 1) -> int:
    """Stage digests for the window ending now."""
    window_end = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    window_start = window_end - timedelta(hours=window_hours)

    async with AsyncSessionLocal() as db:
        staged = await stage_digests(
            db, window_start=window_start, window_end=window_end
        )

    logger.info(
        "digest.staged",
        extra={
            "staged": staged,
            "window_start": window_start.isoformat(),
            "window_end": window_end.isoformat(),
        },
    )
    return staged


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--window-hours",
        type=int,
        default=1,
        help="How many hours the run covers. Match the schedule's interval.",
    )
    args = parser.parse_args()
    count = asyncio.run(main(window_hours=args.window_hours))
    print(f"Staged {count} digest(s).")
