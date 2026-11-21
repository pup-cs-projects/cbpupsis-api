"""One-shot reconciliation for Xendit attempts stranded in PENDING."""

from __future__ import annotations

import argparse
import asyncio
import logging

from cbpupsis_core.config import settings
from cbpupsis_core.logging import configure_logging
from cbpupsis_database.runtime import validate_runtime_engine
from cbpupsis_database.session import AsyncSessionLocal, engine
from cbpupsis_shared.domains.billing.client import XenditGateway, reconcile_payments


async def run(*, run_key: str, limit: int = 100) -> None:
    """Reconcile one scheduler run; retrying its run key is idempotent."""
    try:
        await validate_runtime_engine(engine, settings.audit_runtime_role)
        async with AsyncSessionLocal() as db:
            result = await reconcile_payments(
                db, run_key=run_key, gateway=XenditGateway(), limit=limit
            )
        logging.getLogger(__name__).info(
            "payment_reconciliation.completed",
            extra=result.model_dump(),
        )
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--run-key",
        required=True,
        help="Stable scheduler/event identifier; reuse it for retries.",
    )
    parser.add_argument("--limit", type=int, default=100, choices=range(1, 501))
    args = parser.parse_args()
    configure_logging()
    logging.getLogger("httpx").setLevel(logging.WARNING)
    asyncio.run(run(run_key=args.run_key, limit=args.limit))


if __name__ == "__main__":
    main()
