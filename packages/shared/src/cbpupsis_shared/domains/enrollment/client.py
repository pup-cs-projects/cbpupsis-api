from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_shared.domains.academics.schemas import AcademicTermRead
from cbpupsis_shared.domains.enrollment import service
from cbpupsis_shared.domains.enrollment.schemas import (
    AddDropDecisionRequest,
    AddDropRequestCreate,
    AddDropRequestRead,
    AdminAddDropRequestRead,
    AvailableCourseRead,
    EnrollmentSubmitRead,
    EnrollmentSummaryRead,
    OverloadRequestFiledRead,
    OverloadRequestRead,
    TermEnrollmentRead,
)
from cbpupsis_shared.domains.enrollment.service import (
    assess_overload_eligibility,
    check_schedule_conflict,
    check_unit_ceiling,
    compute_effective_ceiling,
    enrolled_sections,
    record_enrollments,
)

__all__ = [
    "AddDropDecisionRequest",
    "AddDropRequestCreate",
    "AddDropRequestRead",
    "AdminAddDropRequestRead",
    "AvailableCourseRead",
    "EnrollmentSubmitRead",
    "EnrollmentSummaryRead",
    "OverloadRequestFiledRead",
    "OverloadRequestRead",
    "TermEnrollmentRead",
    "assess_overload_eligibility",
    "check_schedule_conflict",
    "check_unit_ceiling",
    "compute_effective_ceiling",
    "decide_add_drop_request",
    "decide_overload_request",
    "enrolled_sections",
    "file_add_drop_request",
    "file_overload_request",
    "get_approved_overload_units",
    "get_enrollment_summary",
    "get_student_add_drop_request",
    "get_term_enrollment",
    "list_admin_add_drop_requests",
    "list_available_courses",
    "list_overload_requests",
    "list_student_add_drop_requests",
    "record_enrollments",
    "submit_enrollment",
]


async def file_overload_request(
    db: AsyncSession,
    *,
    student_id: uuid.UUID,
    term: AcademicTermRead,
    requested_units: int,
    justification: str,
    role: str = "student",
    client_ip: str | None = None,
) -> OverloadRequestFiledRead:
    return await service.file_overload_request(
        db,
        student_id=student_id,
        term=term,
        requested_units=requested_units,
        justification=justification,
        role=role,
        client_ip=client_ip,
    )


async def list_overload_requests(
    db: AsyncSession,
    *,
    position: str,
    department: str | None,
    college: str | None,
) -> list[OverloadRequestRead]:
    return await service.list_overload_requests(
        db,
        position=position,
        department=department,
        college=college,
    )


async def decide_overload_request(
    db: AsyncSession,
    *,
    overload_request_id: uuid.UUID,
    decision: str,
    remarks: str | None,
    reviewer_id: uuid.UUID,
    position: str,
    department: str | None,
    college: str | None,
    client_ip: str | None = None,
) -> OverloadRequestRead:
    return await service.decide_overload_request(
        db,
        overload_request_id=overload_request_id,
        decision=decision,
        remarks=remarks,
        reviewer_id=reviewer_id,
        position=position,
        department=department,
        college=college,
        client_ip=client_ip,
    )


async def list_available_courses(
    db: AsyncSession,
    *,
    student_id: uuid.UUID,
    term: AcademicTermRead,
) -> list[AvailableCourseRead]:
    return await service.list_available_courses(db, student_id=student_id, term=term)


async def submit_enrollment(
    db: AsyncSession,
    *,
    student_id: uuid.UUID,
    term: AcademicTermRead,
    section_ids: list[uuid.UUID],
    client_ip: str | None,
    role: str = "student",
) -> EnrollmentSubmitRead:
    return await service.submit_enrollment(
        db,
        student_id=student_id,
        term=term,
        section_ids=section_ids,
        client_ip=client_ip,
        role=role,
    )


async def get_term_enrollment(
    db: AsyncSession, student_id: uuid.UUID, academic_term_id: uuid.UUID
) -> TermEnrollmentRead | None:
    return await service.get_term_enrollment(db, student_id, academic_term_id)


async def get_enrollment_summary(
    db: AsyncSession,
    *,
    student_id: uuid.UUID,
    academic_term_id: uuid.UUID,
    prescribed_load: Decimal,
    standing_reduction: Decimal,
) -> EnrollmentSummaryRead:
    return await service.get_enrollment_summary(
        db,
        student_id=student_id,
        academic_term_id=academic_term_id,
        prescribed_load=prescribed_load,
        standing_reduction=standing_reduction,
    )


async def get_approved_overload_units(
    db: AsyncSession, *, student_id: uuid.UUID, academic_term_id: uuid.UUID
) -> Decimal:
    return await service.get_approved_overload_units(
        db, student_id=student_id, academic_term_id=academic_term_id
    )


async def file_add_drop_request(
    db: AsyncSession,
    *,
    student_id: uuid.UUID,
    request_data: AddDropRequestCreate,
    client_ip: str | None = None,
) -> AddDropRequestRead:
    return await service.file_add_drop_request(
        db,
        student_id=student_id,
        request_data=request_data,
        client_ip=client_ip,
    )


async def get_student_add_drop_request(
    db: AsyncSession,
    *,
    student_id: uuid.UUID,
    request_id: uuid.UUID,
) -> AddDropRequestRead:
    return await service.get_student_add_drop_request(
        db,
        student_id=student_id,
        request_id=request_id,
    )


async def list_student_add_drop_requests(
    db: AsyncSession,
    *,
    student_id: uuid.UUID,
) -> list[AddDropRequestRead]:
    return await service.list_student_add_drop_requests(
        db,
        student_id=student_id,
    )


async def list_admin_add_drop_requests(
    db: AsyncSession,
    *,
    position: str,
    department: str | None,
    college: str | None,
) -> list[AdminAddDropRequestRead]:
    return await service.list_admin_add_drop_requests(
        db,
        position=position,
        department=department,
        college=college,
    )


async def decide_add_drop_request(
    db: AsyncSession,
    *,
    request_id: uuid.UUID,
    decision: str,
    remarks: str | None,
    reviewer_id: uuid.UUID,
    position: str,
    department: str | None,
    college: str | None,
    client_ip: str | None = None,
    version: int = 1,
) -> AdminAddDropRequestRead:
    return await service.decide_add_drop_request(
        db,
        request_id=request_id,
        decision=decision,
        remarks=remarks,
        reviewer_id=reviewer_id,
        position=position,
        department=department,
        college=college,
        client_ip=client_ip,
        version=version,
    )
