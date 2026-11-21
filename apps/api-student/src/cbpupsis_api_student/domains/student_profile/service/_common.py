"""Internal helpers and audit staging for the student_profile service package."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any

from pydantic import EmailStr, TypeAdapter
from pydantic import ValidationError as SchemaValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_api_student.domains.student_profile.constants import (
    EMAIL_SHAPE_PATTERN,
    MANILA_TZ,
    PHILIPPINE_MOBILE_PATTERN,
)
from cbpupsis_shared.domains.audit import client as audit_client

logger = logging.getLogger(__name__)

_EMAIL_ADAPTER = TypeAdapter(EmailStr)


def validate_email(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if not cleaned or len(cleaned) > 255 or not EMAIL_SHAPE_PATTERN.match(cleaned):
        return None
    try:
        return str(_EMAIL_ADAPTER.validate_python(cleaned)).lower()
    except SchemaValidationError:
        return None


def validate_philippine_mobile(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if not PHILIPPINE_MOBILE_PATTERN.match(cleaned):
        return None
    return cleaned


def validate_address(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if not cleaned or len(cleaned) > 1000:
        return None
    return cleaned


def stage_audit_row(
    db: AsyncSession,
    *,
    action: str,
    actor_id: uuid.UUID,
    target_type: str,
    target_id: str,
    field: str,
    prior_value: Any,
    new_value: Any,
    ip_address: str | None,
) -> None:
    """Stage one append-only audit entry in UTC+8 (AC-102.8, BR-AUTH-006)."""
    occurred_at = datetime.now(MANILA_TZ)
    audit_client.stage(
        db,
        action=action,
        actor_id=actor_id,
        target_type=target_type,
        target_id=target_id,
        occurred_at=occurred_at,
        prior_state={field: prior_value},
        new_state={field: new_value},
        payload={
            "field": field,
            "prior_value": prior_value,
            "new_value": new_value,
            "actor": str(actor_id),
            "timestamp": occurred_at.isoformat(),
            "ip_address": ip_address,
        },
    )
