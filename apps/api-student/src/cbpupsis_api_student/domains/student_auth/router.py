"""HTTP routing for student authentication endpoints."""

from __future__ import annotations

import json
import uuid

from fastapi import APIRouter, Depends, Header, Request, Response, status
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_api_student.domains.student_auth import service as student_service
from cbpupsis_api_student.domains.student_auth.dependencies import (
    get_current_student_id,
)
from cbpupsis_api_student.domains.student_auth.schemas import (
    StudentLoginRequest,
    StudentLoginResponse,
)
from cbpupsis_api_student.domains.student_auth.validation import validate_student_id
from cbpupsis_core.middleware import rate_limit
from cbpupsis_database.session import get_db

router = APIRouter()


def _extract_client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "127.0.0.1"


@router.post(
    "/login",
    response_model=StudentLoginResponse,
    status_code=status.HTTP_200_OK,
    operation_id="student_login",
)
@rate_limit("login")
async def student_login(
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> StudentLoginResponse:
    """Authenticate student using student number, birthdate, and password.

    Sets a refresh token in an HttpOnly, Secure, SameSite=Lax cookie.
    This route deliberately accepts only student credentials. Generic email/password
    authentication belongs to the shared ``/auth/login`` endpoint.
    """
    body = await request.json()

    # Validate the ID before schema or credential operations (AC-001.2).
    if isinstance(body, dict) and "student_number" in body:
        validate_student_id(str(body["student_number"]))

    try:
        data = StudentLoginRequest.model_validate(body)
    except ValidationError as exc:
        # Manual body handling lets the student-number format check run first;
        # preserve FastAPI's normal 422 contract for every other invalid body.
        raise RequestValidationError(exc.errors()) from exc
    client_ip = _extract_client_ip(request)
    user_agent = request.headers.get("user-agent")

    login_dto, raw_refresh_token = await student_service.login_student(
        db,
        data,
        client_ip=client_ip,
        user_agent=user_agent,
    )

    # Set refresh token cookie matching AC-001.1
    response.set_cookie(
        key="refresh_token",
        value=raw_refresh_token,
        httponly=True,
        secure=True,
        samesite="lax",
        max_age=30 * 24 * 3600,
        path="/",
    )

    return login_dto


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    operation_id="student_logout",
)
async def student_logout(
    request: Request,
    response: Response,
    authorization: str | None = Header(default=None),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Terminate student session server-side, revoke refresh token, and clear cookie.

    A student access token identifies the server-side idle session. Refresh
    token revocation is delegated to shared auth through the service boundary.
    """
    refresh_token = request.cookies.get("refresh_token")
    try:
        body = await request.json()
    except json.JSONDecodeError:
        body = None
    if isinstance(body, dict):
        refresh_token = body.get("refresh_token", refresh_token)

    token = (
        authorization.removeprefix("Bearer ").strip()
        if authorization and authorization.startswith("Bearer ")
        else None
    )
    await student_service.logout_student(db, token, refresh_token)

    response.delete_cookie("refresh_token", path="/")


# --------------------------------------------------------------------------- #
# Endpoints supporting AC-001.7 verification
# --------------------------------------------------------------------------- #


@router.get("/me", operation_id="student_auth_me")
async def get_current_student_session(
    student_id: uuid.UUID = Depends(get_current_student_id),
) -> dict[str, str]:
    """Protected endpoint validating active student session (AC-001.7)."""
    return {"status": "success", "user_id": str(student_id)}
