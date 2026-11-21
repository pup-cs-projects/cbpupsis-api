"""Student profile view and update business logic (US-STU-002)."""

from __future__ import annotations

import logging
import sys
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_api_student.domains.student_profile.constants import (
    EDITABLE_FIELD_COLUMNS,
    LOCKED_FIELD_ALIASES,
    PETITION_ROUTE,
)
from cbpupsis_api_student.domains.student_profile.exceptions import (
    ProfileFieldLockedError,
    ProfileValidationError,
)
from cbpupsis_api_student.domains.student_profile.schemas import (
    EmergencyContactSummaryRead,
    ProfileFieldEntry,
    StudentProfileRead,
)
from cbpupsis_api_student.domains.student_profile.service._common import (
    stage_audit_row,
    validate_address,
    validate_email,
    validate_philippine_mobile,
)
from cbpupsis_shared.domains.auth.exceptions import ResourceNotFoundError
from cbpupsis_shared.domains.curriculum import client as curriculum_client
from cbpupsis_shared.domains.users import client as users_client

logger = logging.getLogger(__name__)


async def _apply_profile_field(
    db: AsyncSession, user_id: uuid.UUID, column: str, value: str
) -> None:
    """Stage a single profile column update within the caller's transaction."""
    await users_client.stage_update_user_profile(db, user_id, **{column: value})


async def get_profile(
    db: AsyncSession,
    *,
    actor_id: uuid.UUID,
) -> StudentProfileRead:
    """Return the signed-in student's profile and minimised emergency contacts."""
    user = await users_client.get_active_user(db, actor_id)
    user_profile = await users_client.get_user_profile(db, actor_id)
    student_profile = await users_client.get_student_profile(db, actor_id)
    if user is None or user_profile is None or student_profile is None:
        raise ResourceNotFoundError

    program = await curriculum_client.get_program(db, student_profile.program_id)
    curriculum = (
        await curriculum_client.get_curriculum(db, student_profile.curriculum_id)
        if student_profile.curriculum_id is not None
        else None
    )
    contacts = await users_client.list_emergency_contacts(db, actor_id)

    catalog_year = (
        curriculum.curriculum_year
        if curriculum is not None
        else student_profile.student_number[:4]
    )
    program_display = program.name if program is not None else None
    program_code = program.code if program is not None else None
    email_value = user_profile.personal_email or user.email
    permanent_address = user_profile.home_address
    current_address = user_profile.current_address or user_profile.home_address

    fields: dict[str, ProfileFieldEntry] = {
        "studentId": ProfileFieldEntry(
            value=student_profile.student_number, editable=False
        ),
        "firstName": ProfileFieldEntry(value=user_profile.first_name, editable=False),
        "middleName": ProfileFieldEntry(value=user_profile.middle_name, editable=False),
        "lastName": ProfileFieldEntry(value=user_profile.last_name, editable=False),
        "birthdate": ProfileFieldEntry(
            value=(
                user_profile.birthdate.isoformat()
                if user_profile.birthdate is not None
                else None
            ),
            editable=False,
        ),
        "gender": ProfileFieldEntry(value=user_profile.gender, editable=False),
        "civilStatus": ProfileFieldEntry(
            value=user_profile.civil_status, editable=False
        ),
        "program": ProfileFieldEntry(value=program_display, editable=False),
        "programCode": ProfileFieldEntry(value=program_code, editable=False),
        "catalogYear": ProfileFieldEntry(value=catalog_year, editable=False),
        "yearLevel": ProfileFieldEntry(
            value=student_profile.year_level, editable=False
        ),
        "enrollmentStatus": ProfileFieldEntry(
            value=student_profile.enrollment_status, editable=False
        ),
        "phone": ProfileFieldEntry(value=user_profile.phone_number, editable=True),
        "email": ProfileFieldEntry(value=email_value, editable=True),
        "emailAddress": ProfileFieldEntry(value=email_value, editable=True),
        "permanentAddress": ProfileFieldEntry(value=permanent_address, editable=True),
        "currentAddress": ProfileFieldEntry(value=current_address, editable=True),
    }

    return StudentProfileRead(
        studentId=student_profile.student_number,
        avatarUrl=user_profile.avatar_url,
        petitionRoute=PETITION_ROUTE,
        fields=fields,
        emergencyContacts=[
            EmergencyContactSummaryRead(
                id=c.id,
                name=c.contact_name,
                relationship=c.relationship,
                phone=c.phone_number,
                isPrimary=c.is_primary,
            )
            for c in contacts
        ],
    )


async def update_profile(
    db: AsyncSession,
    *,
    actor_id: uuid.UUID,
    payload: dict[str, Any],
    ip_address: str | None = None,
) -> StudentProfileRead:
    """Validate and update editable profile fields atomically (AC-102.2..102.4)."""
    raw_fields = (
        payload["fields"]
        if isinstance(payload.get("fields"), dict) and len(payload) == 1
        else payload
    )
    if not isinstance(raw_fields, dict) or not raw_fields:
        raise ProfileValidationError(
            {"profile": "Provide at least one editable field to update."}
        )

    locked = [
        LOCKED_FIELD_ALIASES[key] for key in raw_fields if key in LOCKED_FIELD_ALIASES
    ]
    if locked:
        raise ProfileFieldLockedError(locked)

    errors: dict[str, str] = {}
    planned_updates: list[tuple[str, str, str]] = []
    for key, raw_val in raw_fields.items():
        if key not in EDITABLE_FIELD_COLUMNS:
            errors[key] = "Unknown or non-editable field."
            continue
        canonical_name, column_name = EDITABLE_FIELD_COLUMNS[key]
        val = (
            raw_val.get("value")
            if isinstance(raw_val, dict) and "value" in raw_val
            else raw_val
        )
        if column_name == "phone_number":
            cleaned = validate_philippine_mobile(val)
            if cleaned is None:
                errors[key] = (
                    "Expected Philippine mobile format (+639XXXXXXXXX or 09XXXXXXXXX)."
                )
            else:
                planned_updates.append((canonical_name, column_name, cleaned))
        elif column_name == "personal_email":
            cleaned = validate_email(val)
            if cleaned is None:
                errors[key] = "Expected a valid email address (e.g. name@example.com)."
            else:
                planned_updates.append((canonical_name, column_name, cleaned))
        else:
            cleaned = validate_address(val)
            if cleaned is None:
                errors[key] = "Expected a non-empty mailing address."
            else:
                planned_updates.append((canonical_name, column_name, cleaned))

    if errors:
        raise ProfileValidationError(errors)

    user = await users_client.get_active_user(db, actor_id)
    user_profile = await users_client.get_user_profile(db, actor_id)
    student_profile = await users_client.get_student_profile(db, actor_id)
    if user is None or user_profile is None or student_profile is None:
        raise ResourceNotFoundError

    current_values: dict[str, str | None] = {
        "phone_number": user_profile.phone_number,
        "personal_email": user_profile.personal_email or user.email,
        "home_address": user_profile.home_address,
        "current_address": user_profile.current_address,
    }

    facade_module = sys.modules.get(
        "cbpupsis_api_student.domains.student_profile.service"
    )
    apply_fn = getattr(facade_module, "_apply_profile_field", _apply_profile_field)

    try:
        for canonical_name, column_name, new_value in planned_updates:
            prior_value = current_values.get(column_name)
            await apply_fn(db, actor_id, column_name, new_value)
            current_values[column_name] = new_value
            stage_audit_row(
                db,
                action="student_profile.updated",
                actor_id=actor_id,
                target_type="student_profile",
                target_id=str(actor_id),
                field=canonical_name,
                prior_value=prior_value,
                new_value=new_value,
                ip_address=ip_address,
            )
        await db.commit()
    except Exception:
        await db.rollback()
        raise

    profile = await get_profile(db, actor_id=actor_id)
    profile.message = "Profile updated successfully."
    return profile
