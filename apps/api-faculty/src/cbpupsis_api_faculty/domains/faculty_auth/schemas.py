"""Pydantic schemas for faculty authentication and MFA."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class FacultyLoginRequest(BaseModel):
    """Payload submitted to POST /api/v1/auth/login."""

    identifier: str | None = Field(
        default=None,
        description="Faculty employee ID in format YYYY-NNNNN-XX-N",
        examples=["2021-00456-MN-0"],
    )
    password: str = Field(description="Account password")
    birthday: str | None = Field(default=None, description="Optional YYYY-MM-DD")
    email: str | None = Field(
        default=None,
        description="Fallback email for backward compatibility with shared suite",
    )


class FacultyLoginChallengeData(BaseModel):
    """Challenge issued when faculty credentials are valid."""

    challenge_token: str
    mfa_required: bool = True
    expires_in: int = 300
    mfa_enrolled: bool = True


class FacultyLoginResponse(BaseModel):
    """HTTP 200 response for successful first-factor credential validation."""

    status: str = "success"
    data: FacultyLoginChallengeData


class FacultyMfaVerifyRequest(BaseModel):
    """Payload submitted to POST /api/v1/auth/mfa/verify."""

    model_config = ConfigDict(populate_by_name=True)

    challenge_token: str = Field(
        ...,
        alias="challengeToken",
        description="Short-lived challenge token issued by login step",
    )
    code: str = Field(
        ...,
        alias="totp_code",
        min_length=6,
        max_length=6,
        description="Six-digit time-based code from authenticator app",
        examples=["123456"],
    )


class FacultyUserData(BaseModel):
    """User profile data returned upon successful session issuance."""

    id: str
    firstName: str
    lastName: str
    email: str
    role: str = "FACULTY"


class FacultySessionData(BaseModel):
    """Session credentials returned upon successful MFA verification."""

    accessToken: str
    tokenType: str = "Bearer"
    expiresIn: int = 1800
    role: str = "faculty"
    user: FacultyUserData


class FacultyMfaVerifyResponse(BaseModel):
    """HTTP 200 response for successful second-factor verification."""

    status: str = "success"
    data: FacultySessionData


class FacultyMfaEnrollData(BaseModel):
    """Provisioning details returned to enroll Google Authenticator."""

    provisioning_uri: str
    account_name: str
    issuer: str = "PUP CBPUPSIS"
    mfa_enrolled: bool = False


class FacultyMfaEnrollResponse(BaseModel):
    """HTTP 200 response for starting MFA enrollment."""

    status: str = "success"
    data: FacultyMfaEnrollData


class FacultyMfaConfirmRequest(BaseModel):
    """Payload to confirm TOTP code and activate MFA enrollment."""

    code: str = Field(
        ...,
        min_length=6,
        max_length=6,
        description="Six-digit code generated from newly scanned authenticator",
    )


class FacultyMfaConfirmData(BaseModel):
    """Response data on successful MFA activation."""

    recovery_codes: list[str]
    message: str = (
        "MFA enrolled successfully. Save these recovery codes in a secure location."
    )


class FacultyMfaConfirmResponse(BaseModel):
    """HTTP 200 response upon activating MFA enrollment."""

    status: str = "success"
    data: FacultyMfaConfirmData


class SectionItem(BaseModel):
    """Summary of a course section assigned to faculty."""

    id: uuid.UUID
    section_name: str
    capacity: int
    status: str


class SectionListResponse(BaseModel):
    """HTTP 200 response listing sections."""

    status: str = "success"
    data: list[SectionItem]


class SectionRosterResponse(BaseModel):
    """HTTP 200 response with section roster details."""

    status: str = "success"
    data: dict[str, Any]


class AuthAuditLogRead(BaseModel):
    """Schema for reading an audit ledger record."""

    id: uuid.UUID
    created_at: datetime
    attempted_id: str
    user_id: uuid.UUID | None
    ip_address: str | None
    user_agent: str | None
    success: bool
    mfa_used: bool
    failure_reason: str | None
