"""Daily job turning overdue Incomplete (INC) grades into 5.00 idempotently.

Run on a schedule (EventBridge Scheduler firing one task per day)::

    uv run python -m scripts.expire_incompletes
    uv run python -m scripts.expire_incompletes --run-key inc-expiry-2027-01-01
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from datetime import UTC, datetime, timedelta, timezone

from cbpupsis_core.config import settings
from cbpupsis_core.logging import configure_logging
from cbpupsis_database.runtime import validate_runtime_engine
from cbpupsis_database.session import AsyncSessionLocal, engine
from cbpupsis_shared.domains.grading.client import (
    IncExpiryRunResult,
    expire_overdue_incompletes,
)
from cbpupsis_shared.outbox import notify_wakeup

configure_logging()
logger = logging.getLogger(__name__)

MANILA = timezone(timedelta(hours=8))


def default_run_key(as_of: datetime | None = None) -> str:
    """Return the canonical ``inc-expiry-YYYY-MM-DD`` run key."""
    instant = as_of or datetime.now(UTC)
    return f"inc-expiry-{instant.astimezone(MANILA).date().isoformat()}"


async def run_inc_expiry(
    *,
    run_key: str | None = None,
    as_of: datetime | None = None,
) -> IncExpiryRunResult:
    """Validate runtime grants and execute one idempotent INC expiry sweep."""
    await validate_runtime_engine(engine, settings.audit_runtime_role)
    resolved_key = run_key or default_run_key(as_of)
    try:
        async with AsyncSessionLocal() as db:
            result = await expire_overdue_incompletes(
                db, run_key=resolved_key, as_of=as_of
            )
            if result.converted > 0:
                await notify_wakeup(db)
        logger.info("grading.inc_expiry_completed", extra=result.model_dump())
        return result
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Convert expired INC marks to 5.00 and recompute standing."
    )
    parser.add_argument(
        "--run-key",
        default=None,
        help="Optional explicit idempotency key (default inc-expiry-YYYY-MM-DD).",
    )
    parser.add_argument(
        "--as-of",
        default=None,
        help="Optional ISO-8601 reference timestamp (for deterministic backfills).",
    )
    args = parser.parse_args()
    as_of = datetime.fromisoformat(args.as_of) if args.as_of else None
    asyncio.run(run_inc_expiry(run_key=args.run_key, as_of=as_of))


if __name__ == "__main__":
    main()
