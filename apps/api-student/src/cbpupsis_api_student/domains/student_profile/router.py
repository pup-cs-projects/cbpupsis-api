"""HTTP routing for student profile self-service (US-STU-002)."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import (
    APIRouter,
    Body,
    Depends,
    File,
    Request,
    Response,
    UploadFile,
    status,
)
from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_api_student.domains.student_auth.dependencies import (
    require_student_session,
)
from cbpupsis_api_student.domains.student_profile import service
from cbpupsis_api_student.domains.student_profile.constants import AVATAR_MAX_BYTES
from cbpupsis_api_student.domains.student_profile.schemas import (
    AvatarUploadRead,
    EmergencyContactDetailRead,
    StudentProfileRead,
)
from cbpupsis_core.middleware import client_ip_or_none
from cbpupsis_database.session import get_db
from cbpupsis_shared.domains.auth.dependencies import CurrentUser

router = APIRouter()


@router.get(
    "",
    response_model=StudentProfileRead,
    operation_id="get_student_profile",
    summary="Get Student Profile",
)
async def get_student_profile(
    user: CurrentUser = Depends(require_student_session),
    db: AsyncSession = Depends(get_db),
) -> StudentProfileRead:
    """Return the signed-in student's full profile and minimised emergency contacts."""
    return await service.get_profile(db, actor_id=user.id)


@router.patch(
    "",
    response_model=StudentProfileRead,
    operation_id="update_student_profile",
    summary="Update Student Profile",
)
async def update_student_profile(
    request: Request,
    payload: dict[str, Any] = Body(default_factory=dict),
    user: CurrentUser = Depends(require_student_session),
    db: AsyncSession = Depends(get_db),
) -> StudentProfileRead:
    """Update editable contact fields (phone, email, addresses).

    Returns 403 PROFILE_FIELD_LOCKED if any locked master field is submitted.
    Returns 422 VALIDATION_ERROR if phone or email format is invalid.
    On success, includes a human-readable confirmation message.
    """
    return await service.update_profile(
        db,
        actor_id=user.id,
        payload=payload,
        ip_address=client_ip_or_none(request),
    )


@router.post(
    "/avatar",
    response_model=AvatarUploadRead,
    status_code=status.HTTP_201_CREATED,
    operation_id="upload_student_avatar",
    summary="Upload Student Avatar",
)
async def upload_student_avatar(
    request: Request,
    file: UploadFile = File(description="JPEG or PNG profile picture up to 5 MB"),
    user: CurrentUser = Depends(require_student_session),
    db: AsyncSession = Depends(get_db),
) -> AvatarUploadRead:
    """Upload a JPEG or PNG profile picture after magic-byte and malware checks.

    Returns 413 FILE_TOO_LARGE if file exceeds 5 MB.
    Returns 415 UNSUPPORTED_MEDIA_TYPE if content is not JPEG/PNG or is malicious.
    On success, returns the avatar URL and a confirmation message.
    """
    content = await file.read(AVATAR_MAX_BYTES + 1)
    return await service.upload_avatar(
        db,
        actor_id=user.id,
        content=content,
        ip_address=client_ip_or_none(request),
    )


@router.get(
    "/avatar/{avatar_id}",
    operation_id="get_student_avatar",
    summary="Get Student Avatar Image",
    responses={
        200: {
            "content": {"image/jpeg": {}, "image/png": {}},
            "description": "Binary image stream of the profile picture.",
        },
        404: {"description": "Avatar not found."},
    },
)
async def get_student_avatar(
    avatar_id: uuid.UUID,
    _user: CurrentUser = Depends(require_student_session),
) -> Response:
    """Stream the student profile picture by avatar ID (referenced by avatarUrl)."""
    content, content_type = service.get_avatar_bytes(avatar_id)
    return Response(
        content=content,
        media_type=content_type,
        headers={
            "Content-Disposition": "inline",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get(
    "/emergency-contacts",
    response_model=list[EmergencyContactDetailRead],
    operation_id="list_student_emergency_contacts",
    summary="List Emergency Contacts",
)
async def list_student_emergency_contacts(
    user: CurrentUser = Depends(require_student_session),
    db: AsyncSession = Depends(get_db),
) -> list[EmergencyContactDetailRead]:
    """List emergency contacts with full details (including email and address)."""
    return await service.list_emergency_contacts(db, actor_id=user.id)


@router.post(
    "/emergency-contacts",
    response_model=EmergencyContactDetailRead,
    status_code=status.HTTP_201_CREATED,
    operation_id="create_student_emergency_contact",
    summary="Create Emergency Contact",
)
async def create_student_emergency_contact(
    request: Request,
    payload: dict[str, Any] = Body(default_factory=dict),
    user: CurrentUser = Depends(require_student_session),
    db: AsyncSession = Depends(get_db),
) -> EmergencyContactDetailRead:
    """Create a new emergency contact while maintaining one primary contact.

    Returns 422 VALIDATION_ERROR if name, relationship, or phone format is invalid.
    On success, returns the created contact and a confirmation message.
    """
    return await service.create_emergency_contact(
        db,
        actor_id=user.id,
        payload=payload,
        ip_address=client_ip_or_none(request),
    )


@router.patch(
    "/emergency-contacts/{contact_id}",
    response_model=EmergencyContactDetailRead,
    operation_id="update_student_emergency_contact",
    summary="Update Emergency Contact",
)
async def update_student_emergency_contact(
    contact_id: uuid.UUID,
    request: Request,
    payload: dict[str, Any] = Body(default_factory=dict),
    user: CurrentUser = Depends(require_student_session),
    db: AsyncSession = Depends(get_db),
) -> EmergencyContactDetailRead:
    """Update an emergency contact owned by the student.

    Returns 404 RESOURCE_NOT_FOUND if not owned by the student.
    Returns 422 VALIDATION_ERROR if phone, email, or address format is invalid.
    On success, returns the updated contact and a confirmation message.
    """
    return await service.update_emergency_contact(
        db,
        actor_id=user.id,
        contact_id=contact_id,
        payload=payload,
        ip_address=client_ip_or_none(request),
    )


@router.delete(
    "/emergency-contacts/{contact_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    operation_id="delete_student_emergency_contact",
    summary="Delete Emergency Contact",
)
async def delete_student_emergency_contact(
    contact_id: uuid.UUID,
    request: Request,
    response: Response,
    user: CurrentUser = Depends(require_student_session),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Delete an emergency contact owned by the student.

    Returns 404 RESOURCE_NOT_FOUND if not owned by the student.
    """
    await service.delete_emergency_contact(
        db,
        actor_id=user.id,
        contact_id=contact_id,
        ip_address=client_ip_or_none(request),
    )
    response.headers["X-Message"] = "Emergency contact deleted successfully."
