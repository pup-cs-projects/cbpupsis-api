"""Register the reset email handler with the durable outbox worker."""

from __future__ import annotations

from cbpupsis_shared import worker
from cbpupsis_shared.domains.auth.delivery import deliver_reset_email


def register_auth_subscribers() -> None:
    """Idempotent registration in the worker process."""
    worker.register_handler("auth.password_reset_email", deliver_reset_email)
