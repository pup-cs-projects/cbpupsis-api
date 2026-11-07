from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from cbpupsis_database import models as _models  # noqa: F401
from cbpupsis_database.base import Base
from cbpupsis_database.models.academics import AcademicTerm
from cbpupsis_database.models.audit import AuditEntry
from cbpupsis_database.models.billing import (
    FeeStructure,
    StudentBillingAccount,
    StudentHold,
)
from cbpupsis_database.models.curriculum import (
    Course,
    Curriculum,
    CurriculumCourse,
    Program,
)
from cbpupsis_database.models.enrollment import (
    OverloadRequest,
    StudentCourseEnrollment,
    TermEnrollment,
)
from cbpupsis_database.models.grading import StudentTermGPA
from cbpupsis_database.models.outbox import OutboxMessage
from cbpupsis_database.models.sections import CourseSection, SectionSchedule
from cbpupsis_database.models.users import StudentProfile, User, UserProfile
from cbpupsis_shared.domains.academics.schemas import AcademicTermRead
from cbpupsis_shared.domains.auth.security import hash_password
from cbpupsis_shared.domains.billing import client as billing
from cbpupsis_shared.domains.enrollment import service as enrollment_service
from cbpupsis_shared.domains.enrollment.constants import (
    CREATE_ENROLLMENT,
    ENROLLED_PENDING,
    ENROLLMENT_SUBMITTED_ACTION,
    ENROLLMENT_SUBMITTED_EVENT,
    READ_ENROLLMENT,
)
from cbpupsis_shared.domains.enrollment.exceptions import (
    EnrollmentAssessmentUnavailableError,
    EnrollmentSectionFullError,
)

URL = "/api/v1/student/enrollment/submit"
AVAILABLE_URL = "/api/v1/student/enrollment/available-courses"
LOGIN_URL = "/api/v1/auth/student/login"
PASSWORD = "StudentPassword123!"


async def _program_curriculum(
    db: AsyncSession,
    *,
    code: str,
    semester: str = "First Semester",
    course_count: int = 2,
) -> tuple[Program, Curriculum, list[Course]]:
    program = Program(
        code=code,
        name=f"{code} Program",
        department="Computer Science",
        total_units_required=Decimal("140"),
        is_active=True,
    )
    db.add(program)
    await db.flush()
    curriculum = Curriculum(
        program_id=program.id, curriculum_year="2026", is_active=True
    )
    db.add(curriculum)
    await db.flush()
    courses = []
    for index in range(course_count):
        course = Course(
            course_code=f"{code}{index + 1}",
            title=f"Enrollment Course {index + 1}",
            lecture_units=Decimal("3"),
            lab_units=Decimal("0"),
            is_nstp=False,
            is_active=True,
        )
        db.add(course)
        await db.flush()
        db.add(
            CurriculumCourse(
                curriculum_id=curriculum.id,
                course_id=course.id,
                year_level=2,
                semester=semester,
            )
        )
        courses.append(course)
    return program, curriculum, courses


async def _term(
    db: AsyncSession,
    *,
    year: str = "2026-2027",
    semester: str = "First Semester",
) -> AcademicTerm:
    now = datetime.now(UTC)
    term = AcademicTerm(
        academic_year=year,
        semester=semester,
        start_date=date(2026, 8, 1),
        end_date=date(2027, 1, 1),
        is_current=True,
        regular_enrollment_start=now - timedelta(days=1),
        regular_enrollment_end=now + timedelta(days=1),
        add_drop_deadline=now + timedelta(days=7),
        grade_submission_deadline=now + timedelta(days=30),
    )
    db.add(term)
    await db.flush()
    return term


async def _student(
    db: AsyncSession,
    *,
    program: Program,
    curriculum: Curriculum,
    number: str,
    password_hash: str | None = None,
) -> User:
    user = User(
        email=f"{number.replace('-', '_')}@example.com",
        password_hash=password_hash or hash_password(PASSWORD),
        full_name=f"Student {number}",
        is_active=True,
    )
    db.add(user)
    await db.flush()
    db.add(
        UserProfile(
            user_id=user.id,
            first_name="Enrollment",
            last_name="Student",
            birthdate=date(2003, 5, 14),
        )
    )
    db.add(
        StudentProfile(
            user_id=user.id,
            student_number=number,
            program_id=program.id,
            curriculum_id=curriculum.id,
            year_level=2,
            enrollment_status="PENDING",
        )
    )
    await db.flush()
    return user


async def _section(
    db: AsyncSession,
    *,
    term: AcademicTerm,
    course: Course,
    name: str,
    day: int,
    start: time,
    end: time,
    capacity: int = 40,
) -> CourseSection:
    section = CourseSection(
        academic_term_id=term.id,
        course_id=course.id,
        section_name=name,
        capacity=capacity,
        status="open",
    )
    db.add(section)
    await db.flush()
    db.add(
        SectionSchedule(
            section_id=section.id,
            day_of_week=day,
            start_time=start,
            end_time=end,
            room="R101",
            schedule_type="lecture",
        )
    )
    await db.flush()
    return section


async def _fee_structure(
    db: AsyncSession, *, program: Program, term: AcademicTerm
) -> FeeStructure:
    fee = FeeStructure(
        program_id=program.id,
        academic_term_id=term.id,
        tuition_per_unit=Decimal("100"),
        registration_fee=Decimal("50"),
        lab_fee=Decimal("25"),
        misc_fee=Decimal("75"),
    )
    db.add(fee)
    await db.flush()
    return fee


async def _headers(client: AsyncClient, number: str) -> dict[str, str]:
    response = await client.post(
        LOGIN_URL,
        json={
            "student_number": number,
            "birthdate": "2003-05-14",
            "password": PASSWORD,
        },
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


@pytest.fixture
async def submission(db: AsyncSession, grant):
    program, curriculum, courses = await _program_curriculum(
        db, code=f"S{uuid.uuid4().hex[:6].upper()}"
    )
    term = await _term(db)
    student = await _student(
        db,
        program=program,
        curriculum=curriculum,
        number="2026-00169-MN-0",
    )
    other = await _student(
        db,
        program=program,
        curriculum=curriculum,
        number="2026-00170-MN-0",
    )
    first = await _section(
        db,
        term=term,
        course=courses[0],
        name="BSCS 3-1 A",
        day=0,
        start=time(9),
        end=time(10),
    )
    second = await _section(
        db,
        term=term,
        course=courses[1],
        name="BSCS 3-1 B",
        day=1,
        start=time(9),
        end=time(10),
    )
    await _fee_structure(db, program=program, term=term)
    await db.commit()
    await grant(student.id, CREATE_ENROLLMENT)
    return {
        "program": program,
        "curriculum": curriculum,
        "courses": courses,
        "term": term,
        "student": student,
        "other": other,
        "sections": [first, second],
    }


def _body(submission, **extra) -> dict[str, object]:
    body: dict[str, object] = {
        "academicTermId": str(submission["term"].id),
        "sectionIds": [str(row.id) for row in submission["sections"]],
        "confirmation": True,
    }
    body.update(extra)
    return body


async def _cross_placement_submission(
    db: AsyncSession,
    grant,
    *,
    extra_units: Decimal,
    student_number: str,
) -> dict[str, object]:
    code = f"L{uuid.uuid4().hex[:6].upper()}"
    program, curriculum, _ = await _program_curriculum(db, code=code, course_count=0)
    term = await _term(db)
    student = await _student(
        db,
        program=program,
        curriculum=curriculum,
        number=student_number,
    )
    prescribed = Course(
        course_code=f"{code}26",
        title="Prescribed term load",
        lecture_units=Decimal("26"),
        lab_units=Decimal("0"),
        is_nstp=False,
        is_active=True,
    )
    extra = Course(
        course_code=f"{code}EX",
        title="Additional curriculum course",
        lecture_units=extra_units,
        lab_units=Decimal("0"),
        is_nstp=False,
        is_active=True,
    )
    db.add_all([prescribed, extra])
    await db.flush()
    db.add_all(
        [
            CurriculumCourse(
                curriculum_id=curriculum.id,
                course_id=prescribed.id,
                year_level=2,
                semester="First Semester",
            ),
            CurriculumCourse(
                curriculum_id=curriculum.id,
                course_id=extra.id,
                year_level=1,
                semester="Second Semester",
            ),
        ]
    )
    sections = [
        await _section(
            db,
            term=term,
            course=prescribed,
            name=f"{code} A",
            day=0,
            start=time(9),
            end=time(10),
        ),
        await _section(
            db,
            term=term,
            course=extra,
            name=f"{code} B",
            day=1,
            start=time(9),
            end=time(10),
        ),
    ]
    await _fee_structure(db, program=program, term=term)
    await db.commit()
    await grant(student.id, CREATE_ENROLLMENT)
    await grant(student.id, READ_ENROLLMENT)
    return {
        "program": program,
        "curriculum": curriculum,
        "term": term,
        "student": student,
        "student_id": student.id,
        "courses": [prescribed, extra],
        "sections": sections,
    }


async def test_stage_seven_refuses_without_seats_then_accepts_approved_waiver(
    client, db, submission, grant, monkeypatch
):
    from unittest.mock import AsyncMock

    from cbpupsis_database.models.curriculum import CoursePrerequisite
    from cbpupsis_shared.domains.enrollment import waiver
    from cbpupsis_shared.domains.enrollment.constants import CREATE_WAIVER_REQUEST
    from cbpupsis_shared.domains.enrollment.schemas import (
        WaiverDecision,
        WaiverReviewer,
    )
    from cbpupsis_shared.domains.sections import client as sections

    student_id = submission["student"].id
    reviewer_id = submission["other"].id
    term_id = submission["term"].id
    target, prerequisite = submission["courses"]
    section_id = str(submission["sections"][0].id)
    prerequisite_code = prerequisite.course_code
    submission["program"].college = "CCIS"
    profile = await db.get(StudentProfile, student_id)
    profile.is_graduating_senior = True
    profile.graduating_final_term_id = term_id
    db.add(
        CoursePrerequisite(course_id=target.id, prerequisite_course_id=prerequisite.id)
    )
    await db.commit()
    await grant(student_id, CREATE_WAIVER_REQUEST)
    headers = await _headers(client, "2026-00169-MN-0")
    body = _body(submission, sectionIds=[section_id])
    lock = AsyncMock(wraps=sections.lock_for_enrollment)
    monkeypatch.setattr(sections, "lock_for_enrollment", lock)

    response = await client.post(URL, json=body, headers=headers)
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "ENROLL_PREREQUISITE_NOT_MET"
    assert response.json()["details"]["unmet"][0]["course"] == prerequisite_code
    lock.assert_not_awaited()
    filed = await client.post(
        "/api/v1/student/enrollment/waiver",
        headers=headers,
        json={
            "sectionId": section_id,
            "prerequisiteCourseCode": prerequisite_code,
            "condition": "GRADUATING",
            "justification": "Synthetic graduating final-term fixture",
        },
    )
    assert filed.status_code == 202, filed.text
    waiver_id = uuid.UUID(filed.json()["waiverRequestId"])
    pending = await client.post(URL, json=body, headers=headers)
    assert pending.status_code == 409, pending.text
    assert pending.json()["code"] == "ENROLL_WAIVER_PENDING"
    lock.assert_not_awaited()
    assert not await db.scalar(
        select(TermEnrollment.id).where(TermEnrollment.student_id == student_id)
    )
    for position in ("chairperson", "dean", "registrar"):
        await waiver.decide_waiver(
            db,
            reviewer=WaiverReviewer(
                id=reviewer_id,
                position=position,
                department="Computer Science",
                college="CCIS",
            ),
            waiver_id=waiver_id,
            data=WaiverDecision(decision="APPROVE", vpaa_reference="SYNTHETIC-213"),
        )
    accepted = await client.post(URL, json=body, headers=headers)
    assert accepted.status_code == 201, accepted.text
    assert accepted.json()["status"] == ENROLLED_PENDING
    lock.assert_awaited_once()


async def test_ac_enrl_001_1_eligibility_shows_current_enrollment(
    client: AsyncClient, db: AsyncSession, submission, grant
) -> None:
    """After a reload the page reads currentEnrollment instead of the picker."""
    await grant(submission["student"].id, READ_ENROLLMENT)
    headers = await _headers(client, "2026-00169-MN-0")
    eligibility_url = "/api/v1/student/enrollment/eligibility"
    before = await client.get(eligibility_url, headers=headers)
    assert before.status_code == 200, before.text
    assert before.json()["currentEnrollment"] is None

    submitted = await client.post(URL, headers=headers, json=_body(submission))
    assert submitted.status_code == 201, submitted.text

    after = await client.get(eligibility_url, headers=headers)
    assert after.status_code == 200, after.text
    current = after.json()["currentEnrollment"]
    assert current["enrollmentId"] == submitted.json()["enrollmentId"]
    assert current["status"] == ENROLLED_PENDING
    assert Decimal(current["totalUnits"]) == Decimal(submitted.json()["totalUnits"])


async def test_ac_enrl_001_1_success_returns_pending_assessment(
    client: AsyncClient, db: AsyncSession, submission
) -> None:
    response = await client.post(
        URL,
        headers=await _headers(client, "2026-00169-MN-0"),
        json=_body(submission),
    )

    assert response.status_code == 201, response.text
    data = response.json()
    assert data["status"] == ENROLLED_PENDING
    assert Decimal(data["totalUnits"]) == Decimal("6")
    assert {row["sectionId"] for row in data["sections"]} == {
        str(section.id) for section in submission["sections"]
    }
    stored = await billing.read_stored_assessment(
        db,
        submission["student"].id,
        submission["term"].id,
    )
    assert stored is not None
    assert stored.assessment.amountAssessed == "750.00"
    assert stored.assessment.subsidyApplied == "750.00"
    assert stored.assessment.netPayable == "0.00"
    assert {course.section_id for course in stored.inputs.courses} == {
        section.id for section in submission["sections"]
    }
    account = await db.scalar(select(StudentBillingAccount))
    assert account is not None
    assert account.due_date == submission["term"].regular_enrollment_end.date()


async def test_ac_enrl_001_2_prescribed_26_refuses_actual_29_unit_submit(
    client: AsyncClient, db: AsyncSession, grant
) -> None:
    submission = await _cross_placement_submission(
        db,
        grant,
        extra_units=Decimal("3"),
        student_number="2026-00262-MN-0",
    )
    headers = await _headers(client, "2026-00262-MN-0")

    available = await client.get(
        AVAILABLE_URL,
        headers=headers,
        params={"academicTermId": str(submission["term"].id)},
    )
    assert available.status_code == 200, available.text
    assert {row["sectionId"] for row in available.json()} == {
        str(section.id) for section in submission["sections"]
    }

    response = await client.post(URL, headers=headers, json=_body(submission))

    assert response.status_code == 409, response.text
    data = response.json()
    assert data["code"] == "ENROLL_UNIT_CEILING_EXCEEDED"
    assert "prescribed load is 26 units" in data["message"]
    assert "0 units remain" in data["message"]
    assert data["details"] == {
        "prescribedLoad": 26,
        "approvedOverloadUnits": 0,
        "standingReduction": 0,
        "effectiveCeiling": 26,
        "attemptedUnits": 29,
        "unitsRemaining": 0,
    }
    assert not await db.scalar(
        select(TermEnrollment.id).where(
            TermEnrollment.student_id == submission["student_id"]
        )
    )


async def test_ac_enrl_001_3_approved_9_allows_actual_35_unit_submit(
    client: AsyncClient, db: AsyncSession, grant
) -> None:
    submission = await _cross_placement_submission(
        db,
        grant,
        extra_units=Decimal("9"),
        student_number="2026-00263-MN-0",
    )
    db.add(
        OverloadRequest(
            student_id=submission["student_id"],
            academic_term_id=submission["term"].id,
            requested_units=9,
            justification="Approved nine-unit overload fixture",
            state="APPROVED",
        )
    )
    await db.commit()

    response = await client.post(
        URL,
        headers=await _headers(client, "2026-00263-MN-0"),
        json=_body(submission),
    )

    assert response.status_code == 201, response.text
    assert response.json()["status"] == ENROLLED_PENDING
    assert Decimal(response.json()["totalUnits"]) == Decimal("35")
    assert (
        await db.scalar(select(func.count()).select_from(StudentCourseEnrollment)) == 2
    )


async def test_ac_enrl_001_10_ordered_refusals_write_nothing(
    client: AsyncClient, db: AsyncSession, submission
) -> None:
    term = submission["term"]
    student = submission["student"]
    profile = await db.get(StudentProfile, student.id)
    assert profile is not None
    profile.has_financial_hold = True
    profile.has_academic_hold = True
    term.regular_enrollment_end = datetime.now(UTC) - timedelta(hours=1)
    db.add(
        StudentBillingAccount(
            student_id=student.id,
            academic_term_id=term.id,
            gross_amount=Decimal("1500.50"),
            fhe_subsidy_amount=Decimal("0"),
            amount_paid=Decimal("0"),
            payment_status="unpaid",
            due_date=term.regular_enrollment_end.date(),
        )
    )
    db.add(
        StudentTermGPA(
            student_id=student.id,
            academic_term_id=term.id,
            total_gpa_units=Decimal("18"),
            term_gpa=Decimal("5"),
            cumulative_gpa=Decimal("5"),
            academic_standing="University Dismissal",
        )
    )
    await db.commit()
    headers = await _headers(client, "2026-00169-MN-0")
    body = _body(submission)

    closed = await client.post(URL, headers=headers, json=body)
    assert closed.status_code == 409
    assert closed.json()["code"] == "ENROLL_PERIOD_CLOSED"

    term.regular_enrollment_end = datetime.now(UTC) + timedelta(days=1)
    await db.commit()
    financial = await client.post(URL, headers=headers, json=body)
    assert financial.status_code == 409
    financial_data = financial.json()
    assert financial_data["code"] == "ENROLL_FINANCIAL_HOLD"
    assert "PHP 1500.50" in financial_data["message"]
    assert "Accounting Office" in financial_data["message"]
    assert financial_data["details"] == {
        "office": "Accounting Office",
        "outstandingAmount": "1500.50",
    }

    profile.has_financial_hold = False
    await db.commit()
    administrative = await client.post(URL, headers=headers, json=body)
    assert administrative.status_code == 409
    assert administrative.json()["code"] == "ENROLL_HOLD_ACTIVE"

    profile.has_academic_hold = False
    await db.commit()
    standing = await client.post(URL, headers=headers, json=body)
    assert standing.status_code == 409
    assert standing.json()["code"] == "ENROLL_STANDING_BLOCKED"

    assert await db.scalar(select(func.count()).select_from(TermEnrollment)) == 0
    assert (
        await db.scalar(select(func.count()).select_from(StudentCourseEnrollment)) == 0
    )
    assert await db.scalar(select(func.count()).select_from(AuditEntry)) == 0
    assert (
        await db.scalar(
            select(func.count())
            .select_from(OutboxMessage)
            .where(OutboxMessage.event_name == ENROLLMENT_SUBMITTED_EVENT)
        )
        == 0
    )


@pytest.mark.parametrize(
    ("hold_type", "office"),
    [
        ("ADMINISTRATIVE", "Office of the University Registrar"),
        ("DISCIPLINARY", "Office of Student Services"),
    ],
)
async def test_ac_enrl_001_13_administrative_and_disciplinary_holds_name_office(
    client: AsyncClient,
    db: AsyncSession,
    submission,
    hold_type: str,
    office: str,
) -> None:
    db.add(
        StudentHold(
            student_id=submission["student"].id,
            hold_type=hold_type,
            office=office,
            description=f"Unresolved {hold_type.lower()} hold",
            blocks_enrollment=True,
        )
    )
    await db.commit()

    response = await client.post(
        URL,
        headers=await _headers(client, "2026-00169-MN-0"),
        json=_body(submission),
    )

    assert response.status_code == 409, response.text
    data = response.json()
    assert data["code"] == "ENROLL_HOLD_ACTIVE"
    assert office in data["message"]
    assert data["details"] == {"office": office, "holdType": hold_type}
    assert await db.scalar(select(func.count()).select_from(TermEnrollment)) == 0
    assert (
        await db.scalar(select(func.count()).select_from(StudentCourseEnrollment)) == 0
    )


async def test_ac_enrl_001_10_conflict_refusal_rolls_back_all_rows(
    client: AsyncClient, db: AsyncSession, submission
) -> None:
    second_meeting = await db.scalar(
        select(SectionSchedule).where(
            SectionSchedule.section_id == submission["sections"][1].id
        )
    )
    assert second_meeting is not None
    second_meeting.day_of_week = 0
    second_meeting.start_time = time(9, 30)
    second_meeting.end_time = time(10, 30)
    await db.commit()

    response = await client.post(
        URL,
        headers=await _headers(client, "2026-00169-MN-0"),
        json=_body(submission),
    )

    assert response.status_code == 409
    assert response.json()["code"] == "ENROLL_SCHEDULE_CONFLICT"
    assert await db.scalar(select(func.count()).select_from(TermEnrollment)) == 0
    assert (
        await db.scalar(select(func.count()).select_from(StudentCourseEnrollment)) == 0
    )


async def test_ac_enrl_001_10_assessment_failure_takes_no_seat(
    client: AsyncClient, db: AsyncSession, submission
) -> None:
    db.add(
        StudentTermGPA(
            student_id=submission["student"].id,
            academic_term_id=submission["term"].id,
            total_gpa_units=Decimal("18"),
            term_gpa=Decimal("2.75"),
            cumulative_gpa=Decimal("2.75"),
            academic_standing="Warning Level 2",
        )
    )
    await db.commit()

    response = await client.post(
        URL,
        headers=await _headers(client, "2026-00169-MN-0"),
        json=_body(submission),
    )

    assert response.status_code == 409
    assert response.json()["code"] == "ENROLL_UNIT_CEILING_EXCEEDED"
    assert response.json()["details"]["attemptedUnits"] == 6
    assert await db.scalar(select(func.count()).select_from(TermEnrollment)) == 0
    assert (
        await db.scalar(select(func.count()).select_from(StudentCourseEnrollment)) == 0
    )


async def test_ac_enrl_001_10_missing_fee_structure_refuses_with_a_conflict(
    db: AsyncSession, submission
) -> None:
    await db.execute(delete(FeeStructure))
    await db.commit()

    with pytest.raises(EnrollmentAssessmentUnavailableError) as refused:
        await enrollment_service.submit_enrollment(
            db,
            student_id=submission["student"].id,
            term=AcademicTermRead.model_validate(submission["term"]),
            section_ids=[section.id for section in submission["sections"]],
            client_ip="127.0.0.1",
        )

    assert refused.value.code == "ENROLL_ASSESSMENT_UNAVAILABLE"
    assert await db.scalar(select(func.count()).select_from(TermEnrollment)) == 0
    assert (
        await db.scalar(select(func.count()).select_from(StudentCourseEnrollment)) == 0
    )


async def test_ac_enrl_001_10_assessment_persistence_failure_rolls_back_enrollment(
    db: AsyncSession, submission, monkeypatch
) -> None:
    stage_assessment = billing.stage_assessment

    async def fail_assessment(*args, **kwargs):
        await stage_assessment(*args, **kwargs)
        raise billing.AssessmentInvalid

    monkeypatch.setattr(billing, "stage_assessment", fail_assessment)

    with pytest.raises(EnrollmentAssessmentUnavailableError):
        await enrollment_service.submit_enrollment(
            db,
            student_id=submission["student"].id,
            term=AcademicTermRead.model_validate(submission["term"]),
            section_ids=[section.id for section in submission["sections"]],
            client_ip="127.0.0.1",
        )

    assert await db.scalar(select(func.count()).select_from(TermEnrollment)) == 0
    assert (
        await db.scalar(select(func.count()).select_from(StudentCourseEnrollment)) == 0
    )
    assert await db.scalar(select(func.count()).select_from(StudentBillingAccount)) == 0
    assert await db.scalar(select(func.count()).select_from(AuditEntry)) == 0
    assert (
        await db.scalar(
            select(func.count())
            .select_from(OutboxMessage)
            .where(OutboxMessage.event_name == ENROLLMENT_SUBMITTED_EVENT)
        )
        == 0
    )


async def test_ac_enrl_001_12_replay_returns_original_without_second_seat(
    client: AsyncClient, db: AsyncSession, submission
) -> None:
    headers = await _headers(client, "2026-00169-MN-0")
    first = await client.post(URL, headers=headers, json=_body(submission))
    second = await client.post(URL, headers=headers, json=_body(submission))

    assert first.status_code == second.status_code == 201
    assert first.json() == second.json()
    assert await db.scalar(select(func.count()).select_from(TermEnrollment)) == 1
    assert (
        await db.scalar(select(func.count()).select_from(StudentCourseEnrollment)) == 2
    )
    assert await db.scalar(select(func.count()).select_from(AuditEntry)) == 1
    assert (
        await db.scalar(
            select(func.count())
            .select_from(OutboxMessage)
            .where(OutboxMessage.event_name == ENROLLMENT_SUBMITTED_EVENT)
        )
        == 1
    )


async def test_ac_enrl_001_13_session_owner_ignores_body_student_id(
    client: AsyncClient, db: AsyncSession, submission
) -> None:
    response = await client.post(
        URL,
        headers=await _headers(client, "2026-00169-MN-0"),
        json=_body(submission, studentId=str(submission["other"].id)),
    )

    assert response.status_code == 201, response.text
    row = await db.scalar(select(TermEnrollment))
    assert row is not None
    assert row.student_id == submission["student"].id
    assert row.student_id != submission["other"].id


async def test_ac_enrl_001_14_audit_and_confirmation_are_atomic_and_append_only(
    client: AsyncClient, db: AsyncSession, submission
) -> None:
    response = await client.post(
        URL,
        headers=await _headers(client, "2026-00169-MN-0"),
        json=_body(submission),
    )
    assert response.status_code == 201, response.text

    audit = await db.scalar(select(AuditEntry))
    assert audit is not None
    assert audit.action == ENROLLMENT_SUBMITTED_ACTION
    assert audit.actor_id == submission["student"].id
    assert audit.target_type == "term_enrollment"
    assert audit.target_id == response.json()["enrollmentId"]
    assert audit.prior_state == {"status": None, "sectionIds": []}
    assert audit.new_state["status"] == ENROLLED_PENDING
    assert audit.payload["role"] == "student"
    assert audit.payload["timestamp"].endswith("+00:00")
    assert audit.payload["ip_address"] == "127.0.0.1"

    message = await db.scalar(
        select(OutboxMessage).where(
            OutboxMessage.event_name == ENROLLMENT_SUBMITTED_EVENT
        )
    )
    assert message is not None
    assert message.event_name == ENROLLMENT_SUBMITTED_EVENT
    assert message.payload["user_id"] == str(submission["student"].id)
    assert message.payload["enrollment_id"] == response.json()["enrollmentId"]

    with pytest.raises(PermissionError, match="append-only"):
        await db.delete(audit)
        await db.flush()
    await db.rollback()


async def test_ac_enrl_001_11_last_seat_never_oversells_in_100_submits(
    tmp_path,
) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'race.db'}")
    session_factory = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    cases = []
    async with session_factory() as setup_db:
        program, curriculum, _ = await _program_curriculum(
            setup_db, code="RACE", semester="Race", course_count=0
        )
        for index in range(100):
            course = Course(
                course_code=f"RACE{index:03}",
                title=f"Race Course {index}",
                lecture_units=Decimal("1"),
                lab_units=Decimal("0"),
                is_nstp=False,
                is_active=True,
            )
            setup_db.add(course)
            await setup_db.flush()
            setup_db.add(
                CurriculumCourse(
                    curriculum_id=curriculum.id,
                    course_id=course.id,
                    year_level=2,
                    semester="Race",
                )
            )
            term = await _term(setup_db, year=f"RACE-{index:03}", semester="Race")
            await _fee_structure(setup_db, program=program, term=term)
            first = await _student(
                setup_db,
                program=program,
                curriculum=curriculum,
                number=f"2026-{index * 2 + 20000:05}-RC-0",
                password_hash="not-used-by-service-test",
            )
            second = await _student(
                setup_db,
                program=program,
                curriculum=curriculum,
                number=f"2026-{index * 2 + 20001:05}-RC-0",
                password_hash="not-used-by-service-test",
            )
            section = await _section(
                setup_db,
                term=term,
                course=course,
                name=f"RACE-{index:03}",
                day=0,
                start=time(9),
                end=time(10),
                capacity=1,
            )
            cases.append(
                (AcademicTermRead.model_validate(term), first, second, section)
            )
        await setup_db.commit()

    try:
        for term, first, second, section in cases:
            async with session_factory() as first_db, session_factory() as second_db:
                results = await asyncio.gather(
                    enrollment_service.submit_enrollment(
                        first_db,
                        student_id=first.id,
                        term=term,
                        section_ids=[section.id],
                        client_ip="127.0.0.1",
                    ),
                    enrollment_service.submit_enrollment(
                        second_db,
                        student_id=second.id,
                        term=term,
                        section_ids=[section.id],
                        client_ip="127.0.0.2",
                    ),
                    return_exceptions=True,
                )
            successful = [
                result for result in results if not isinstance(result, Exception)
            ]
            refused = [
                result
                for result in results
                if isinstance(result, EnrollmentSectionFullError)
            ]
            assert len(successful) == 1
            assert successful[0].status == ENROLLED_PENDING
            assert len(refused) == 1
            assert refused[0].status_code == 409
            assert refused[0].code == "ENROLL_SECTION_FULL"
            async with session_factory() as verify_db:
                enrolled_count = await verify_db.scalar(
                    select(func.count())
                    .select_from(StudentCourseEnrollment)
                    .where(StudentCourseEnrollment.section_id == section.id)
                )
                assert enrolled_count == section.capacity == 1
    finally:
        await engine.dispose()
