"""Focused lifecycle, scope and transactional-effect regression checks."""

import uuid

import pytest
from sqlalchemy import func, select

from cbpupsis_api_student.domains.student_enrollment.router import filing_student
from cbpupsis_core.exceptions import AppError
from cbpupsis_database.models.audit import AuditEntry
from cbpupsis_database.models.curriculum import Program
from cbpupsis_database.models.enrollment import CourseWaiver, StudentCourseEnrollment
from cbpupsis_database.models.grading import StudentGrade
from cbpupsis_database.models.outbox import OutboxMessage
from cbpupsis_database.models.sections import CourseSection
from cbpupsis_database.models.users import StudentProfile, User
from cbpupsis_shared.domains.academics.schemas import AcademicTermRead
from cbpupsis_shared.domains.auth.dependencies import CurrentUser
from cbpupsis_shared.domains.auth.security import create_access_token
from cbpupsis_shared.domains.enrollment import waiver
from cbpupsis_shared.domains.enrollment.academic import evaluate_academics
from cbpupsis_shared.domains.enrollment.schemas import (
    WaiverDecision,
    WaiverFile,
    WaiverReviewer,
)
from main import app


def filing(f):
    return WaiverFile(
        section_id=f.section.id,
        prerequisite_course_code="PREREQ",
        condition="GRADUATING",
        justification="Synthetic final-term evidence",
    )


async def qualified(db, f):
    f.profile.is_graduating_senior = True
    f.profile.graduating_final_term_id = f.term.id
    await db.commit()


async def counts(db):
    return tuple(
        [
            await db.scalar(select(func.count()).select_from(model))
            for model in (CourseWaiver, AuditEntry, OutboxMessage)
        ]
    )


async def test_ac_enrl_002_9_filing_202_full_scoped_workflow(db, client, academic_db):
    f = academic_db
    await qualified(db, f)
    app.dependency_overrides[filing_student] = lambda: CurrentUser(
        id=f.student, email="student@example.test", role="student"
    )
    try:
        response = await client.post(
            "/api/v1/student/enrollment/waiver",
            json=filing(f).model_dump(mode="json", by_alias=True),
        )
    finally:
        app.dependency_overrides.pop(filing_student, None)
    assert response.status_code == 202, response.text
    assert response.json()["state"] == "PENDING_CHAIR"
    waiver_id = uuid.UUID(response.json()["waiverRequestId"])
    kwargs = {
        "student_id": f.student,
        "term": AcademicTermRead.model_validate(f.term),
        "section_ids": [f.section.id],
    }
    # Real evaluator/owner reads, NOT evidence of the absent #169 submit endpoint.
    assert (await evaluate_academics(db, **kwargs)).blockers[
        0
    ].rule == "ENROLL_WAIVER_PENDING"
    assert await counts(db) == (1, 1, 0)
    for position, state in [
        ("chairperson", "PENDING_DEAN"),
        ("dean", "PENDING_VPAA"),
        ("registrar", "APPROVED"),
    ]:
        result = await waiver.decide_waiver(
            db,
            reviewer=f.reviewers[position],
            waiver_id=waiver_id,
            data=WaiverDecision(
                decision="APPROVE",
                remarks="Reviewed",
                vpaa_reference="EXTERNAL-99" if position == "registrar" else None,
            ),
        )
        assert result.state == state
    assert await counts(db) == (1, 4, 3)
    assert not (await evaluate_academics(db, **kwargs)).blockers
    row = await db.get(CourseWaiver, waiver_id)
    assert row.registrar_approved_by == f.reviewers["registrar"].id
    assert row.vpaa_token == "EXTERNAL-99"
    with pytest.raises(AppError) as error:
        await waiver.decide_waiver(
            db,
            reviewer=f.reviewers["chairperson"],
            waiver_id=waiver_id,
            data=WaiverDecision(decision="APPROVE"),
        )
    assert error.value.code == "REQUEST_ALREADY_DECIDED"
    with pytest.raises(AppError) as duplicate:
        await waiver.file_waiver(db, student_id=f.student, data=f.filing)
    assert duplicate.value.code == "REQUEST_ALREADY_DECIDED"
    assert await counts(db) == (1, 4, 3)


@pytest.mark.parametrize("position", ["dean", "registrar"])
async def test_ac_enrl_002_10_out_of_step_unchanged(db, academic_db, position):
    f = academic_db
    await qualified(db, f)
    filed = await waiver.file_waiver(db, student_id=f.student, data=filing(f))
    with pytest.raises(AppError) as error:
        await waiver.decide_waiver(
            db,
            reviewer=f.reviewers[position],
            waiver_id=filed.waiver_request_id,
            data=WaiverDecision(decision="APPROVE"),
        )
    assert error.value.code == "ENROLL_WAIVER_STEP_MISMATCH"
    assert (
        await db.get(CourseWaiver, filed.waiver_request_id)
    ).state == "PENDING_CHAIR"
    assert await counts(db) == (1, 1, 0)


async def test_filing_rejects_unsupported_claim_and_live_duplicate(db, academic_db):
    f = academic_db
    with pytest.raises(AppError) as error:
        await waiver.file_waiver(db, student_id=f.student, data=filing(f))
    assert error.value.code == "ENROLL_PREREQUISITE_NOT_MET"
    assert await counts(db) == (0, 0, 0)
    # Rollback expires ORM fixtures: reload deliberately.
    profile = await db.get(StudentProfile, f.student)
    profile.is_graduating_senior = True
    profile.graduating_final_term_id = f.term_id
    await db.commit()
    await waiver.file_waiver(db, student_id=f.student, data=f.filing)
    with pytest.raises(AppError) as error:
        await waiver.file_waiver(db, student_id=f.student, data=f.filing)
    assert error.value.code == "ENROLL_WAIVER_PENDING"


async def test_failed_registration_is_not_itself_a_same_term_retake(db, academic_db):
    f = academic_db
    f.item.status = "enrolled"
    await db.commit()
    with pytest.raises(AppError) as error:
        await waiver.file_waiver(
            db,
            student_id=f.student,
            data=f.filing.model_copy(update={"condition": "REPEATED_PREREQUISITE"}),
        )
    assert error.value.code == "ENROLL_PREREQUISITE_NOT_MET"


@pytest.mark.parametrize("condition", ["REPEATED_PREREQUISITE", "SUPERIOR_STANDING"])
async def test_filing_other_conditions_uses_real_authoritative_records(
    db, academic_db, condition
):
    from datetime import UTC, datetime

    from cbpupsis_database.models.curriculum import Course

    f = academic_db
    if condition == "REPEATED_PREREQUISITE":
        course_id = f.prerequisite.id
    else:
        bonus = Course(
            id=uuid.uuid4(),
            course_code="BONUS",
            title="Synthetic prior record",
            lecture_units=15,
            is_nstp=False,
        )
        db.add(bonus)
        await db.flush()
        course_id = bonus.id
    section = CourseSection(
        id=uuid.uuid4(),
        academic_term_id=f.term_id,
        course_id=course_id,
        section_name="RETAKE",
        capacity=30,
    )
    db.add(section)
    await db.flush()
    item = StudentCourseEnrollment(
        id=uuid.uuid4(),
        term_enrollment_id=f.item.term_enrollment_id,
        academic_term_id=f.term_id,
        section_id=section.id,
        status="enrolled",
    )
    db.add(item)
    await db.flush()
    if condition == "SUPERIOR_STANDING":
        db.add(
            StudentGrade(
                enrollment_item_id=item.id,
                final_grade="1.00",
                is_draft=False,
                is_locked=True,
                submitted_at=datetime.now(UTC),
            )
        )
    await db.commit()
    result = await waiver.file_waiver(
        db,
        student_id=f.student,
        data=f.filing.model_copy(update={"condition": condition}),
    )
    assert result.state == "PENDING_CHAIR"


async def test_rejection_remarks_and_final_reference_are_required(db, academic_db):
    f = academic_db
    await qualified(db, f)
    filed = await waiver.file_waiver(db, student_id=f.student, data=filing(f))
    with pytest.raises(AppError) as error:
        await waiver.decide_waiver(
            db,
            reviewer=f.reviewers["chairperson"],
            waiver_id=filed.waiver_request_id,
            data=WaiverDecision(decision="REJECT", remarks="   "),
        )
    assert error.value.code == "DECISION_REMARKS_REQUIRED"
    for position in ("chairperson", "dean"):
        await waiver.decide_waiver(
            db,
            reviewer=f.reviewers[position],
            waiver_id=filed.waiver_request_id,
            data=WaiverDecision(decision="APPROVE"),
        )
    with pytest.raises(AppError) as error:
        await waiver.decide_waiver(
            db,
            reviewer=f.reviewers["registrar"],
            waiver_id=filed.waiver_request_id,
            data=WaiverDecision(decision="APPROVE", vpaa_reference="  "),
        )
    assert error.value.code == "VALIDATION_ERROR"
    result = await waiver.decide_waiver(
        db,
        reviewer=f.reviewers["registrar"],
        waiver_id=filed.waiver_request_id,
        data=WaiverDecision(decision="REJECT", remarks="No external approval"),
    )
    assert result.state == "REJECTED"
    assert await counts(db) == (1, 4, 3)


async def test_chair_dean_scope_isolation_and_registrar_broad_list(db, academic_db):
    f = academic_db
    await qualified(db, f)
    own = await waiver.file_waiver(db, student_id=f.student, data=filing(f))
    other_user = User(
        id=uuid.uuid4(), email="other@example.com", password_hash="!fixture"
    )
    program = Program(
        id=uuid.uuid4(),
        code="OTHER",
        name="Other",
        department="DEPT-B",
        college="COLLEGE-B",
        total_units_required=120,
    )
    db.add_all([other_user, program])
    await db.flush()
    db.add(
        StudentProfile(
            user_id=other_user.id,
            student_number="OTHER",
            program_id=program.id,
            year_level=1,
        )
    )
    row = CourseWaiver(
        id=uuid.uuid4(),
        student_id=other_user.id,
        section_id=f.section_id,
        prerequisite_course_code="PREREQ",
        condition="GRADUATING",
        justification="Synthetic",
    )
    other_id = row.id
    db.add(row)
    await db.commit()
    for position in ("chairperson", "dean"):
        page = await waiver.list_waivers(db, reviewer=f.reviewers[position])
        assert [r.waiver_request_id for r in page.items] == [own.waiver_request_id]
        with pytest.raises(AppError) as error:
            await waiver.decide_waiver(
                db,
                reviewer=f.reviewers[position],
                waiver_id=other_id,
                data=WaiverDecision(decision="APPROVE"),
            )
        assert error.value.status_code == 404
        assert error.value.code == "RESOURCE_NOT_FOUND"
    assert (await waiver.list_waivers(db, reviewer=f.reviewers["registrar"])).total == 2
    missing_scope = WaiverReviewer(id=f.reviewers["dean"].id, position="dean")
    assert (await waiver.list_waivers(db, reviewer=missing_scope)).total == 0


async def test_student_cannot_read_admin_list_even_with_signed_token(
    client, academic_db
):
    token = create_access_token(
        academic_db.student, claims={"role": "student", "sv": 0}
    )
    response = await client.get(
        "/api/v1/admin/waiver-requests", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 403


async def test_decision_failure_rolls_back_state_audit_and_notice(
    db, academic_db, monkeypatch
):
    f = academic_db
    await qualified(db, f)
    filed = await waiver.file_waiver(db, student_id=f.student, data=filing(f))

    def fail(*args, **kwargs):
        raise RuntimeError("Synthetic outbox staging failure")

    monkeypatch.setattr(waiver, "publish_transactional", fail)
    with pytest.raises(RuntimeError):
        await waiver.decide_waiver(
            db,
            reviewer=f.reviewers["chairperson"],
            waiver_id=filed.waiver_request_id,
            data=WaiverDecision(decision="APPROVE"),
        )
    assert (
        await db.get(CourseWaiver, filed.waiver_request_id)
    ).state == "PENDING_CHAIR"
    assert await counts(db) == (1, 1, 0)


async def test_real_admin_mfa_permission_scope_and_expired_session(
    db,
    client,
    academic_db,
    registered_user,
    grant,
):
    from datetime import UTC, datetime

    from cbpupsis_api_admin.domains.admin_auth import service as admin_service
    from cbpupsis_database.models.auth import UserActiveSession
    from cbpupsis_shared.domains.auth.security import totp_at
    from cbpupsis_shared.domains.iam import service as iam
    from cbpupsis_shared.domains.users.constants import AdminPosition

    f = academic_db
    await qualified(db, f)
    filed = await waiver.file_waiver(db, student_id=f.student, data=filing(f))
    admin_id = uuid.UUID(registered_user["id"])
    group = await iam.create_group(db, name="Admins")
    await iam.add_user_to_group(db, admin_id, group.id)
    await admin_service.configure_admin_profile(
        db, user_id=admin_id, position=AdminPosition.chairperson, department_id="DEPT-A"
    )
    await grant(admin_id, "ReadWaiverRequest")
    challenge = await client.post(
        "/api/v1/auth/admin/login",
        json={
            "email": registered_user["email"],
            "password": registered_user["password"],
        },
    )
    challenge_token = challenge.json()["challenge_token"]
    refused = await client.get(
        "/api/v1/admin/waiver-requests",
        headers={"Authorization": f"Bearer {challenge_token}"},
    )
    assert refused.status_code == 403
    enrolled = await client.post(
        "/api/v1/auth/admin/mfa/totp/enroll", json={"challenge_token": challenge_token}
    )
    code = totp_at(enrolled.json()["secret"], int(datetime.now(UTC).timestamp()) // 30)
    confirmed = await client.post(
        "/api/v1/auth/admin/mfa/totp/confirm",
        json={"challenge_token": challenge_token, "code": code},
    )
    assert confirmed.status_code == 200, confirmed.text
    headers = {"Authorization": f"Bearer {confirmed.json()['access_token']}"}
    response = await client.get("/api/v1/admin/waiver-requests", headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["items"][0]["waiverRequestId"] == str(
        filed.waiver_request_id
    )
    path = f"/api/v1/admin/waiver-requests/{filed.waiver_request_id}"
    assert (
        await client.patch(path, json={"decision": "APPROVE"}, headers=headers)
    ).status_code == 403
    await grant(admin_id, "ApproveWaiverRequest")
    assert (
        await client.patch(path, json={"decision": "APPROVE"}, headers=headers)
    ).json()["state"] == "PENDING_DEAN"
    sessions = list(
        await db.scalars(
            select(UserActiveSession).where(UserActiveSession.user_id == admin_id)
        )
    )
    for session in sessions:
        await db.delete(session)
    await db.commit()
    assert (
        await client.get("/api/v1/admin/waiver-requests", headers=headers)
    ).status_code == 401


async def test_finance_never_gains_decision_power_from_broad_scope(db, grant):
    from cbpupsis_api_admin.domains.admin_auth.dependencies import (
        AdminSession,
        require_waiver_reviewer,
    )

    reviewer_id = uuid.uuid4()
    await grant(reviewer_id, "ApproveWaiverRequest")
    checker = require_waiver_reviewer("ApproveWaiverRequest")
    with pytest.raises(AppError) as error:
        await checker(
            AdminSession(
                id=reviewer_id,
                email="finance@example.com",
                position="finance",
                department_id=None,
                college_id=None,
            ),
            db,
        )
    assert error.value.status_code == 403
