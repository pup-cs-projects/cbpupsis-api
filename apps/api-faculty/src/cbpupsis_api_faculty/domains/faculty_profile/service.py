"""Faculty profile view and update business logic (US-FAC-005)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_api_faculty.domains.faculty_profile.constants import (
    EDITABLE_FIELD_COLUMNS,
    EDITABLE_FIELD_LIMITS,
    LOCKED_FIELD_SET,
    MANILA_TZ,
    PETITION_ROUTE,
)
from cbpupsis_api_faculty.domains.faculty_profile.exceptions import (
    ProfileFieldLockedError,
    ProfileValidationError,
)
from cbpupsis_api_faculty.domains.faculty_profile.schemas import (
    FacultyProfileRead,
    ProfileFieldEntry,
)
from cbpupsis_shared.domains.audit import client as audit_client
from cbpupsis_shared.domains.auth.exceptions import ResourceNotFoundError
from cbpupsis_shared.domains.users import client as users_client


def _mask_audit_value(value: str | None) -> str | None:
    """Mask profile string values so audit records never mirror plaintext data."""
    if value is None:
        return None
    if len(value) <= 2:
        return "***"
    return f"{value[0]}***{value[-1]}"


def _full_name(user: Any, user_profile: Any) -> str:
    if user_profile is not None:
        parts = [
            user_profile.first_name,
            user_profile.middle_name,
            user_profile.last_name,
        ]
        joined = " ".join(part.strip() for part in parts if part and part.strip())
        if joined:
            return joined
    return (user.full_name or "").strip()


def _build_profile_read(
    user: Any, faculty_profile: Any, user_profile: Any, *, message: str | None = None
) -> FacultyProfileRead:
    return FacultyProfileRead(
        petitionRoute=PETITION_ROUTE,
        fields={
            "fullName": ProfileFieldEntry(
                value=_full_name(user, user_profile), editable=False
            ),
            "employeeId": ProfileFieldEntry(
                value=faculty_profile.employee_id, editable=False
            ),
            "department": ProfileFieldEntry(
                value=faculty_profile.department, editable=False
            ),
            "title": ProfileFieldEntry(
                value=faculty_profile.academic_rank, editable=True
            ),
            "officeLocation": ProfileFieldEntry(
                value=faculty_profile.office_location, editable=True
            ),
            "officeHours": ProfileFieldEntry(
                value=faculty_profile.office_hours, editable=True
            ),
            "qualifications": ProfileFieldEntry(
                value=faculty_profile.qualifications, editable=True
            ),
        },
        message=message,
    )


async def get_profile(db: AsyncSession, *, actor_id: uuid.UUID) -> FacultyProfileRead:
    """Return the signed-in faculty member's profile with per-field editability."""
    user = await users_client.get_active_user(db, actor_id)
    faculty_profile = await users_client.get_faculty_profile(db, actor_id)
    if user is None or faculty_profile is None or not faculty_profile.is_active:
        raise ResourceNotFoundError
    user_profile = await users_client.get_user_profile(db, actor_id)
    return _build_profile_read(user, faculty_profile, user_profile)


async def update_profile(
    db: AsyncSession,
    *,
    actor_id: uuid.UUID,
    payload: dict[str, Any],
    ip_address: str | None = None,
) -> FacultyProfileRead:
    """Validate and update editable faculty profile fields atomically."""
    raw_fields = (
        payload["fields"]
        if isinstance(payload.get("fields"), dict) and len(payload) == 1
        else payload
    )
    if not isinstance(raw_fields, dict) or not raw_fields:
        raise ProfileValidationError(
            {"profile": "Provide at least one editable field to update."}
        )

    locked = [key for key in raw_fields if key in LOCKED_FIELD_SET]
    if locked:
        raise ProfileFieldLockedError(locked)

    errors: dict[str, str] = {}
    planned_updates: list[tuple[str, str, str]] = []
    for key, raw_val in raw_fields.items():
        if key not in EDITABLE_FIELD_COLUMNS:
            errors[key] = "Unknown or non-editable field."
            continue
        val = (
            raw_val.get("value")
            if isinstance(raw_val, dict) and "value" in raw_val
            else raw_val
        )
        max_len = EDITABLE_FIELD_LIMITS[key]
        if not isinstance(val, str):
            errors[key] = "Expected a text value."
            continue
        cleaned = val.strip()
        if not cleaned:
            errors[key] = "Value must not be empty."
            continue
        if len(cleaned) > max_len:
            errors[key] = f"Value must be at most {max_len} characters."
            continue
        planned_updates.append((key, EDITABLE_FIELD_COLUMNS[key], cleaned))

    if errors:
        raise ProfileValidationError(errors)

    user = await users_client.get_active_user(db, actor_id)
    faculty_profile = await users_client.get_faculty_profile(db, actor_id)
    if user is None or faculty_profile is None or not faculty_profile.is_active:
        raise ResourceNotFoundError
    user_profile = await users_client.get_user_profile(db, actor_id)

    current_values: dict[str, str | None] = {
        "academic_rank": faculty_profile.academic_rank,
        "office_location": faculty_profile.office_location,
        "office_hours": faculty_profile.office_hours,
        "qualifications": faculty_profile.qualifications,
    }
    column_updates = {column: value for _key, column, value in planned_updates}
    occurred_at = datetime.now(MANILA_TZ)

    updated_faculty = await users_client.stage_update_faculty_profile(
        db, actor_id, **column_updates
    )
    for field_name, column_name, new_value in planned_updates:
        prior_value = current_values[column_name]
        if new_value == prior_value:
            continue
        masked_prior = _mask_audit_value(prior_value)
        masked_new = _mask_audit_value(new_value)
        audit_client.stage(
            db,
            action="faculty_profile.updated",
            actor_id=actor_id,
            target_type="faculty_profile",
            target_id=actor_id,
            occurred_at=occurred_at,
            prior_state={field_name: masked_prior},
            new_state={field_name: masked_new},
            payload={
                "field": field_name,
                "prior_value": masked_prior,
                "new_value": masked_new,
                "actor": str(actor_id),
                "timestamp": occurred_at.isoformat(),
                "ip_address": ip_address,
            },
        )
    await db.commit()

    return _build_profile_read(
        user,
        updated_faculty,
        user_profile,
        message="Profile updated successfully.",
    )
