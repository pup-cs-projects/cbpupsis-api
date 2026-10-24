"""Bounded student-owned Google reconciliation, separate from the shared outbox."""

import argparse
import asyncio
import logging

import httpx

from cbpupsis_api_student.domains.google_calendar import repository, service
from cbpupsis_api_student.domains.google_calendar.provider import GoogleProvider
from cbpupsis_core.config import settings
from cbpupsis_core.logging import configure_logging
from cbpupsis_database.runtime import validate_runtime_engine
from cbpupsis_database.session import AsyncSessionLocal, engine


async def run(*, once=False, max_accounts=50):
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
                    async with AsyncSessionLocal() as db:
                        await repository.prune_states(db, service.now())
                        await db.commit()
                        if not await service.run_due(db, provider):
                            break
                        processed += 1
                logging.getLogger(__name__).info(
                    "calendar_sync.tick", extra={"processed": processed}
                )
                if once:
                    return
                await asyncio.sleep(30)
    finally:
        await engine.dispose()


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
