"""Emergency contact management logic (US-STU-002, BR-AUTH-007, AC-102.5)."""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_api_student.domains.student_profile.exceptions import (
    ProfileValidationError,
)
from cbpupsis_api_student.domains.student_profile.schemas import (
    EmergencyContactDetailRead,
)
from cbpupsis_api_student.domains.student_profile.service._common import (
    stage_audit_row,
    validate_address,
    validate_email,
    validate_philippine_mobile,
)
from cbpupsis_shared.domains.auth.exceptions import ResourceNotFoundError
from cbpupsis_shared.domains.users import client as users_client

logger = logging.getLogger(__name__)


def _to_contact_detail(
    contact: users_client.EmergencyContactRead,
) -> EmergencyContactDetailRead:
    return EmergencyContactDetailRead(
        id=contact.id,
        name=contact.contact_name,
        relationship=contact.relationship,
        phone=contact.phone_number,
        email=contact.email,
        address=contact.address,
        isPrimary=contact.is_primary,
    )


def _validate_contact_payload(
    payload: dict[str, Any], *, partial: bool
) -> tuple[dict[str, Any], dict[str, str]]:
    errors: dict[str, str] = {}
    cleaned: dict[str, Any] = {}

    name_key = "contact_name" if "contact_name" in payload else "name"
    phone_key = "phone_number" if "phone_number" in payload else "phone"
    primary_key = "is_primary" if "is_primary" in payload else "isPrimary"

    if not partial or name_key in payload:
        raw_name = payload.get(name_key)
        if not isinstance(raw_name, str) or not raw_name.strip():
            errors["name"] = "Contact name is required."
        else:
            cleaned["contact_name"] = raw_name.strip()[:150]

    if not partial or "relationship" in payload:
        raw_rel = payload.get("relationship")
        if not isinstance(raw_rel, str) or not raw_rel.strip():
            errors["relationship"] = "Relationship is required."
        else:
            cleaned["relationship"] = raw_rel.strip()[:50]

    if not partial or phone_key in payload:
        valid_phone = validate_philippine_mobile(payload.get(phone_key))
        if valid_phone is None:
            errors["phone"] = (
                "Expected Philippine mobile format (+639XXXXXXXXX or 09XXXXXXXXX)."
            )
        else:
            cleaned["phone_number"] = valid_phone

    if "email" in payload or "emailAddress" in payload:
        email_key = "emailAddress" if "emailAddress" in payload else "email"
        raw_email = payload.get(email_key)
        if raw_email is None or (isinstance(raw_email, str) and not raw_email.strip()):
            cleaned["email"] = None
        else:
            valid_email = validate_email(raw_email)
            if valid_email is None:
                errors[email_key] = (
                    "Expected a valid email address (e.g. name@example.com)."
                )
            else:
                cleaned["email"] = valid_email

    if "address" in payload:
        raw_addr = payload.get("address")
        if raw_addr is None or (isinstance(raw_addr, str) and not raw_addr.strip()):
            cleaned["address"] = None
        else:
            valid_addr = validate_address(raw_addr)
            if valid_addr is None:
                errors["address"] = "Expected a valid mailing address."
            else:
                cleaned["address"] = valid_addr

    if primary_key in payload:
        raw_primary = payload.get(primary_key)
        if not isinstance(raw_primary, bool):
            errors["isPrimary"] = "Expected a boolean flag."
        else:
            cleaned["is_primary"] = raw_primary

    return cleaned, errors


async def list_emergency_contacts(
    db: AsyncSession,
    *,
    actor_id: uuid.UUID,
) -> list[EmergencyContactDetailRead]:
    """Return full emergency-contact details for the dedicated contact view."""
    contacts = await users_client.list_emergency_contacts(db, actor_id)
    return [_to_contact_detail(c) for c in contacts]


async def create_emergency_contact(
    db: AsyncSession,
    *,
    actor_id: uuid.UUID,
    payload: dict[str, Any],
    ip_address: str | None = None,
) -> EmergencyContactDetailRead:
    """Create a new emergency contact and record an audit entry (AC-102.5)."""
    cleaned, errors = _validate_contact_payload(payload, partial=False)
    if errors:
        raise ProfileValidationError(errors)

    try:
        contact = await users_client.stage_create_emergency_contact(
            db,
            user_id=actor_id,
            contact_name=cleaned["contact_name"],
            relationship=cleaned["relationship"],
            phone_number=cleaned["phone_number"],
            email=cleaned.get("email"),
            address=cleaned.get("address"),
            is_primary=cleaned.get("is_primary", False),
        )
        detail = _to_contact_detail(contact)
        stage_audit_row(
            db,
            action="emergency_contact.created",
            actor_id=actor_id,
            target_type="emergency_contact",
            target_id=str(contact.id),
            field="emergencyContact",
            prior_value=None,
            new_value=detail.model_dump(mode="json"),
            ip_address=ip_address,
        )
        await db.commit()
    except Exception:
        await db.rollback()
        raise

    detail.message = "Emergency contact created successfully."
    return detail


async def update_emergency_contact(
    db: AsyncSession,
    *,
    actor_id: uuid.UUID,
    contact_id: uuid.UUID,
    payload: dict[str, Any],
    ip_address: str | None = None,
) -> EmergencyContactDetailRead:
    """Update an emergency contact owned by the student (AC-102.5)."""
    existing = await users_client.get_emergency_contact(db, contact_id)
    if existing is None or existing.user_id != actor_id:
        if existing is not None:
            logger.warning(
                "student_profile.contact_ownership_refused",
                extra={"actor_id": str(actor_id), "contact_id": str(contact_id)},
            )
        raise ResourceNotFoundError

    cleaned, errors = _validate_contact_payload(payload, partial=True)
    if not cleaned and not errors:
        errors["contact"] = "Provide at least one field to update."
    if errors:
        raise ProfileValidationError(errors)

    prior_snapshot = _to_contact_detail(existing).model_dump(mode="json")
    try:
        updated = await users_client.stage_update_emergency_contact(
            db, contact_id, **cleaned
        )
        if updated is None:
            raise ResourceNotFoundError
        detail = _to_contact_detail(updated)
        stage_audit_row(
            db,
            action="emergency_contact.updated",
            actor_id=actor_id,
            target_type="emergency_contact",
            target_id=str(contact_id),
            field="emergencyContact",
            prior_value=prior_snapshot,
            new_value=detail.model_dump(mode="json"),
            ip_address=ip_address,
        )
        await db.commit()
    except Exception:
        await db.rollback()
        raise

    detail.message = "Emergency contact updated successfully."
    return detail


async def delete_emergency_contact(
    db: AsyncSession,
    *,
    actor_id: uuid.UUID,
    contact_id: uuid.UUID,
    ip_address: str | None = None,
) -> None:
    """Delete an emergency contact owned by the student (AC-102.5)."""
    existing = await users_client.get_emergency_contact(db, contact_id)
    if existing is None or existing.user_id != actor_id:
        if existing is not None:
            logger.warning(
                "student_profile.contact_ownership_refused",
                extra={"actor_id": str(actor_id), "contact_id": str(contact_id)},
            )
        raise ResourceNotFoundError

    prior_snapshot = _to_contact_detail(existing).model_dump(mode="json")
    try:
        await users_client.stage_delete_emergency_contact(db, contact_id)
        stage_audit_row(
            db,
            action="emergency_contact.deleted",
            actor_id=actor_id,
            target_type="emergency_contact",
            target_id=str(contact_id),
            field="emergencyContact",
            prior_value=prior_snapshot,
            new_value=None,
            ip_address=ip_address,
        )
        await db.commit()
    except Exception:
        await db.rollback()
        raise
