"""Internal client for the enrollment domain.

Other domains use this client rather than importing ``service.py`` directly.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_shared.domains.academics.schemas import AcademicTermRead
from cbpupsis_shared.domains.enrollment import service
from cbpupsis_shared.domains.enrollment.certificate import (
    issue_certificate,
    read_assessment,
    read_certificate,
    stage_reissue,
    verify_certificate,
)
from cbpupsis_shared.domains.enrollment.certificate_schemas import (
    CertificateMetadata,
    CertificateRead,
    IssueCertificate,
    VerificationRead,
)
from cbpupsis_shared.domains.enrollment.repository import begin_certificate_snapshot
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
    SectionRosterAddDrop,
    SectionRosterEnrollment,
    TermEnrollmentRead,
    WaiverFile,
    WaiverFiled,
    WaiverRead,
)
from cbpupsis_shared.domains.enrollment.service import (
    assess_overload_eligibility,
    check_schedule_conflict,
    check_unit_ceiling,
    compute_effective_ceiling,
    count_section_enrollments,
    enrolled_sections,
    record_enrollments,
    section_roster_add_drop,
    section_roster_enrollments,
)

__all__ = [
    "AddDropDecisionRequest",
    "AddDropRequestCreate",
    "AddDropRequestRead",
    "AdminAddDropRequestRead",
    "AvailableCourseRead",
    "CertificateMetadata",
    "CertificateRead",
    "EnrollmentSubmitRead",
    "EnrollmentSummaryRead",
    "IssueCertificate",
    "OverloadRequestFiledRead",
    "OverloadRequestRead",
    "SectionRosterAddDrop",
    "SectionRosterEnrollment",
    "TermEnrollmentRead",
    "VerificationRead",
    "assess_overload_eligibility",
    "begin_certificate_snapshot",
    "check_schedule_conflict",
    "check_unit_ceiling",
    "compute_effective_ceiling",
    "count_section_enrollments",
    "decide_add_drop_request",
    "decide_overload_request",
    "enrolled_sections",
    "evaluate_academics",
    "file_add_drop_request",
    "file_overload_request",
    "file_waiver",
    "get_approved_overload_units",
    "get_current_enrollment",
    "get_enrollment_report_counts",
    "get_enrollment_summary",
    "get_pending_overload_units",
    "get_student_add_drop_request",
    "get_term_enrollment",
    "issue_certificate",
    "list_admin_add_drop_requests",
    "list_available_courses",
    "list_overload_requests",
    "list_student_add_drop_requests",
    "list_student_waivers",
    "read_assessment",
    "read_certificate",
    "record_enrollments",
    "require_academics",
    "section_roster_add_drop",
    "section_roster_enrollments",
    "stage_reissue",
    "submit_enrollment",
    "verify_certificate",
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


async def evaluate_academics(db: AsyncSession, *, student_id, term, section_ids):
    # Grading reads enrollment history; defer the orchestration import so the
    # two domain clients do not create an import-time cycle.
    from cbpupsis_shared.domains.enrollment.academic import (  # noqa: PLC0415
        evaluate_academics as evaluate,
    )

    return await evaluate(db, student_id=student_id, term=term, section_ids=section_ids)


async def file_waiver(
    db: AsyncSession, *, student_id: uuid.UUID, data: WaiverFile, ip_address: str | None
) -> WaiverFiled:
    from cbpupsis_shared.domains.enrollment import waiver  # noqa: PLC0415

    return await waiver.file_waiver(
        db, student_id=student_id, data=data, ip_address=ip_address
    )


async def list_student_waivers(
    db: AsyncSession, *, student_id: uuid.UUID
) -> list[WaiverRead]:
    from cbpupsis_shared.domains.enrollment import waiver  # noqa: PLC0415

    return await waiver.list_student_waivers(db, student_id=student_id)


def require_academics(verdict):
    from cbpupsis_shared.domains.enrollment.academic import (  # noqa: PLC0415
        require_academics as require,
    )

    return require(verdict)


async def get_term_enrollment(
    db: AsyncSession, student_id: uuid.UUID, academic_term_id: uuid.UUID
) -> TermEnrollmentRead | None:
    return await service.get_term_enrollment(db, student_id, academic_term_id)


async def get_current_enrollment(
    db: AsyncSession, student_id: uuid.UUID, academic_term_id: uuid.UUID
):
    return await service.get_current_enrollment(db, student_id, academic_term_id)


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


async def get_enrollment_report_counts(
    db: AsyncSession,
    *,
    term_id: uuid.UUID,
    group_by: str,
    program_code: str | None = None,
    status: str | None = None,
    department: str | None = None,
    college: str | None = None,
) -> list[tuple[str, int]]:
    return await service.get_enrollment_report_counts(
        db,
        term_id=term_id,
        group_by=group_by,
        program_code=program_code,
        status=status,
        department=department,
        college=college,
    )


async def get_approved_overload_units(
    db: AsyncSession, *, student_id: uuid.UUID, academic_term_id: uuid.UUID
) -> Decimal:
    return await service.get_approved_overload_units(
        db, student_id=student_id, academic_term_id=academic_term_id
    )


async def get_pending_overload_units(
    db: AsyncSession, *, student_id: uuid.UUID, academic_term_id: uuid.UUID
) -> Decimal:
    return await service.get_pending_overload_units(
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
    version: int,
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
