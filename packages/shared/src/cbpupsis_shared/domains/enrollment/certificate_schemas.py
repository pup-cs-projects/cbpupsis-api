"""One certificate DTO for immutable JSON and PDF representations."""

import uuid
from datetime import datetime, time
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from cbpupsis_shared.domains.billing.schemas import AssessmentRead


class IssueCertificate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    term: uuid.UUID = Field(
        description=(
            "Stored academic-term UUID; #168/#169 integration must share this selector."
        )
    )


class CertificateMetadata(BaseModel):
    certificateId: uuid.UUID
    issuedAt: datetime
    status: Literal["CURRENT", "SUPERSEDED"]


class CertificateTerm(BaseModel):
    id: uuid.UUID
    year: str
    semester: str


class CertificateMeeting(BaseModel):
    dayOfWeek: int = Field(ge=0, le=6)
    startTime: time
    endTime: time
    room: str | None
    type: str


class CertificateCourse(BaseModel):
    sectionId: uuid.UUID
    code: str
    title: str
    section: str
    schedule: list[CertificateMeeting]
    room: str | None
    faculty: str | None
    units: Decimal


class CertificateRead(CertificateMetadata):
    name: str
    studentNumber: str
    program: str
    term: CertificateTerm
    courses: list[CertificateCourse]
    totalUnits: Decimal
    assessment: AssessmentRead
    verificationUrl: str


class VerificationTerm(BaseModel):
    year: str
    semester: str


class VerificationRead(BaseModel):
    name: str
    studentNumber: str
    term: VerificationTerm
    issuedAt: datetime
    status: Literal["CURRENT", "SUPERSEDED"]
