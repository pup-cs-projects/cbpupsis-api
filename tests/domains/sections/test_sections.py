"""US-102 section access (AC-102.6, AC-102.7): own sections only, 404 otherwise."""

from __future__ import annotations

import csv
import io
import time
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_database.models.academics import AcademicTerm
from cbpupsis_database.models.audit import AuditEntry
from cbpupsis_database.models.curriculum import Course, CoursePrerequisite, Program
from cbpupsis_database.models.enrollment import (
    AddDropRequest,
    AddDropRequestItem,
    StudentCourseEnrollment,
    TermEnrollment,
)
from cbpupsis_database.models.grading import StudentGrade
from cbpupsis_database.models.sections import CourseSection
from cbpupsis_database.models.users import FacultyProfile, StudentProfile, User
from cbpupsis_shared.domains.auth.dependencies import CurrentUser, get_current_user
from cbpupsis_shared.domains.auth.security import hash_password, totp_at
from cbpupsis_shared.domains.sections.constants import ACADEMIC_RECORD_DISCLOSED
from main import app

AUTH = "/api/v1/auth/faculty"
PASSWORD = "Faculty-Password-123!"


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


async def _session(client: AsyncClient, employee_id: str) -> dict[str, str]:
    challenge = (
        await client.post(
            f"{AUTH}/login", json={"employee_id": employee_id, "password": PASSWORD}
        )
    ).json()["challenge_token"]
    secret = (
        await client.post(
            f"{AUTH}/mfa/totp/enroll", json={"challenge_token": challenge}
        )
    ).json()["secret"]
    confirmed = await client.post(
        f"{AUTH}/mfa/totp/confirm",
        json={
            "challenge_token": challenge,
            "code": totp_at(secret, int(datetime.now(UTC).timestamp()) // 30),
        },
    )
    assert confirmed.status_code == 200, confirmed.text
    return {"Authorization": f"Bearer {confirmed.json()['access_token']}"}


def _section(faculty_id: uuid.UUID | None, name: str) -> CourseSection:
    return CourseSection(
        academic_term_id=uuid.uuid4(),
        course_id=uuid.uuid4(),
        faculty_id=faculty_id,
        section_name=name,
        capacity=40,
    )


@pytest.fixture
async def setup(db: AsyncSession, grant):
    mine = await _faculty(db, "2021-00456-MN-0", "mine@example.com")
    other = await _faculty(db, "2021-00457-MN-0", "other@example.com")
    await grant(mine.id, "ReadCourseSection")
    own = _section(mine.id, "BSCS 3-1")
    foreign = _section(other.id, "BSCS 3-2")
    db.add_all([own, foreign])
    await db.commit()
    return {"mine": mine, "other": other, "own": own, "foreign": foreign}


async def test_ac_102_6_lists_only_own_sections(client: AsyncClient, setup) -> None:
    headers = await _session(client, "2021-00456-MN-0")
    response = await client.get("/api/v1/faculty/sections", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 1
    assert [s["id"] for s in body["items"]] == [str(setup["own"].id)]


async def test_ac_102_7_someone_elses_section_answers_404(
    client: AsyncClient, setup
) -> None:
    headers = await _session(client, "2021-00456-MN-0")
    own = await client.get(
        f"/api/v1/faculty/sections/{setup['own'].id}/roster", headers=headers
    )
    assert own.status_code == 200 and own.json()["roster"] == []
    foreign = await client.get(
        f"/api/v1/faculty/sections/{setup['foreign'].id}/roster", headers=headers
    )
    missing = await client.get(
        f"/api/v1/faculty/sections/{uuid.uuid4()}/roster", headers=headers
    )
    assert foreign.status_code == missing.status_code == 404
    body_foreign, body_missing = foreign.json(), missing.json()
    body_foreign.pop("request_id")
    body_missing.pop("request_id")
    assert body_foreign == body_missing
    assert body_foreign["code"] == "RESOURCE_NOT_FOUND"


async def test_sections_need_the_permission(client: AsyncClient, setup) -> None:
    """The coarse check: a faculty session without ReadCourseSection is 403."""
    headers = await _session(client, "2021-00457-MN-0")
    response = await client.get("/api/v1/faculty/sections", headers=headers)
    assert response.status_code == 403


async def test_old_unscoped_sections_path_is_not_mounted(
    client: AsyncClient, setup
) -> None:
    headers = await _session(client, "2021-00456-MN-0")
    assert (await client.get("/api/v1/sections", headers=headers)).status_code == 404
    response = await client.get(
        f"/api/v1/sections/{setup['own'].id}/roster", headers=headers
    )
    assert response.status_code == 404


async def _student(
    db: AsyncSession,
    *,
    program_id: uuid.UUID,
    number: str,
    name: str,
    email: str,
    year_level: int = 3,
) -> User:
    user = User(
        email=email,
        password_hash="!faculty-roster-fixture",
        full_name=name,
        is_active=True,
    )
    db.add(user)
    await db.flush()
    db.add(
        StudentProfile(
            user_id=user.id,
            student_number=number,
            program_id=program_id,
            year_level=year_level,
            enrollment_status="enrolled",
        )
    )
    return user


def _term(*, current: bool, year: str) -> AcademicTerm:
    now = datetime.now(UTC)
    return AcademicTerm(
        academic_year=year,
        semester="FIRST",
        term_type="REGULAR",
        start_date=date(2026 if current else 2025, 8, 1),
        end_date=date(2026 if current else 2025, 12, 1),
        is_current=current,
        regular_enrollment_start=now - timedelta(days=30),
        regular_enrollment_end=now + timedelta(days=30),
        add_drop_deadline=now + timedelta(days=10),
        grade_submission_deadline=now + timedelta(days=90),
    )


@pytest.fixture
async def faculty_roster_setup(db: AsyncSession, grant):
    faculty = await _faculty(db, "2021-00901-MN-0", "faculty@pup.edu.ph")
    other_faculty = await _faculty(db, "2021-00902-MN-0", "other.fac@pup.edu.ph")
    await grant(faculty.id, "ReadCourseSection")

    program = Program(
        code="BSCS",
        name="Bachelor of Science in Computer Science",
        department="Computer Science",
        college="CCIS",
        total_units_required=Decimal("150.0"),
    )
    current_term = _term(current=True, year="2026-2027")
    prior_term = _term(current=False, year="2025-2026")
    prerequisite = Course(
        course_code="COMP 201",
        title="Data Structures",
        lecture_units=Decimal("3.0"),
        lab_units=Decimal("0.0"),
        is_nstp=False,
    )
    target = Course(
        course_code="COMP 301",
        title="Algorithms",
        lecture_units=Decimal("3.0"),
        lab_units=Decimal("0.0"),
        is_nstp=False,
    )
    db.add_all([program, current_term, prior_term, prerequisite, target])
    await db.flush()
    db.add(
        CoursePrerequisite(
            course_id=target.id,
            prerequisite_course_id=prerequisite.id,
            min_grade="3.00",
        )
    )

    own = CourseSection(
        academic_term_id=current_term.id,
        course_id=target.id,
        faculty_id=faculty.id,
        section_name="BSCS 3-1",
        capacity=40,
    )
    foreign = CourseSection(
        academic_term_id=current_term.id,
        course_id=target.id,
        faculty_id=other_faculty.id,
        section_name="BSCS 3-2",
        capacity=40,
    )
    empty = CourseSection(
        academic_term_id=current_term.id,
        course_id=target.id,
        faculty_id=faculty.id,
        section_name="BSCS 3-3",
        capacity=40,
    )
    prior_target = CourseSection(
        academic_term_id=prior_term.id,
        course_id=target.id,
        faculty_id=faculty.id,
        section_name="OLD-TARGET",
        capacity=40,
    )
    prior_prerequisite = CourseSection(
        academic_term_id=prior_term.id,
        course_id=prerequisite.id,
        faculty_id=faculty.id,
        section_name="OLD-PREREQ",
        capacity=40,
    )
    db.add_all([own, foreign, empty, prior_target, prior_prerequisite])
    await db.flush()

    alice = await _student(
        db,
        program_id=program.id,
        number="2024-00001-MN-0",
        name="Alice Abad",
        email="alice.abad@iskolarngbayan.pup.edu.ph",
    )
    bob = await _student(
        db,
        program_id=program.id,
        number="2024-00002-MN-0",
        name="Bob Bautista",
        email="bob.bautista@iskolarngbayan.pup.edu.ph",
    )
    cara = await _student(
        db,
        program_id=program.id,
        number="2024-00003-MN-0",
        name="Cara Cruz",
        email="cara.cruz@iskolarngbayan.pup.edu.ph",
    )
    evan = await _student(
        db,
        program_id=program.id,
        number="2024-00005-MN-0",
        name="Evan Evangelista",
        email="evan.evangelista@iskolarngbayan.pup.edu.ph",
    )
    outsider = await _student(
        db,
        program_id=program.id,
        number="2024-00004-MN-0",
        name="Dina Diaz",
        email="dina.diaz@iskolarngbayan.pup.edu.ph",
    )
    await db.flush()

    current_items = {}
    for student, status in (
        (alice, "enrolled"),
        (bob, "dropped"),
        (cara, "withdrawn"),
        (evan, "enrolled"),
    ):
        enrollment = TermEnrollment(
            student_id=student.id,
            academic_term_id=current_term.id,
            enrollment_status="enrolled",
        )
        db.add(enrollment)
        await db.flush()
        item = StudentCourseEnrollment(
            term_enrollment_id=enrollment.id,
            academic_term_id=current_term.id,
            section_id=own.id,
            status=status,
        )
        db.add(item)
        await db.flush()
        current_items[student.id] = item

    db.add(
        StudentGrade(
            enrollment_item_id=current_items[bob.id].id,
            raw_score=Decimal("86.00"),
            is_draft=True,
            is_locked=False,
        )
    )
    db.add(
        StudentGrade(
            enrollment_item_id=current_items[evan.id].id,
            final_grade="1.75",
            is_draft=False,
            is_locked=True,
        )
    )
    db.add(
        StudentGrade(
            enrollment_item_id=current_items[cara.id].id,
            final_grade="INC",
            inc_expires_at=datetime.now(UTC) + timedelta(days=365),
            is_draft=False,
            is_locked=True,
            submitted_at=datetime.now(UTC),
        )
    )

    for student, state, action in (
        (alice, "PENDING", "ADD"),
        (bob, "APPROVED", "DROP"),
    ):
        request = AddDropRequest(
            student_id=student.id,
            academic_term_id=current_term.id,
            state=state,
            status=state.lower(),
        )
        db.add(request)
        await db.flush()
        db.add(
            AddDropRequestItem(
                request_id=request.id,
                section_id=own.id,
                action=action,
            )
        )

    previous_enrollment = TermEnrollment(
        student_id=alice.id,
        academic_term_id=prior_term.id,
        enrollment_status="enrolled",
    )
    db.add(previous_enrollment)
    await db.flush()
    for section, mark in ((prior_prerequisite, "1.50"), (prior_target, "2.00")):
        item = StudentCourseEnrollment(
            term_enrollment_id=previous_enrollment.id,
            academic_term_id=prior_term.id,
            section_id=section.id,
            status="enrolled",
        )
        db.add(item)
        await db.flush()
        db.add(
            StudentGrade(
                enrollment_item_id=item.id,
                final_grade=mark,
                is_draft=False,
                is_locked=True,
                submitted_by=faculty.id,
                submitted_at=datetime.now(UTC),
            )
        )
    await db.commit()
    return {
        "faculty": faculty,
        "other_faculty": other_faculty,
        "own": own,
        "foreign": foreign,
        "empty": empty,
        "program": program,
        "current_term": current_term,
        "alice": alice,
        "bob": bob,
        "cara": cara,
        "evan": evan,
        "outsider": outsider,
        "target": target,
        "prerequisite": prerequisite,
    }


async def _roster_session(client: AsyncClient) -> dict[str, str]:
    return await _session(client, "2021-00901-MN-0")


def _without_request_id(body: dict) -> dict:
    return {key: value for key, value in body.items() if key != "request_id"}


async def test_ac_fac_004_1_roster_returns_required_fields(
    client: AsyncClient, faculty_roster_setup
) -> None:
    headers = await _roster_session(client)
    section = faculty_roster_setup["own"]
    response = await client.get(
        f"/api/v1/faculty/sections/{section.id}/roster", headers=headers
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 4
    assert set(body["roster"][0]) == {
        "user_id",
        "studentNumber",
        "name",
        "email",
        "enrollmentStatus",
        "gradeStatus",
        "addDropStatus",
    }


async def test_ac_fac_004_2_filters_and_clear_returns_all(
    client: AsyncClient, faculty_roster_setup
) -> None:
    headers = await _roster_session(client)
    path = f"/api/v1/faculty/sections/{faculty_roster_setup['own'].id}/roster"
    enrolled = await client.get(
        path, headers=headers, params={"enrollmentStatus": "ENROLLED"}
    )
    assert enrolled.status_code == 200
    assert [row["name"] for row in enrolled.json()["roster"]] == [
        "Alice Abad",
        "Evan Evangelista",
    ]
    draft = await client.get(path, headers=headers, params={"gradeStatus": "DRAFT"})
    assert draft.status_code == 200
    assert [row["name"] for row in draft.json()["roster"]] == ["Bob Bautista"]
    submitted = await client.get(
        path, headers=headers, params={"gradeStatus": "SUBMITTED"}
    )
    assert submitted.status_code == 200
    assert [row["name"] for row in submitted.json()["roster"]] == ["Evan Evangelista"]
    assert (await client.get(path, headers=headers)).json()["total"] == 4


async def test_ac_fac_004_3_pages_and_rejects_end_offset(
    client: AsyncClient, faculty_roster_setup
) -> None:
    headers = await _roster_session(client)
    path = f"/api/v1/faculty/sections/{faculty_roster_setup['own'].id}/roster"
    page = await client.get(path, headers=headers, params={"limit": 1, "offset": 1})
    assert page.status_code == 200
    assert page.json()["total"] == 4
    assert len(page.json()["roster"]) == 1
    past_end = await client.get(path, headers=headers, params={"offset": 4})
    assert past_end.status_code == 400
    assert past_end.json()["code"] == "INVALID_INPUT"


async def test_ac_fac_004_4_add_drop_status_is_text(
    client: AsyncClient, faculty_roster_setup
) -> None:
    headers = await _roster_session(client)
    path = f"/api/v1/faculty/sections/{faculty_roster_setup['own'].id}/roster"
    response = await client.get(path, headers=headers)
    rows = {row["name"]: row for row in response.json()["roster"]}
    assert rows["Alice Abad"]["addDropStatus"] == "PENDING_ADD"
    assert rows["Bob Bautista"]["addDropStatus"] == "APPROVED_DROP"
    assert rows["Cara Cruz"]["addDropStatus"] == "NONE"


async def test_ac_fac_004_5_csv_matches_filtered_roster(
    client: AsyncClient, faculty_roster_setup
) -> None:
    headers = await _roster_session(client)
    section_id = faculty_roster_setup["own"].id
    params = {"format": "csv", "gradeStatus": "INC"}
    response = await client.get(
        f"/api/v1/faculty/sections/{section_id}/roster/export",
        headers=headers,
        params=params,
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    roster = await client.get(
        f"/api/v1/faculty/sections/{section_id}/roster",
        headers=headers,
        params={"gradeStatus": "INC"},
    )
    expected = [
        {
            key: row[key]
            for key in (
                "studentNumber",
                "name",
                "email",
                "enrollmentStatus",
                "gradeStatus",
                "addDropStatus",
            )
        }
        for row in roster.json()["roster"]
    ]
    assert list(csv.DictReader(io.StringIO(response.text))) == expected


async def test_ac_fac_004_6_student_detail_has_only_safe_fields(
    client: AsyncClient, faculty_roster_setup
) -> None:
    headers = await _roster_session(client)
    setup = faculty_roster_setup
    response = await client.get(
        f"/api/v1/faculty/sections/{setup['own'].id}/roster/{setup['alice'].id}",
        headers=headers,
    )
    assert response.status_code == 200, response.text
    assert response.json() == {
        "name": "Alice Abad",
        "studentNumber": "2024-00001-MN-0",
        "email": "alice.abad@iskolarngbayan.pup.edu.ph",
        "program": "Bachelor of Science in Computer Science",
        "yearLevel": 3,
    }


async def test_ac_fac_004_7_foreign_and_missing_sections_are_identical(
    client: AsyncClient, faculty_roster_setup
) -> None:
    headers = await _roster_session(client)
    setup = faculty_roster_setup
    missing_id = uuid.uuid4()
    suffixes = (
        "/roster",
        "/roster/export?format=csv",
        f"/roster/{setup['alice'].id}",
        f"/roster/{setup['alice'].id}/record",
    )
    for suffix in suffixes:
        foreign = await client.get(
            f"/api/v1/faculty/sections/{setup['foreign'].id}{suffix}",
            headers=headers,
        )
        missing = await client.get(
            f"/api/v1/faculty/sections/{missing_id}{suffix}", headers=headers
        )
        assert foreign.status_code == missing.status_code == 404
        assert _without_request_id(foreign.json()) == _without_request_id(
            missing.json()
        )
        assert foreign.json()["code"] == "RESOURCE_NOT_FOUND"


async def test_ac_fac_004_8_record_requires_membership_and_audits_disclosure(
    client: AsyncClient, db: AsyncSession, faculty_roster_setup
) -> None:
    headers = await _roster_session(client)
    setup = faculty_roster_setup
    base = f"/api/v1/faculty/sections/{setup['own'].id}/roster"
    missing = await client.get(f"{base}/{setup['outsider'].id}/record", headers=headers)
    assert missing.status_code == 404
    response = await client.get(f"{base}/{setup['alice'].id}/record", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["courseId"] == str(setup["target"].id)
    assert body["courseAttempts"][0]["finalGrade"] == "2.00"
    assert body["prerequisites"][0]["courseId"] == str(setup["prerequisite"].id)
    assert body["prerequisites"][0]["result"] == "PASSED"
    audits = list(
        await db.scalars(
            select(AuditEntry).where(AuditEntry.action == ACADEMIC_RECORD_DISCLOSED)
        )
    )
    assert len(audits) == 1
    assert audits[0].actor_id == setup["faculty"].id
    assert audits[0].target_id == str(setup["alice"].id)
    assert audits[0].payload["sectionId"] == str(setup["own"].id)


async def test_ac_fac_004_9_empty_section_is_not_an_error(
    client: AsyncClient, faculty_roster_setup
) -> None:
    headers = await _roster_session(client)
    section = faculty_roster_setup["empty"]
    response = await client.get(
        f"/api/v1/faculty/sections/{section.id}/roster",
        headers=headers,
        params={"offset": 100},
    )
    assert response.status_code == 200
    assert response.json()["roster"] == []
    assert response.json()["total"] == 0


async def test_faculty_roster_60_students_meets_parent_speed_target(
    client: AsyncClient, db: AsyncSession, faculty_roster_setup
) -> None:
    setup = faculty_roster_setup
    for index in range(56):
        student = await _student(
            db,
            program_id=setup["program"].id,
            number=f"2024-{index + 100:05d}-MN-0",
            name=f"Load Student {index:02d}",
            email=f"load.student.{index:02d}@iskolarngbayan.pup.edu.ph",
        )
        enrollment = TermEnrollment(
            student_id=student.id,
            academic_term_id=setup["current_term"].id,
            enrollment_status="enrolled",
        )
        db.add(enrollment)
        await db.flush()
        db.add(
            StudentCourseEnrollment(
                term_enrollment_id=enrollment.id,
                academic_term_id=setup["current_term"].id,
                section_id=setup["own"].id,
                status="enrolled",
            )
        )
    await db.commit()

    headers = await _roster_session(client)
    path = f"/api/v1/faculty/sections/{setup['own'].id}/roster"
    warmup = await client.get(path, headers=headers, params={"limit": 200})
    assert warmup.status_code == 200
    assert warmup.json()["total"] == 60

    durations = []
    for _ in range(10):
        started = time.perf_counter()
        response = await client.get(path, headers=headers, params={"limit": 200})
        durations.append(time.perf_counter() - started)
        assert response.status_code == 200
    p90 = sorted(durations)[8]
    assert p90 <= 0.5, f"60-student roster p90 was {p90:.3f}s"


@pytest.mark.parametrize(
    ("role", "mfa", "status", "code"),
    (
        ("student", True, 403, "AUTH_INSUFFICIENT_ROLE"),
        ("faculty", False, 401, "AUTH_MFA_REQUIRED"),
    ),
)
async def test_faculty_roster_uses_canonical_role_and_mfa_errors(
    client: AsyncClient,
    faculty_roster_setup,
    role: str,
    mfa: bool,
    status: int,
    code: str,
) -> None:
    principal = CurrentUser(
        id=uuid.uuid4(),
        email="wrong-role@example.com",
        role=role,
        claims={"mfa": mfa},
    )
    app.dependency_overrides[get_current_user] = lambda: principal
    try:
        section_id = faculty_roster_setup["own"].id
        response = await client.get(f"/api/v1/faculty/sections/{section_id}/roster")
    finally:
        app.dependency_overrides.pop(get_current_user, None)
    assert response.status_code == status
    assert response.json()["code"] == code
