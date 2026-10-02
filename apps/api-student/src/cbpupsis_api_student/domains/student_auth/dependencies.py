"""FastAPI dependencies for student authentication and session validation."""

from __future__ import annotations

import logging
import uuid

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_api_student.domains.student_auth import service as student_service
from cbpupsis_api_student.domains.student_auth.exceptions import (
    StudentResourceNotFoundError,
)
from cbpupsis_database.session import get_db

logger = logging.getLogger(__name__)


async def get_current_student_id(
    request: Request,
    authorization: str | None = Header(default=None),
    db: AsyncSession = Depends(get_db),
) -> uuid.UUID:
    """Validate the incoming bearer token against server-side user_active_sessions.

    Enforces the 15-minute idle session timeout (AC-001.7).
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
        )

    token = authorization.removeprefix("Bearer ").strip()
    return await student_service.validate_active_student_session(db, token)


def check_student_resource_ownership(
    requested_student_id: str,
    authenticated_student_id: str,
) -> None:
    """Enforce horizontal access restriction (AC-001.8).

    Must return 404 RESOURCE_NOT_FOUND rather than 403, preventing enumeration.
    """
    if requested_student_id != authenticated_student_id:
        logger.warning(
            "student.horizontal_access_denied",
            extra={
                "requested_student_id": requested_student_id,
                "authenticated_student_id": authenticated_student_id,
            },
        )
        raise StudentResourceNotFoundError()
