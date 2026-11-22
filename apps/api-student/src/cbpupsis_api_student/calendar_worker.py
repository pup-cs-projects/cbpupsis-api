"""Bounded student-owned Google reconciliation, separate from the shared outbox.

Locally it runs as a process (``python -m cbpupsis_api_student.calendar_worker``).
Deployed, :func:`lambda_handler` is the entry point for a scheduled one-shot run,
because a Lambda cannot host the continuous loop.
"""

import argparse
import asyncio
import logging
import time
from typing import Any

import httpx

from cbpupsis_api_student.domains.google_calendar import repository, service
from cbpupsis_api_student.domains.google_calendar.provider import GoogleProvider
from cbpupsis_core.config import settings
from cbpupsis_core.logging import configure_logging
from cbpupsis_database.runtime import validate_runtime_engine
from cbpupsis_database.session import AsyncSessionLocal, engine

logger = logging.getLogger(__name__)

#: One account may take up to 90 seconds (service.run_due), plus the engine
#: shutdown, so no new account is claimed with less than this left.
LAMBDA_MARGIN_SECONDS = 100.0


async def run(*, once=False, max_accounts=50, deadline: float | None = None) -> int:
    """Reconcile due accounts. With ``once``, return after one pass.

    ``deadline`` is a ``time.monotonic()`` value after which no further account
    is claimed, so a scheduled run stops before its time limit.
    """
    service.require_enabled()
    try:
        await validate_runtime_engine(engine, settings.audit_runtime_role)
        async with httpx.AsyncClient(
            timeout=8.0, follow_redirects=False, trust_env=False
        ) as client:
            provider = GoogleProvider(client)
            while True:
                processed = 0
                for _ in range(max_accounts):
                    if deadline is not None and time.monotonic() >= deadline:
                        break
                    async with AsyncSessionLocal() as db:
                        await repository.prune_states(db, service.now())
                        await db.commit()
                        if not await service.run_due(db, provider):
                            break
                        processed += 1
                logger.info("calendar_sync.tick", extra={"processed": processed})
                if once:
                    return processed
                await asyncio.sleep(30)
    finally:
        # Each Lambda invocation runs its own event loop, and a pooled
        # connection bound to the previous one fails.
        await engine.dispose()


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, int]:
    """AWS Lambda entry point for the scheduled (EventBridge) one-shot run.

    With the integration disabled it does nothing and succeeds, so the schedule
    and the deploy smoke test do not fail on an environment that has it off.
    """
    configure_logging()
    if not settings.google_calendar_enabled:
        logger.info("calendar_sync.disabled")
        return {"processed": 0}
    remaining = context.get_remaining_time_in_millis() / 1000
    deadline = time.monotonic() + max(remaining - LAMBDA_MARGIN_SECONDS, 0.0)
    return {"processed": asyncio.run(run(once=True, deadline=deadline))}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--max-accounts", type=int, default=50, choices=range(1, 501))
    args = parser.parse_args()
    configure_logging()
    logging.getLogger("httpx").setLevel(logging.WARNING)
    asyncio.run(run(once=args.once, max_accounts=args.max_accounts))


if __name__ == "__main__":
    main()
