"""Register the auth domain's mail handlers with the durable outbox worker."""

from __future__ import annotations

from cbpupsis_shared import worker
from cbpupsis_shared.domains.auth.delivery import (
    deliver_reset_email,
    deliver_temporary_password_email,
    deliver_verification_email,
)
from cbpupsis_shared.domains.auth.service import TEMPORARY_PASSWORD_EVENT


def register_auth_subscribers() -> None:
    """Idempotent registration in the worker process."""
    worker.register_handler("auth.password_reset_email", deliver_reset_email)
    worker.register_handler("auth.verification_email", deliver_verification_email)
    worker.register_handler(TEMPORARY_PASSWORD_EVENT, deliver_temporary_password_email)
