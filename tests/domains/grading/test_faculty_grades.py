"""Faculty grade submission tests (US-FAC-001: AC-FAC-001.1, .2, .9, .10, .11)."""

from __future__ import annotations

import time
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_database.models.academics import AcademicTerm
from cbpupsis_database.models.audit import AuditEntry
from cbpupsis_database.models.curriculum import Course, Program
from cbpupsis_database.models.enrollment import StudentCourseEnrollment, TermEnrollment
from cbpupsis_database.models.grading import SectionGradeSubmission, StudentGrade
from cbpupsis_database.models.outbox import OutboxMessage
from cbpupsis_database.models.sections import CourseSection
from cbpupsis_database.models.users import (
    FacultyProfile,
    StudentProfile,
    User,
    UserProfile,
)
from cbpupsis_shared.domains.audit.schemas import render_in_utc8
from cbpupsis_shared.domains.auth.dependencies import CurrentUser, get_current_user
from cbpupsis_shared.domains.auth.security import hash_password, totp_at
from cbpupsis_shared.domains.grading.constants import (
    GRADE_DRAFT_SAVED_ACTION,
    GRADES_FINALIZED_ACTION,
    GRADES_FINALIZED_EVENT,
    READ_GRADE,
    SUBMIT_GRADE,
    UPDATE_GRADE,
)
from cbpupsis_shared.domains.notifications.templates import render
from cbpupsis_shared.domains.sections.constants import READ_COURSE_SECTION
from main import app

FACULTY_AUTH = "/api/v1/auth/faculty"
STUDENT_AUTH = "/api/v1/auth/student"
PASSWORD = "Faculty-Password-123!"
STUDENT_PASSWORD = "Student-Password-123!"
STUDENT_BIRTHDATE = date(2004, 5, 14)


async def _faculty(db: AsyncSession, employee_id: str, email: str) -> User:
    user = User(
        email=email,
        password_hash=hash_password(PASSWORD),
        is_active=True,
        email_verified_at=datetime.now(UTC),
    )
    db.add(user)
    await db.flush()
    db.add(FacultyProfile(user_id=user.id, employee_id=employee_id, department="CS"))
    await db.commit()
    return user


async def _faculty_headers(client: AsyncClient, employee_id: str) -> dict[str, str]:
    challenge = (
        await client.post(
            f"{FACULTY_AUTH}/login",
            json={"employee_id": employee_id, "password": PASSWORD},
        )
    ).json()["challenge_token"]
    secret = (
        await client.post(
            f"{FACULTY_AUTH}/mfa/totp/enroll", json={"challenge_token": challenge}
        )
    ).json()["secret"]
    confirmed = await client.post(
        f"{FACULTY_AUTH}/mfa/totp/confirm",
        json={
            "challenge_token": challenge,
            "code": totp_at(secret, int(datetime.now(UTC).timestamp()) // 30),
        },
    )
    assert confirmed.status_code == 200, confirmed.text
    return {"Authorization": f"Bearer {confirmed.json()['access_token']}"}


async def _student_headers(client: AsyncClient, student_number: str) -> dict[str, str]:
    response = await client.post(
        f"{STUDENT_AUTH}/login",
        json={
            "student_number": student_number,
            "birthdate": STUDENT_BIRTHDATE.isoformat(),
            "password": STUDENT_PASSWORD,
        },
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


async def _create_student(
    db: AsyncSession,
    *,
    program_id: uuid.UUID,
    number: str,
    name: str,
    email: str,
    password_hash: str | None = None,
) -> User:
    parts = name.split(" ", 1)
    user = User(
        email=email,
        password_hash=password_hash or "!student-grade-fixture",
        full_name=name,
        is_active=True,
    )
    db.add(user)
    await db.flush()
    db.add(
        UserProfile(
            user_id=user.id,
            first_name=parts[0],
            last_name=parts[1] if len(parts) > 1 else "Student",
            birthdate=STUDENT_BIRTHDATE,
        )
    )
    db.add(
        StudentProfile(
            user_id=user.id,
            student_number=number,
            program_id=program_id,
            year_level=3,
            enrollment_status="enrolled",
        )
    )
    return user


async def _enroll(
    db: AsyncSession,
    *,
    student_id: uuid.UUID,
    term_id: uuid.UUID,
    section_id: uuid.UUID,
    status: str = "enrolled",
) -> StudentCourseEnrollment:
    term_enrollment = TermEnrollment(
        student_id=student_id,
        academic_term_id=term_id,
        enrollment_status="enrolled",
    )
    db.add(term_enrollment)
    await db.flush()
    item = StudentCourseEnrollment(
        term_enrollment_id=term_enrollment.id,
        academic_term_id=term_id,
        section_id=section_id,
        status=status,
    )
    db.add(item)
    await db.flush()
    return item


def _without_request_id(body: dict) -> dict:
    return {k: v for k, v in body.items() if k != "request_id"}


@pytest.fixture
async def grading_setup(db: AsyncSession, grant):
    faculty = await _faculty(db, "2021-00911-MN-0", "grader@pup.edu.ph")
    other_faculty = await _faculty(db, "2021-00912-MN-0", "other.grader@pup.edu.ph")
    for perm in (READ_COURSE_SECTION, UPDATE_GRADE, SUBMIT_GRADE):
        await grant(faculty.id, perm)

    now = datetime.now(UTC)
    open_term = AcademicTerm(
        academic_year="2026-2027",
        semester="First",
        term_type="REGULAR",
        start_date=date(2026, 8, 1),
        end_date=date(2026, 12, 15),
        is_current=True,
        regular_enrollment_start=now - timedelta(days=30),
        regular_enrollment_end=now + timedelta(days=30),
        add_drop_deadline=now + timedelta(days=10),
        grade_submission_deadline=now + timedelta(days=14),
    )
    closed_term = AcademicTerm(
        academic_year="2025-2026",
        semester="Second",
        term_type="REGULAR",
        start_date=date(2026, 1, 15),
        end_date=date(2026, 5, 30),
        is_current=False,
        regular_enrollment_start=now - timedelta(days=180),
        regular_enrollment_end=now - timedelta(days=150),
        add_drop_deadline=now - timedelta(days=140),
        grade_submission_deadline=now - timedelta(days=1),
    )
    program = Program(
        code="BSCS",
        name="Bachelor of Science in Computer Science",
        department="Computer Science",
        college="CCIS",
        total_units_required=Decimal("150.0"),
    )
    course = Course(
        course_code="COMP 301",
        title="Algorithms and Complexity",
        lecture_units=Decimal("3.0"),
        lab_units=Decimal("0.0"),
        is_nstp=False,
    )
    db.add_all([open_term, closed_term, program, course])
    await db.flush()

    own_section = CourseSection(
        academic_term_id=open_term.id,
        course_id=course.id,
        faculty_id=faculty.id,
        section_name="BSCS 3-1",
        capacity=50,
    )
    closed_section = CourseSection(
        academic_term_id=closed_term.id,
        course_id=course.id,
        faculty_id=faculty.id,
        section_name="BSCS 3-1-CLOSED",
        capacity=50,
    )
    foreign_section = CourseSection(
        academic_term_id=open_term.id,
        course_id=course.id,
        faculty_id=other_faculty.id,
        section_name="BSCS 3-2",
        capacity=50,
    )
    db.add_all([own_section, closed_section, foreign_section])
    await db.flush()

    student_pw = hash_password(STUDENT_PASSWORD)
    alice = await _create_student(
        db,
        program_id=program.id,
        number="2024-00101-MN-0",
        name="Alice Abad",
        email="alice.g@iskolarngbayan.pup.edu.ph",
        password_hash=student_pw,
    )
    bob = await _create_student(
        db,
        program_id=program.id,
        number="2024-00102-MN-0",
        name="Bob Bautista",
        email="bob.g@iskolarngbayan.pup.edu.ph",
        password_hash=student_pw,
    )
    dropped = await _create_student(
        db,
        program_id=program.id,
        number="2024-00103-MN-0",
        name="Cara Cruz",
        email="cara.g@iskolarngbayan.pup.edu.ph",
    )
    await grant(alice.id, READ_GRADE)
    # Bob shares the same ReadGrade permission via direct policy or group;
    # grant() creates the permission row, so attach the already-created ReadGrade.
    from cbpupsis_database.models.iam import Policy

    read_grade_policy = await db.scalar(
        select(Policy).where(Policy.name == "policy-ReadGrade")
    )
    assert read_grade_policy is not None
    from cbpupsis_shared.domains.iam import service as iam_service

    await iam_service.attach_policy_to_user(db, bob.id, read_grade_policy.id)

    alice_enr = await _enroll(
        db, student_id=alice.id, term_id=open_term.id, section_id=own_section.id
    )
    bob_enr = await _enroll(
        db, student_id=bob.id, term_id=open_term.id, section_id=own_section.id
    )
    dropped_enr = await _enroll(
        db,
        student_id=dropped.id,
        term_id=open_term.id,
        section_id=own_section.id,
        status="dropped",
    )
    closed_alice_enr = await _enroll(
        db, student_id=alice.id, term_id=closed_term.id, section_id=closed_section.id
    )
    await db.commit()

    return {
        "faculty_id": faculty.id,
        "other_faculty_id": other_faculty.id,
        "open_term_id": open_term.id,
        "closed_term_id": closed_term.id,
        "program_id": program.id,
        "course_id": course.id,
        "own_id": own_section.id,
        "closed_id": closed_section.id,
        "foreign_id": foreign_section.id,
        "alice_id": alice.id,
        "bob_id": bob.id,
        "dropped_id": dropped.id,
        "alice_enr_id": alice_enr.id,
        "bob_enr_id": bob_enr.id,
        "dropped_enr_id": dropped_enr.id,
        "closed_alice_enr_id": closed_alice_enr.id,
    }


async def test_ac_fac_001_1_save_draft_and_reread_editable(
    client: AsyncClient, db: AsyncSession, grading_setup
) -> None:
    setup = grading_setup
    headers = await _faculty_headers(client, "2021-00911-MN-0")
    section_id = setup["own_id"]

    initial = await client.get(
        f"/api/v1/faculty/sections/{section_id}/grades", headers=headers
    )
    assert initial.status_code == 200, initial.text
    init_body = initial.json()
    assert init_body["state"] == "DRAFT"
    assert init_body["version"] == 1
    assert init_body["deadlinePassed"] is False
    assert [g["studentNumber"] for g in init_body["grades"]] == [
        "2024-00101-MN-0",
        "2024-00102-MN-0",
    ]

    first_save = await client.put(
        f"/api/v1/faculty/sections/{section_id}/grades/draft",
        headers={**headers, "X-Forwarded-For": "203.0.113.10"},
        json={
            "version": 1,
            "grades": [
                {"enrollmentId": str(setup["alice_enr_id"]), "finalGrade": "1.50"},
                {"studentId": str(setup["bob_id"]), "finalGrade": "2.00"},
            ],
        },
    )
    assert first_save.status_code == 200, first_save.text
    saved_body = first_save.json()
    assert saved_body["state"] == "DRAFT"
    assert saved_body["version"] == 2
    by_num = {g["studentNumber"]: g for g in saved_body["grades"]}
    assert by_num["2024-00101-MN-0"]["finalGrade"] == "1.50"
    assert by_num["2024-00101-MN-0"]["isDraft"] is True
    assert by_num["2024-00101-MN-0"]["isLocked"] is False
    assert by_num["2024-00102-MN-0"]["finalGrade"] == "2.00"

    reread = await client.get(
        f"/api/v1/faculty/sections/{section_id}/grades", headers=headers
    )
    assert reread.status_code == 200
    assert reread.json()["version"] == 2
    assert reread.json()["state"] == "DRAFT"

    second_save = await client.put(
        f"/api/v1/faculty/sections/{section_id}/grades/draft",
        headers={**headers, "X-Forwarded-For": "203.0.113.11"},
        json={
            "version": 2,
            "grades": [
                {"enrollmentId": str(setup["alice_enr_id"]), "finalGrade": "1.25"},
                {"enrollmentId": str(setup["bob_enr_id"]), "finalGrade": "Inc"},
            ],
        },
    )
    assert second_save.status_code == 200, second_save.text
    assert second_save.json()["version"] == 3
    by_num_2 = {g["studentNumber"]: g for g in second_save.json()["grades"]}
    assert by_num_2["2024-00101-MN-0"]["finalGrade"] == "1.25"
    assert by_num_2["2024-00102-MN-0"]["finalGrade"] == "INC"

    audits = list(
        await db.scalars(
            select(AuditEntry)
            .where(AuditEntry.action == GRADE_DRAFT_SAVED_ACTION)
            .order_by(AuditEntry.occurred_at.asc())
        )
    )
    assert len(audits) == 2
    second_audit = audits[1]
    assert second_audit.actor_id == setup["faculty_id"]
    assert second_audit.payload["role"] == "faculty"
    assert second_audit.payload["ip_address"] == "203.0.113.11"
    assert second_audit.payload["timestampUtc8"].endswith("+08:00")
    assert render_in_utc8(second_audit.occurred_at).endswith("+08:00")
    assert (
        second_audit.prior_state["marks"][str(setup["alice_enr_id"])]["finalGrade"]
        == "1.50"
    )
    assert (
        second_audit.new_state["marks"][str(setup["alice_enr_id"])]["finalGrade"]
        == "1.25"
    )


@pytest.mark.parametrize("bad_mark", ["0.75", "5.50", "4.00", "3.50", "W", "INVALID"])
async def test_ac_fac_001_1_invalid_mark_returns_422_and_saves_nothing(
    client: AsyncClient, db: AsyncSession, grading_setup, bad_mark: str
) -> None:
    setup = grading_setup
    headers = await _faculty_headers(client, "2021-00911-MN-0")
    section_id = setup["own_id"]

    valid_draft = await client.put(
        f"/api/v1/faculty/sections/{section_id}/grades/draft",
        headers=headers,
        json={
            "version": 1,
            "grades": [
                {"enrollmentId": str(setup["alice_enr_id"]), "finalGrade": "1.75"}
            ],
        },
    )
    assert valid_draft.status_code == 200

    bad = await client.put(
        f"/api/v1/faculty/sections/{section_id}/grades/draft",
        headers=headers,
        json={
            "version": 2,
            "grades": [
                {"enrollmentId": str(setup["alice_enr_id"]), "finalGrade": "1.25"},
                {"enrollmentId": str(setup["bob_enr_id"]), "finalGrade": bad_mark},
            ],
        },
    )
    assert bad.status_code == 422, bad.text
    body = bad.json()
    assert body["code"] == "VALIDATION_ERROR"
    assert "2024-00102-MN-0" in body["detail"]
    assert body["errors"][0]["studentId"] == str(setup["bob_id"])
    assert body["errors"][0]["enrollmentId"] == str(setup["bob_enr_id"])

    reread = await client.get(
        f"/api/v1/faculty/sections/{section_id}/grades", headers=headers
    )
    assert reread.json()["version"] == 2
    by_num = {g["studentNumber"]: g for g in reread.json()["grades"]}
    assert by_num["2024-00101-MN-0"]["finalGrade"] == "1.75"
    assert by_num["2024-00102-MN-0"]["finalGrade"] is None


async def test_ac_fac_001_1_stale_version_returns_409_and_saves_nothing(
    client: AsyncClient, grading_setup
) -> None:
    setup = grading_setup
    headers = await _faculty_headers(client, "2021-00911-MN-0")
    section_id = setup["own_id"]

    first = await client.put(
        f"/api/v1/faculty/sections/{section_id}/grades/draft",
        headers=headers,
        json={
            "version": 1,
            "grades": [
                {"enrollmentId": str(setup["alice_enr_id"]), "finalGrade": "1.50"}
            ],
        },
    )
    assert first.status_code == 200

    stale = await client.put(
        f"/api/v1/faculty/sections/{section_id}/grades/draft",
        headers=headers,
        json={
            "version": 1,
            "grades": [
                {"enrollmentId": str(setup["alice_enr_id"]), "finalGrade": "3.00"}
            ],
        },
    )
    assert stale.status_code == 409, stale.text
    assert stale.json()["code"] == "VERSION_CONFLICT"

    reread = await client.get(
        f"/api/v1/faculty/sections/{section_id}/grades", headers=headers
    )
    assert reread.json()["version"] == 2
    by_num = {g["studentNumber"]: g for g in reread.json()["grades"]}
    assert by_num["2024-00101-MN-0"]["finalGrade"] == "1.50"


async def test_ac_fac_001_2_finalize_locks_grades_and_shows_to_student(
    client: AsyncClient, db: AsyncSession, grading_setup
) -> None:
    setup = grading_setup
    headers = await _faculty_headers(client, "2021-00911-MN-0")
    section_id = setup["own_id"]

    # Missing mark cannot finalize; section stays DRAFT.
    incomplete_finalize = await client.post(
        f"/api/v1/faculty/sections/{section_id}/grades/finalize", headers=headers
    )
    assert incomplete_finalize.status_code == 422
    assert incomplete_finalize.json()["code"] == "VALIDATION_ERROR"

    draft = await client.put(
        f"/api/v1/faculty/sections/{section_id}/grades/draft",
        headers=headers,
        json={
            "version": 1,
            "grades": [
                {"enrollmentId": str(setup["alice_enr_id"]), "finalGrade": "1.25"},
                {"enrollmentId": str(setup["bob_enr_id"]), "finalGrade": "INC"},
            ],
        },
    )
    assert draft.status_code == 200

    finalized = await client.post(
        f"/api/v1/faculty/sections/{section_id}/grades/finalize",
        headers={**headers, "X-Forwarded-For": "198.51.100.7"},
        json={"version": 2},
    )
    assert finalized.status_code == 200, finalized.text
    fin_body = finalized.json()
    assert fin_body["state"] == "FINAL"
    assert fin_body["recordsCommitted"] == 2
    assert fin_body["notificationsQueued"] == 2

    # Any later write is refused with 409 GRADES_LOCKED.
    retry_draft = await client.put(
        f"/api/v1/faculty/sections/{section_id}/grades/draft",
        headers=headers,
        json={
            "version": 3,
            "grades": [
                {"enrollmentId": str(setup["alice_enr_id"]), "finalGrade": "2.00"}
            ],
        },
    )
    assert retry_draft.status_code == 409
    assert retry_draft.json()["code"] == "GRADES_LOCKED"

    retry_finalize = await client.post(
        f"/api/v1/faculty/sections/{section_id}/grades/finalize",
        headers=headers,
    )
    assert retry_finalize.status_code == 409
    assert retry_finalize.json()["code"] == "GRADES_LOCKED"

    # Students can now read finalized marks (including Inc with 365-day deadline).
    alice_headers = await _student_headers(client, "2024-00101-MN-0")
    alice_grades = await client.get("/api/v1/student/grades", headers=alice_headers)
    assert alice_grades.status_code == 200, alice_grades.text
    assert alice_grades.json()["courses"][0]["final_grade"] == "1.25"

    bob_headers = await _student_headers(client, "2024-00102-MN-0")
    bob_grades = await client.get("/api/v1/student/grades", headers=bob_headers)
    assert bob_grades.status_code == 200, bob_grades.text
    bob_course = bob_grades.json()["courses"][0]
    assert bob_course["final_grade"] == "Inc"
    posted = datetime.fromisoformat(bob_course["posted_at"])
    deadline = datetime.fromisoformat(bob_course["completion_deadline"])
    assert deadline - posted == timedelta(days=365)

    audits = list(
        await db.scalars(
            select(AuditEntry).where(AuditEntry.action == GRADES_FINALIZED_ACTION)
        )
    )
    assert len(audits) == 1
    assert audits[0].actor_id == setup["faculty_id"]
    assert audits[0].payload["role"] == "faculty"
    assert audits[0].payload["ip_address"] == "198.51.100.7"
    assert audits[0].payload["timestampUtc8"].endswith("+08:00")


async def test_ac_fac_001_9_finalize_after_deadline_returns_403(
    client: AsyncClient, db: AsyncSession, grading_setup
) -> None:
    setup = grading_setup
    headers = await _faculty_headers(client, "2021-00911-MN-0")

    open_dl = await client.get(
        f"/api/v1/faculty/sections/{setup['own_id']}/grade-deadline", headers=headers
    )
    assert open_dl.status_code == 200
    assert open_dl.json()["passed"] is False

    closed_id = setup["closed_id"]
    closed_dl = await client.get(
        f"/api/v1/faculty/sections/{closed_id}/grade-deadline", headers=headers
    )
    assert closed_dl.status_code == 200
    assert closed_dl.json()["passed"] is True

    draft = await client.put(
        f"/api/v1/faculty/sections/{closed_id}/grades/draft",
        headers=headers,
        json={
            "version": 1,
            "grades": [
                {
                    "enrollmentId": str(setup["closed_alice_enr_id"]),
                    "finalGrade": "1.75",
                }
            ],
        },
    )
    assert draft.status_code == 200

    refused = await client.post(
        f"/api/v1/faculty/sections/{closed_id}/grades/finalize",
        headers=headers,
    )
    assert refused.status_code == 403, refused.text
    body = refused.json()
    assert body["code"] == "GRADE_DEADLINE_PASSED"
    assert "deadline" in body
    assert "override" in body["detail"].lower()

    outbox = list(
        await db.scalars(
            select(OutboxMessage).where(
                OutboxMessage.event_name == GRADES_FINALIZED_EVENT
            )
        )
    )
    assert outbox == []


async def test_ac_fac_001_10_foreign_and_missing_sections_return_404(
    client: AsyncClient, grading_setup
) -> None:
    setup = grading_setup
    headers = await _faculty_headers(client, "2021-00911-MN-0")
    missing_id = uuid.uuid4()
    foreign_id = setup["foreign_id"]

    calls = [
        ("GET", "/grades", None),
        ("GET", "/grade-deadline", None),
        ("PUT", "/grades/draft", {"version": 1, "grades": []}),
        ("POST", "/grades/finalize", None),
    ]
    for method, suffix, json_body in calls:
        foreign = await client.request(
            method,
            f"/api/v1/faculty/sections/{foreign_id}{suffix}",
            headers=headers,
            json=json_body,
        )
        missing = await client.request(
            method,
            f"/api/v1/faculty/sections/{missing_id}{suffix}",
            headers=headers,
            json=json_body,
        )
        assert foreign.status_code == missing.status_code == 404
        assert _without_request_id(foreign.json()) == _without_request_id(
            missing.json()
        )
        assert foreign.json()["code"] == "RESOURCE_NOT_FOUND"


@pytest.mark.parametrize(
    ("role", "mfa", "status", "code"),
    (
        ("student", True, 403, "AUTH_INSUFFICIENT_ROLE"),
        ("faculty", False, 401, "AUTH_MFA_REQUIRED"),
    ),
)
async def test_ac_fac_001_10_role_and_mfa_guards(
    client: AsyncClient, grading_setup, role: str, mfa: bool, status: int, code: str
) -> None:
    principal = CurrentUser(
        id=uuid.uuid4(),
        email="wrong-role@example.com",
        role=role,
        claims={"mfa": mfa},
    )
    app.dependency_overrides[get_current_user] = lambda: principal
    try:
        resp = await client.get(
            f"/api/v1/faculty/sections/{grading_setup['own_id']}/grades"
        )
    finally:
        app.dependency_overrides.pop(get_current_user, None)
    assert resp.status_code == status
    assert resp.json()["code"] == code


async def test_ac_fac_001_10_missing_permissions_return_403(
    client: AsyncClient, grading_setup
) -> None:
    headers = await _faculty_headers(client, "2021-00912-MN-0")
    section_id = grading_setup["foreign_id"]
    assert (
        await client.get(
            f"/api/v1/faculty/sections/{section_id}/grades", headers=headers
        )
    ).status_code == 403
    assert (
        await client.put(
            f"/api/v1/faculty/sections/{section_id}/grades/draft",
            headers=headers,
            json={"version": 1, "grades": []},
        )
    ).status_code == 403
    assert (
        await client.post(
            f"/api/v1/faculty/sections/{section_id}/grades/finalize",
            headers=headers,
        )
    ).status_code == 403


async def test_ac_fac_001_11_finalize_40_students_queues_40_notifications(
    client: AsyncClient, db: AsyncSession, grading_setup
) -> None:
    setup = grading_setup
    bulk_section = CourseSection(
        academic_term_id=setup["open_term_id"],
        course_id=setup["course_id"],
        faculty_id=setup["faculty_id"],
        section_name="BSCS 3-40",
        capacity=45,
    )
    db.add(bulk_section)
    await db.flush()
    bulk_section_id = bulk_section.id

    enrollments = []
    for index in range(40):
        student = await _create_student(
            db,
            program_id=setup["program_id"],
            number=f"2024-{index + 500:05d}-MN-0",
            name=f"Student {index:02d}",
            email=f"stu40.{index:02d}@iskolarngbayan.pup.edu.ph",
        )
        enr = await _enroll(
            db,
            student_id=student.id,
            term_id=setup["open_term_id"],
            section_id=bulk_section_id,
        )
        enrollments.append((student.id, enr.id))
    await db.commit()

    headers = await _faculty_headers(client, "2021-00911-MN-0")
    draft = await client.put(
        f"/api/v1/faculty/sections/{bulk_section_id}/grades/draft",
        headers=headers,
        json={
            "version": 1,
            "grades": [
                {"enrollmentId": str(enr_id), "finalGrade": "1.50"}
                for _, enr_id in enrollments
            ],
        },
    )
    assert draft.status_code == 200

    finalized = await client.post(
        f"/api/v1/faculty/sections/{bulk_section_id}/grades/finalize",
        headers=headers,
    )
    assert finalized.status_code == 200, finalized.text
    assert finalized.json()["recordsCommitted"] == 40
    assert finalized.json()["notificationsQueued"] == 40

    outbox = list(
        await db.scalars(
            select(OutboxMessage).where(
                OutboxMessage.event_name == GRADES_FINALIZED_EVENT
            )
        )
    )
    assert len(outbox) == 40
    assert {m.payload["user_id"] for m in outbox} == {
        str(sid) for sid, _ in enrollments
    }
    rendered = render(
        type(
            "E",
            (),
            {"name": GRADES_FINALIZED_EVENT, "payload": outbox[0].payload},
        )()
    )
    assert "COMP 301" in rendered.title


async def test_ac_fac_001_11_failed_finalize_leaves_section_draft(
    client: AsyncClient, db: AsyncSession, grading_setup
) -> None:
    setup = grading_setup
    headers = await _faculty_headers(client, "2021-00911-MN-0")
    section_id = setup["own_id"]
    alice_enr_id = setup["alice_enr_id"]
    bob_enr_id = setup["bob_enr_id"]

    draft = await client.put(
        f"/api/v1/faculty/sections/{section_id}/grades/draft",
        headers=headers,
        json={
            "version": 1,
            "grades": [
                {"enrollmentId": str(alice_enr_id), "finalGrade": "1.25"},
                {"enrollmentId": str(bob_enr_id), "finalGrade": "2.00"},
            ],
        },
    )
    assert draft.status_code == 200

    calls = 0

    def fail_on_second(session, event):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("simulated outbox failure mid-finalize")

    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as safe_client:
        with patch(
            "cbpupsis_shared.domains.grading.service.publish_transactional",
            side_effect=fail_on_second,
        ):
            failed = await safe_client.post(
                f"/api/v1/faculty/sections/{section_id}/grades/finalize",
                headers=headers,
            )
    assert failed.status_code == 500

    submission = await db.scalar(
        select(SectionGradeSubmission)
        .where(SectionGradeSubmission.section_id == section_id)
        .execution_options(populate_existing=True)
    )
    assert submission is not None
    assert submission.state == "DRAFT"

    grades = list(
        await db.scalars(
            select(StudentGrade)
            .where(StudentGrade.enrollment_item_id.in_([alice_enr_id, bob_enr_id]))
            .execution_options(populate_existing=True)
        )
    )
    assert all(g.is_draft and not g.is_locked for g in grades)

    outbox = list(
        await db.scalars(
            select(OutboxMessage).where(
                OutboxMessage.event_name == GRADES_FINALIZED_EVENT
            )
        )
    )
    assert outbox == []


async def test_ac_fac_001_1_draft_60_students_meets_speed_target(
    client: AsyncClient, db: AsyncSession, grading_setup
) -> None:
    setup = grading_setup
    perf_section = CourseSection(
        academic_term_id=setup["open_term_id"],
        course_id=setup["course_id"],
        faculty_id=setup["faculty_id"],
        section_name="BSCS 3-60",
        capacity=70,
    )
    db.add(perf_section)
    await db.flush()
    perf_section_id = perf_section.id

    enrollment_ids = []
    for index in range(60):
        student = await _create_student(
            db,
            program_id=setup["program_id"],
            number=f"2024-{index + 700:05d}-MN-0",
            name=f"Perf Student {index:02d}",
            email=f"perf.{index:02d}@iskolarngbayan.pup.edu.ph",
        )
        enr = await _enroll(
            db,
            student_id=student.id,
            term_id=setup["open_term_id"],
            section_id=perf_section_id,
        )
        enrollment_ids.append(enr.id)
    await db.commit()

    headers = await _faculty_headers(client, "2021-00911-MN-0")
    url = f"/api/v1/faculty/sections/{perf_section_id}/grades/draft"
    payload_rows = [
        {"enrollmentId": str(enr_id), "finalGrade": "1.75"} for enr_id in enrollment_ids
    ]

    warmup = await client.put(
        url, headers=headers, json={"version": 1, "grades": payload_rows}
    )
    assert warmup.status_code == 200
    version = warmup.json()["version"]

    durations = []
    for _ in range(10):
        started = time.perf_counter()
        response = await client.put(
            url, headers=headers, json={"version": version, "grades": payload_rows}
        )
        durations.append(time.perf_counter() - started)
        assert response.status_code == 200
        version = response.json()["version"]
    p90 = sorted(durations)[8]
    assert p90 <= 0.5, f"60-student grade draft save p90 was {p90:.3f}s"
