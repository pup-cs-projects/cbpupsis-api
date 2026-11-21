"""Session-owned certificate routes and deliberately anonymous verification."""

import asyncio
import logging
import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_api_student.domains.schedule import cor_exports as exports
from cbpupsis_api_student.domains.student_auth.dependencies import (
    require_student_session,
)
from cbpupsis_core.middleware.rate_limit import client_ip, client_ip_or_none, limiter
from cbpupsis_database.session import get_db
from cbpupsis_shared.domains.auth.dependencies import CurrentUser
from cbpupsis_shared.domains.billing.client import AssessmentRead
from cbpupsis_shared.domains.enrollment import client as enrollment
from cbpupsis_shared.domains.enrollment.client import (
    CertificateMetadata,
    CertificateRead,
    IssueCertificate,
    VerificationRead,
)
from cbpupsis_shared.domains.enrollment.exceptions import CertificateUnavailable
from cbpupsis_shared.domains.iam.dependencies import require_permission

logger = logging.getLogger(__name__)


async def private_response(request: Request):
    request.state.private_response = True
    request.state.redact_validation_errors = True
    request.state.safe_error_metadata = True


router = APIRouter(dependencies=[Depends(private_response)])
public_router = APIRouter(dependencies=[Depends(private_response)])
Database = Annotated[AsyncSession, Depends(get_db)]
Student = Annotated[CurrentUser, Depends(require_student_session)]
Term = Annotated[
    uuid.UUID,
    Query(
        alias="academicTermId",
        description="Academic-term UUID, as returned by eligibility.",
    ),
]
ERRORS = {
    401: {"description": "A live student bearer session is required."},
    403: {"description": "Student role and operation permission are required."},
    409: {
        "description": (
            "COR_NOT_ENROLLED, or COR_ENROLLMENT_NOT_FINAL with "
            "details.unmet: HOLD, PREREQUISITE_VIOLATION, NO_ASSESSMENT."
        )
    },
    503: {
        "description": (
            "SERVICE_UNAVAILABLE; no partial document. "
            "Includes unavailable dependency, encryption or renderer."
        )
    },
}


@router.post(
    "/student/schedule/cor",
    status_code=201,
    response_model=CertificateMetadata,
    dependencies=[Depends(require_permission("IssueCertificate"))],
    responses=ERRORS,
)
async def issue(
    request: Request, body: IssueCertificate, student: Student, db: Database
):
    """Atomically issue; retries preserve certificate identity/token and one audit."""
    return await enrollment.issue_certificate(
        db, student.id, body.academicTermId, client_ip=client_ip_or_none(request)
    )


@router.get(
    "/student/schedule/cor",
    response_model=CertificateRead,
    dependencies=[Depends(require_permission("ReadSchedule"))],
    responses={
        **ERRORS,
        200: {
            "content": {
                "application/pdf": {"schema": {"type": "string", "format": "binary"}}
            }
        },
    },
)
async def read(
    student: Student, db: Database, term: Term, format: Literal["pdf", "json"] = "pdf"
):
    """Read immutable issued content; never issue, assess or repair enrollment."""
    dto = await enrollment.read_certificate(db, student.id, term)
    if format == "json":
        return dto
    try:
        content = await asyncio.wait_for(
            asyncio.to_thread(exports.render_pdf, dto), timeout=10
        )
    except TimeoutError as exc:
        # Renderer is an external boundary; log only type, never contents/traceback.
        logger.warning("cor.render_failed", extra={"error_type": type(exc).__name__})
        raise CertificateUnavailable from None
    if not content.startswith(b"%PDF-"):
        raise CertificateUnavailable
    return Response(
        content,
        media_type="application/pdf",
        headers={"Content-Disposition": 'attachment; filename="certificate.pdf"'},
    )


@router.get(
    "/student/assessment",
    response_model=AssessmentRead,
    dependencies=[Depends(require_permission("ReadAssessment"))],
    responses=ERRORS,
)
async def assessment(student: Student, db: Database, term: Term):
    """Read stored shared assessment only. Every amount is a decimal string."""
    return await enrollment.read_assessment(db, student.id, term)


@public_router.get(
    "/public/cor/verify/{token}",
    response_model=VerificationRead,
    responses={
        404: {"description": "RESOURCE_NOT_FOUND: unknown or malformed token."},
        429: {
            "description": (
                "rate_limited: at most ten checks/minute/IP across all tokens."
            )
        },
        503: ERRORS[503],
    },
)
@limiter.shared_limit("10/minute", scope="cor.verify", key_func=client_ip)
async def verify(request: Request, token: str, db: Database):
    """Anonymous, non-cacheable: five fields, no database IDs or assessment."""
    return await enrollment.verify_certificate(db, token)
