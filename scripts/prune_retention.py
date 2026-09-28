"""Delete the rows nothing needs any more, in bounded batches.

Run on a schedule — the same EventBridge Scheduler that fires the digests, so
there is one scheduled-task pattern rather than two::

    uv run python -m scripts.prune_retention

**Why this exists.** The outbox is a queue that keeps its history: every message
ever delivered stays as a ``dispatched`` row. Without pruning it becomes the
largest table in the database, the claim index carries millions of rows nobody
will ever look at, and autovacuum falls behind on exactly the table the worker
polls. That degrades slowly and silently, which is the worst way for a thing to
degrade.

**Why batched.** An unbounded ``DELETE`` takes a long-lived lock and can time out
on precisely the table big enough to need pruning — so the first run after
neglecting this is the one most likely to fail. Each statement deletes at most
``retention_batch_size`` rows and the loop repeats until a pass deletes nothing,
letting other work interleave.

**One ordering rule that matters.** Delivery receipts are pruned *last* and kept
*longest*. A receipt is what stops a redelivered message being sent twice;
deleting one while its message could still be retried re-opens the exact
duplicate window the receipt exists to close.

Deleting does not reclaim disk on Postgres without a vacuum. Autovacuum handles
that for a steadily-pruned table; a first run against a long-neglected one may
want a manual ``VACUUM`` afterwards.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_core.config import settings
from cbpupsis_core.logging import configure_logging
from cbpupsis_database.session import AsyncSessionLocal
from cbpupsis_shared.domains.notifications import repository as notifications_repository
from cbpupsis_shared.outbox.store import prune_dispatched, prune_failed, prune_receipts

configure_logging()
logger = logging.getLogger(__name__)

#: A ceiling on batches per table per run, so one enormous backlog cannot make a
#: scheduled task run for hours. Whatever is left is collected next time.
MAX_BATCHES = 200


async def _prune_in_batches(
    db: AsyncSession,
    label: str,
    delete_batch: Callable[[], Awaitable[int]],
) -> int:
    """Call ``delete_batch`` until it deletes nothing, committing between passes.

    Committing per batch is what keeps each lock short. It also means a run that
    is interrupted has still made progress rather than rolling all of it back.
    """
    total = 0
    for _ in range(MAX_BATCHES):
        deleted = await delete_batch()
        await db.commit()
        total += deleted
        if deleted == 0:
            break
    else:
        logger.warning(
            "retention.batch_limit_reached",
            extra={"table": label, "deleted": total},
        )
    return total


async def prune(db: AsyncSession, *, now: datetime | None = None) -> dict[str, int]:
    """Prune every table with a retention window. Returns what was deleted."""
    at = now or datetime.now(UTC)
    batch = settings.retention_batch_size

    deleted = {
        "outbox_dispatched": await _prune_in_batches(
            db,
            "outbox_messages(dispatched)",
            lambda: prune_dispatched(
                db,
                older_than=at
                - timedelta(days=settings.outbox_dispatched_retention_days),
                batch_size=batch,
            ),
        ),
        "outbox_failed": await _prune_in_batches(
            db,
            "outbox_messages(failed)",
            lambda: prune_failed(
                db,
                older_than=at - timedelta(days=settings.outbox_failed_retention_days),
                batch_size=batch,
            ),
        ),
        "notifications_read": await _prune_in_batches(
            db,
            "notifications",
            lambda: notifications_repository.prune_read(
                db,
                at - timedelta(days=settings.notification_read_retention_days),
                batch,
            ),
        ),
    }

    # Receipts LAST and with the longest window: see the module docstring. A
    # receipt outliving its message is harmless; the reverse re-opens the
    # duplicate-send window.
    deleted["delivery_receipts"] = await _prune_in_batches(
        db,
        "delivery_receipts",
        lambda: prune_receipts(
            db,
            older_than=at - timedelta(days=settings.delivery_receipt_retention_days),
            batch_size=batch,
        ),
    )
    return deleted


async def main() -> dict[str, int]:
    """Entry point for the scheduled task."""
    async with AsyncSessionLocal() as db:
        deleted = await prune(db)

    logger.info("retention.pruned", extra=deleted)
    return deleted


if __name__ == "__main__":
    argparse.ArgumentParser(description=__doc__).parse_args()
    result = asyncio.run(main())
    for table, count in result.items():
        print(f"{table}: {count} row(s) deleted")
