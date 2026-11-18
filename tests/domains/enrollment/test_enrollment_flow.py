"""The connected flow: submit, certificate, add/drop approval, reassessment."""

from __future__ import annotations

import base64
from decimal import Decimal
from types import SimpleNamespace

import pytest
from pydantic import SecretStr
from sqlalchemy import func, select

from cbpupsis_core.config import settings
from cbpupsis_database.models.audit import AuditEntry
from cbpupsis_database.models.billing import FeeStructure, StudentBillingAccount
from cbpupsis_database.models.curriculum import Course, CoursePrerequisite
from cbpupsis_database.models.enrollment import (
    AddDropRequest,
    CorIssuance,
    StudentCourseEnrollment,
)
from cbpupsis_shared.domains.academics.schemas import AcademicTermRead
from cbpupsis_shared.domains.enrollment import academic, academic_verdict, service
from cbpupsis_shared.domains.enrollment.constants import (
    APPROVE_ADD_DROP,
    CREATE_ADD_DROP_REQUEST,
    CREATE_ENROLLMENT,
    READ_ADD_DROP_REQUEST,
)
from cbpupsis_shared.domains.enrollment.exceptions import CertificateUnavailable
from tests.domains.enrollment.test_add_drop import (
    _admin,
    _course,
    _grant_action,
    _program,
    _section,
    _student,
    _student_headers,
    _term,
)

NUMBER = "2026-00301-MN-0"
SUBMIT_URL = "/api/v1/student/enrollment/submit"
ADD_DROP_URL = "/api/v1/student/enrollment/add-drop"
ADMIN_URL = "/api/v1/admin/add-drop"
COR_URL = "/api/v1/student/schedule/cor"
ASSESSMENT_URL = "/api/v1/student/assessment"
VERIFY_URL = "/api/v1/public/cor/verify/"


@pytest.fixture
async def flow(db, client, monkeypatch):
    monkeypatch.setattr(
        settings,
        "cor_encryption_key",
        SecretStr(base64.urlsafe_b64encode(bytes(range(32))).decode()),
    )
    monkeypatch.setattr(
        settings, "frontend_base_url_student", "https://student.example.test"
    )
    term = await _term(db)
    program, curriculum = await _program(db)
    sections = {}
    for day, code in enumerate(("CS401", "CS402", "CS403", "CS404", "CS405")):
        course = await _course(db, curriculum=curriculum, code=code, title=code)
        sections[code] = await _section(
            db, term=term, course=course, name=code, day=day
        )
    extra = await _course(db, curriculum=curriculum, code="CS406", title="CS406")
    sections["CS406"] = await _section(db, term=term, course=extra, name="CS406", day=5)
    student = await _student(db, number=NUMBER, program=program, curriculum=curriculum)
    db.add(
        FeeStructure(
            program_id=program.id,
            academic_term_id=term.id,
            tuition_per_unit=Decimal("100"),
            registration_fee=Decimal("50"),
            lab_fee=Decimal("25"),
            misc_fee=Decimal("75"),
        )
    )
    await db.commit()
    for action in (
        CREATE_ENROLLMENT,
        CREATE_ADD_DROP_REQUEST,
        READ_ADD_DROP_REQUEST,
        "IssueCertificate",
        "ReadSchedule",
        "ReadAssessment",
    ):
        await _grant_action(db, action=action, users=[student])
    chair, chair_headers = await _admin(
        db, position="chairperson", department="Computer Science", college="CCIS"
    )
    await _grant_action(db, action=APPROVE_ADD_DROP, users=[chair])
    await _grant_action(db, action=READ_ADD_DROP_REQUEST, users=[chair])
    return SimpleNamespace(
        term=term,
        curriculum=curriculum,
        sections=sections,
        student=student,
        chair=chair,
        chair_headers=chair_headers,
        headers=await _student_headers(client, NUMBER),
    )


async def _submit(client, flow, codes=("CS401", "CS402", "CS403", "CS404", "CS405")):
    response = await client.post(
        SUBMIT_URL,
        headers=flow.headers,
        json={
            "academicTermId": str(flow.term.id),
            "sectionIds": [str(flow.sections[code].id) for code in codes],
            "confirmation": True,
        },
    )
    assert response.status_code == 201, response.text


async def _issue(client, flow):
    return await client.post(
        COR_URL, headers=flow.headers, json={"academicTermId": str(flow.term.id)}
    )


async def _read_cor(client, flow):
    response = await client.get(
        COR_URL,
        headers=flow.headers,
        params={"academicTermId": str(flow.term.id), "format": "json"},
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _assessment(client, flow):
    response = await client.get(
        ASSESSMENT_URL,
        headers=flow.headers,
        params={"academicTermId": str(flow.term.id)},
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _file_add(client, flow, code="CS406"):
    response = await client.post(
        ADD_DROP_URL,
        headers=flow.headers,
        json={"add": [{"sectionId": str(flow.sections[code].id)}], "drop": []},
    )
    assert response.status_code == 201, response.text
    return response.json()["requestId"]


async def _approve(client, flow, request_id):
    return await client.patch(
        f"{ADMIN_URL}/{request_id}",
        headers=flow.chair_headers,
        json={"decision": "APPROVE", "remarks": "Approved.", "version": 1},
    )


async def test_whole_flow_from_submit_to_superseded_certificate(client, db, flow):
    await _submit(client, flow)
    issued = await _issue(client, flow)
    assert issued.status_code == 201, issued.text
    first = await _read_cor(client, flow)
    old_token = first["verificationUrl"].rsplit("/", 1)[1]
    verified = await client.get(f"{VERIFY_URL}{old_token}")
    assert verified.status_code == 200 and verified.json()["status"] == "CURRENT"
    before = await _assessment(client, flow)
    assert before["amountAssessed"] == "1650.00"

    request_id = await _file_add(client, flow)
    assert await db.scalar(select(func.count()).select_from(CorIssuance)) == 1
    approved = await _approve(client, flow, request_id)
    assert approved.status_code == 200, approved.text
    assert approved.json()["state"] == "APPROVED"

    assert (await _assessment(client, flow))["amountAssessed"] == "1950.00"
    second = await _read_cor(client, flow)
    assert second["certificateId"] != first["certificateId"]
    assert second["status"] == "CURRENT"
    assert second["totalUnits"] == "18.0"
    assert second["assessment"]["amountAssessed"] == "1950.00"
    new_token = second["verificationUrl"].rsplit("/", 1)[1]
    assert (await client.get(f"{VERIFY_URL}{old_token}")).json()[
        "status"
    ] == "SUPERSEDED"
    assert (await client.get(f"{VERIFY_URL}{new_token}")).json()["status"] == "CURRENT"
    audit = await db.scalar(
        select(AuditEntry).where(AuditEntry.action == "enrollment.cor_reissued")
    )
    assert audit.actor_id == flow.chair.id
    assert audit.payload["actor_role"] == "chairperson"


async def test_student_without_certificate_is_approvable(client, db, flow):
    await _submit(client, flow)
    request_id = await _file_add(client, flow)
    approved = await _approve(client, flow, request_id)
    assert approved.status_code == 200, approved.text
    assert await db.scalar(select(func.count()).select_from(CorIssuance)) == 0
    assert (await _assessment(client, flow))["amountAssessed"] == "1950.00"
    # The first certificate reflects the approved load.
    assert (await _issue(client, flow)).status_code == 201
    assert (await _read_cor(client, flow))["totalUnits"] == "18.0"


async def test_failed_reissue_rolls_back_the_approval(client, db, flow, monkeypatch):
    await _submit(client, flow)
    assert (await _issue(client, flow)).status_code == 201
    request_id = await _file_add(client, flow)
    added_id = flow.sections["CS406"].id
    term_id = flow.term.id

    async def fail(*args, **kwargs):
        raise CertificateUnavailable

    monkeypatch.setattr(service, "stage_reissue", fail)
    refused = await _approve(client, flow, request_id)
    assert refused.status_code == 503

    db.expire_all()
    request_row = await db.scalar(select(AddDropRequest))
    assert request_row.state == "PENDING"
    assert (
        await db.scalar(
            select(func.count())
            .select_from(StudentCourseEnrollment)
            .where(StudentCourseEnrollment.section_id == added_id)
        )
        == 0
    )
    account = await db.scalar(select(StudentBillingAccount))
    assert account.gross_amount == Decimal("1650.00")
    current = await client.get(
        COR_URL,
        headers=flow.headers,
        params={"academicTermId": str(term_id), "format": "json"},
    )
    assert current.json()["status"] == "CURRENT"


async def test_approval_without_fee_structure_is_refused(client, db, flow):
    await _submit(client, flow)
    request_id = await _file_add(client, flow)
    await db.delete(await db.scalar(select(FeeStructure)))
    await db.commit()
    refused = await _approve(client, flow, request_id)
    assert refused.status_code == 409
    assert refused.json()["code"] == "COR_ENROLLMENT_NOT_FINAL"
    assert refused.json()["details"]["unmet"] == ["NO_ASSESSMENT"]


async def test_certificate_counts_a_registered_pe_course_once(client, db, flow):
    """The sections under review are also active history: not a second PE."""
    course = await _course(
        db, curriculum=flow.curriculum, code="PATHFIT 1", title="PE 1"
    )
    course.pe_sequence = 1
    flow.sections["PE1"] = await _section(
        db, term=flow.term, course=course, name="PE1", day=6
    )
    await db.commit()
    await _submit(client, flow, ("CS401", "CS402", "PE1"))
    response = await _issue(client, flow)
    assert response.status_code == 201, response.text


async def test_certificate_refuses_a_real_prerequisite_violation(client, db, flow):
    await _submit(client, flow, ("CS401", "CS402"))
    db.add(
        CoursePrerequisite(
            course_id=flow.sections["CS401"].course_id,
            prerequisite_course_id=flow.sections["CS406"].course_id,
        )
    )
    await db.commit()
    response = await _issue(client, flow)
    assert response.status_code == 409
    assert response.json()["details"]["unmet"] == ["PREREQUISITE_VIOLATION"]


async def test_missing_academic_data_makes_the_certificate_unavailable(
    client, db, flow
):
    await _submit(client, flow)
    course_section = flow.sections["CS401"]
    await db.refresh(course_section)
    (await db.get(Course, course_section.course_id)).is_nstp = None
    await db.commit()
    response = await _issue(client, flow)
    assert response.status_code == 503


async def test_verdict_does_not_block_on_own_in_progress_course(db, flow, client):
    await _submit(client, flow, ("CS401", "CS402"))
    eligible = await academic_verdict.certificate_eligible(
        db,
        student_id=flow.student.id,
        term_id=flow.term.id,
        section_ids=[flow.sections["CS401"].id, flow.sections["CS402"].id],
    )
    assert eligible is True


async def test_filing_refuses_an_unmet_prerequisite(client, db, flow):
    await _submit(client, flow)
    db.add(
        CoursePrerequisite(
            course_id=flow.sections["CS406"].course_id,
            prerequisite_course_id=flow.sections["CS401"].course_id,
        )
    )
    await db.commit()
    response = await client.post(
        ADD_DROP_URL,
        headers=flow.headers,
        json={"add": [{"sectionId": str(flow.sections["CS406"].id)}], "drop": []},
    )
    assert response.status_code == 409
    assert response.json()["code"] == "ENROLL_PREREQUISITE_NOT_MET"
    assert response.json()["details"]["unmet"][0]["course"] == "CS401"
    assert await db.scalar(select(func.count()).select_from(AddDropRequest)) == 0


async def test_filing_checks_pe_against_the_load_after_drops(client, db, flow):
    first = await _course(
        db, curriculum=flow.curriculum, code="PATHFIT 1", title="PE 1", units=Decimal(2)
    )
    second = await _course(
        db, curriculum=flow.curriculum, code="PATHFIT 2", title="PE 2", units=Decimal(2)
    )
    first.pe_sequence, second.pe_sequence = 1, 2
    flow.sections["PE1"] = await _section(
        db, term=flow.term, course=first, name="PE1", day=6
    )
    flow.sections["PE2"] = await _section(
        db, term=flow.term, course=second, name="PE2", day=5
    )
    await db.commit()
    await _submit(client, flow, ("CS401", "CS402", "CS403", "CS404", "PE1"))
    add = {"add": [{"sectionId": str(flow.sections["PE2"].id)}]}

    kept = await client.post(
        ADD_DROP_URL, headers=flow.headers, json={**add, "drop": []}
    )
    assert kept.status_code == 409
    assert kept.json()["code"] == "ENROLL_PE_SEQUENCE"

    swapped = await client.post(
        ADD_DROP_URL,
        headers=flow.headers,
        json={
            **add,
            "drop": [{"sectionId": str(flow.sections["PE1"].id), "reason": "Swap"}],
        },
    )
    assert swapped.status_code == 201, swapped.text


async def test_off_curriculum_section_does_not_fail_the_preview(db, flow):
    """Another program's course is left out instead of refusing the whole list."""
    _, other_curriculum = await _program(db, code="BSIT")
    outside = await _course(
        db, curriculum=other_curriculum, code="IT101", title="Outside"
    )
    section = await _section(db, term=flow.term, course=outside, name="IT101")
    verdicts = await academic.preview_academics(
        db,
        student_id=flow.student.id,
        term=AcademicTermRead.model_validate(flow.term),
        section_ids=[section.id, flow.sections["CS401"].id],
    )
    assert set(verdicts) == {flow.sections["CS401"].id}


async def test_corequisite_is_met_by_taking_both_together(client, db, flow):
    db.add(
        CoursePrerequisite(
            course_id=flow.sections["CS401"].course_id,
            prerequisite_course_id=flow.sections["CS402"].course_id,
            is_corequisite=True,
        )
    )
    await db.commit()
    verdicts = await academic.preview_academics(
        db,
        student_id=flow.student.id,
        term=AcademicTermRead.model_validate(flow.term),
        section_ids=[flow.sections["CS401"].id, flow.sections["CS402"].id],
    )
    assert verdicts[flow.sections["CS401"].id].prerequisites_satisfied is True

    alone = await client.post(
        SUBMIT_URL,
        headers=flow.headers,
        json={
            "academicTermId": str(flow.term.id),
            "sectionIds": [str(flow.sections["CS401"].id)],
            "confirmation": True,
        },
    )
    assert alone.status_code == 409
    assert alone.json()["code"] == "ENROLL_PREREQUISITE_NOT_MET"
    # The refusal rolled back the shared session, which expired these rows.
    for row in (flow.term, flow.sections["CS401"], flow.sections["CS402"]):
        await db.refresh(row)
    await _submit(client, flow, ("CS401", "CS402"))
