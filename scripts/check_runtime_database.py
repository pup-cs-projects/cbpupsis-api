"""Read-only deployment gate using each API/worker's actual DATABASE_URL."""

from __future__ import annotations

import argparse
import asyncio

from cbpupsis_core.config import settings
from cbpupsis_database.runtime import check_database, inspect_runtime

__all__ = ["check_database", "inspect_runtime"]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", default=settings.database_url)
    parser.add_argument("--runtime-role", default=settings.audit_runtime_role)
    args = parser.parse_args()
    try:
        failures = asyncio.run(
            check_database(args.database_url, args.runtime_role or "")
        )
    except Exception as exc:
        # Drivers may include connection URLs in exceptions; never print them.
        raise SystemExit(
            f"Runtime database check failed ({type(exc).__name__})"
        ) from None
    if failures:
        raise SystemExit("Runtime database check failed:\n- " + "\n- ".join(failures))
    print("Runtime database identity, revision, grants, and audit triggers verified")


if __name__ == "__main__":
    main()
