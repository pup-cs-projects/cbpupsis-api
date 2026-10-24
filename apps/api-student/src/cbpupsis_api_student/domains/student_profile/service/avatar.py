"""Avatar upload, inspection, and streaming business logic (AC-102.6, AC-102.7)."""

from __future__ import annotations

import logging
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_api_student.domains.student_profile.constants import (
    AVATAR_MAX_BYTES,
    JPEG_MAGIC_PREFIX,
    MALWARE_SIGNATURES,
    PNG_MAGIC_PREFIX,
    SCRIPT_PREFIXES,
)
from cbpupsis_api_student.domains.student_profile.exceptions import (
    AvatarFileTooLargeError,
    AvatarUnsupportedMediaTypeError,
)
from cbpupsis_api_student.domains.student_profile.schemas import (
    AvatarUploadRead,
)
from cbpupsis_api_student.domains.student_profile.service._common import (
    stage_audit_row,
)
from cbpupsis_shared.domains.auth.exceptions import ResourceNotFoundError
from cbpupsis_shared.domains.users import client as users_client

logger = logging.getLogger(__name__)


def inspect_image_content_type(content: bytes) -> str:
    """Determine JPEG or PNG content type by magic-byte and structure inspection.

    Never trusts the filename extension or client-supplied Content-Type header
    (AC-102.6, AC-102.7).
    """
    if (
        len(content) >= 4
        and content.startswith(JPEG_MAGIC_PREFIX)
        and (content[3] == 0xFF or (0xC0 <= content[3] <= 0xFE))
    ):
        return "image/jpeg"
    if (
        len(content) >= 24
        and content.startswith(PNG_MAGIC_PREFIX)
        and content[12:16] == b"IHDR"
    ):
        return "image/png"
    raise AvatarUnsupportedMediaTypeError


def scan_avatar_bytes(content: bytes) -> None:
    """Scan uploaded avatar bytes for malware or embedded script payloads."""
    for prefix in SCRIPT_PREFIXES:
        if content.startswith(prefix):
            raise AvatarUnsupportedMediaTypeError

    lowered = content.lower()
    for sig in MALWARE_SIGNATURES:
        if sig.lower() in lowered:
            raise AvatarUnsupportedMediaTypeError


async def upload_avatar(
    db: AsyncSession,
    *,
    actor_id: uuid.UUID,
    content: bytes,
    ip_address: str | None = None,
) -> AvatarUploadRead:
    """Inspect, scan, and store a student's profile picture (AC-102.6, AC-102.7)."""
    if len(content) > AVATAR_MAX_BYTES:
        raise AvatarFileTooLargeError
    if not content:
        raise AvatarUnsupportedMediaTypeError

    content_type = inspect_image_content_type(content)
    scan_avatar_bytes(content)

    user_profile = await users_client.get_user_profile(db, actor_id)
    student_profile = await users_client.get_student_profile(db, actor_id)
    if user_profile is None or student_profile is None:
        raise ResourceNotFoundError

    prior_avatar = user_profile.avatar_url

    try:
        avatar = await users_client.stage_replace_avatar(
            db, actor_id, content=content, content_type=content_type
        )
        avatar_url = f"/api/v1/student/profile/avatar/{avatar.id}"
        await users_client.stage_update_user_profile(
            db, actor_id, avatar_url=avatar_url
        )
        stage_audit_row(
            db,
            action="student_profile.avatar_uploaded",
            actor_id=actor_id,
            target_type="student_profile",
            target_id=str(actor_id),
            field="avatarUrl",
            prior_value=prior_avatar,
            new_value=avatar_url,
            ip_address=ip_address,
        )
        await db.commit()
    except Exception:
        await db.rollback()
        raise

    return AvatarUploadRead(
        avatarUrl=avatar_url,
        url=avatar_url,
        contentType=content_type,
        sizeBytes=len(content),
    )


async def get_avatar_bytes(
    db: AsyncSession, *, actor_id: uuid.UUID, avatar_id: uuid.UUID
) -> tuple[bytes, str]:
    """Return the actor's own stored, scanned avatar image by id, or raise 404.

    Another student's picture answers 404 like a missing one, so an id cannot
    be used to tell whether someone else has a picture (RA 10173).
    """
    avatar = await users_client.get_avatar(db, avatar_id)
    if avatar is None or avatar.user_id != actor_id:
        raise ResourceNotFoundError
    scan_avatar_bytes(avatar.content)
    return avatar.content, avatar.content_type
