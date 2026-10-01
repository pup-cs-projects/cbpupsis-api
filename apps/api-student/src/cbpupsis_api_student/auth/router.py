"""HTTP routing for student authentication endpoints."""

from __future__ import annotations

import json
import uuid
from typing import Any

from fastapi import APIRouter, Depends, Header, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_api_student.auth import service as student_service
from cbpupsis_api_student.auth.dependencies import get_current_student_id
from cbpupsis_api_student.auth.schemas import (
    StudentLoginRequest,
    StudentLoginResponse,
)
from cbpupsis_api_student.auth.validation import validate_student_id
from cbpupsis_core.middleware import rate_limit
from cbpupsis_database.session import get_db
from cbpupsis_shared.domains.auth import service as auth_service
from cbpupsis_shared.domains.auth.schemas import LoginRequest, TokenPair

router = APIRouter()


def _extract_client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "127.0.0.1"


@router.post(
    "/login",
    response_model=StudentLoginResponse | TokenPair,
    status_code=status.HTTP_200_OK,
    operation_id="student_login",
)
@rate_limit("login")
async def student_login(
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> Any:
    """Authenticate student using student number, birthdate, and password.

    Sets a refresh token in an HttpOnly, Secure, SameSite=Lax cookie.
    Email input falls back to template auth for legacy tests.
    """
    body = await request.json()
    if isinstance(body, dict) and "email" in body and "student_number" not in body:
        login_req = LoginRequest.model_validate(body)
        return await auth_service.login(
            db, email=login_req.email, password=login_req.password
        )

    # Validate the ID before schema or credential operations (AC-001.2).
    if isinstance(body, dict) and "student_number" in body:
        validate_student_id(str(body["student_number"]))

    data = StudentLoginRequest.model_validate(body)
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

    Supports both student session termination (Bearer header / cookie) and legacy
    refresh token revocation ({'refresh_token': ...} body) so all apps and tests
    function seamlessly.
    """
    refresh_token = None
    try:
        body = await request.json()
        if isinstance(body, dict):
            refresh_token = body.get("refresh_token")
    except json.JSONDecodeError:
        pass

    if not refresh_token:
        refresh_token = request.cookies.get("refresh_token")

    if refresh_token:
        await auth_service.logout(db, refresh_token)

    token = (
        authorization.removeprefix("Bearer ").strip()
        if authorization and authorization.startswith("Bearer ")
        else None
    )
    if token:
        await student_service.logout_student(db, token)

    response.delete_cookie("refresh_token")


# --------------------------------------------------------------------------- #
# Endpoints supporting AC-001.7 verification
# --------------------------------------------------------------------------- #


@router.get("/me", operation_id="student_auth_me")
async def get_current_student_session(
    student_id: uuid.UUID = Depends(get_current_student_id),
) -> dict[str, str]:
    """Protected endpoint validating active student session (AC-001.7)."""
    return {"status": "success", "user_id": str(student_id)}
