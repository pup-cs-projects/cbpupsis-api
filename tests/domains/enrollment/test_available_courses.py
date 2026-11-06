from __future__ import annotations

import json
import uuid
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_database.models.academics import AcademicTerm
from cbpupsis_database.models.curriculum import (
    Course,
    Curriculum,
    CurriculumCourse,
    Program,
)
from cbpupsis_database.models.enrollment import (
    StudentCourseEnrollment,
    TermEnrollment,
)
from cbpupsis_database.models.sections import CourseSection, SectionSchedule
from cbpupsis_database.models.users import StudentProfile, User, UserProfile
from cbpupsis_shared.domains.auth.security import hash_password
from cbpupsis_shared.domains.enrollment.constants import READ_ENROLLMENT
from cbpupsis_shared.domains.enrollment.exceptions import ScheduleConflictError
from cbpupsis_shared.domains.enrollment.schemas import (
    ConflictMeeting,
    ConflictSection,
    ConflictStudent,
    ConflictTerm,
)
from cbpupsis_shared.domains.enrollment.service import check_schedule_conflict

URL = "/api/v1/student/enrollment/available-courses"
LOGIN_URL = "/api/v1/auth/student/login"
PASSWORD = "StudentPassword123!"


async def _curriculum(
    db: AsyncSession,
    *,
    code: str,
    course_codes: list[str],
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
        program_id=program.id,
        curriculum_year="2026",
        is_active=True,
    )
    db.add(curriculum)
    await db.flush()
    courses = []
    for index, course_code in enumerate(course_codes):
        course = Course(
            course_code=course_code,
            title=f"Course {index + 1}",
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
                semester="First Semester",
            )
        )
        courses.append(course)
    return program, curriculum, courses


async def _term(
    db: AsyncSession,
    *,
    academic_year: str = "2026-2027",
    semester: str = "First Semester",
) -> AcademicTerm:
    now = datetime.now(UTC)
    term = AcademicTerm(
        academic_year=academic_year,
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
) -> User:
    user = User(
        email=f"{number.replace('-', '_')}@example.com",
        password_hash=hash_password(PASSWORD),
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
    start: time,
    end: time,
    capacity: int = 2,
    faculty_id: uuid.UUID | None = None,
) -> CourseSection:
    section = CourseSection(
        academic_term_id=term.id,
        course_id=course.id,
        faculty_id=faculty_id,
        section_name=name,
        capacity=capacity,
        status="open",
    )
    db.add(section)
    await db.flush()
    db.add(
        SectionSchedule(
            section_id=section.id,
            day_of_week=0,
            start_time=start,
            end_time=end,
            room="R101",
            schedule_type="lecture",
        )
    )
    await db.flush()
    return section


async def _enroll(
    db: AsyncSession,
    *,
    student: User,
    term: AcademicTerm,
    section: CourseSection,
) -> None:
    enrollment = TermEnrollment(
        student_id=student.id,
        academic_term_id=term.id,
        enrollment_status="enrolled",
    )
    db.add(enrollment)
    await db.flush()
    db.add(
        StudentCourseEnrollment(
            term_enrollment_id=enrollment.id,
            academic_term_id=term.id,
            section_id=section.id,
            status="enrolled",
        )
    )
    await db.flush()


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


def _conflict_section(
    *,
    term_id: uuid.UUID,
    course_code: str,
    start: time,
    end: time,
) -> ConflictSection:
    return ConflictSection(
        section_id=uuid.uuid4(),
        course_code=course_code,
        academic_term_id=term_id,
        schedule=[ConflictMeeting(day=0, start=start, end=end)],
    )


def test_ac_enrl_001_9_overlapping_sections_conflict() -> None:
    student = ConflictStudent(student_id=uuid.uuid4())
    term = ConflictTerm(academic_term_id=uuid.uuid4())
    first = _conflict_section(
        term_id=term.academic_term_id,
        course_code="COMP 101",
        start=time(9),
        end=time(12),
    )
    second = _conflict_section(
        term_id=term.academic_term_id,
        course_code="COMP 102",
        start=time(10),
        end=time(13),
    )

    with pytest.raises(ScheduleConflictError) as caught:
        check_schedule_conflict(student, term, [first, second])

    assert caught.value.code == "ENROLL_SCHEDULE_CONFLICT"
    assert caught.value.response_fields["details"]["courseCodes"] == [
        "COMP 101",
        "COMP 102",
    ]
    assert caught.value.response_fields["details"]["sectionIds"] == [
        str(first.section_id),
        str(second.section_id),
    ]
    assert caught.value.response_fields["details"]["overlap"] == {
        "day": 0,
        "start": "10:00:00",
        "end": "12:00:00",
    }
    json.dumps(caught.value.response_fields)


def test_ac_enrl_001_9_adjacent_sections_do_not_conflict() -> None:
    student = ConflictStudent(student_id=uuid.uuid4())
    term = ConflictTerm(academic_term_id=uuid.uuid4())
    first = _conflict_section(
        term_id=term.academic_term_id,
        course_code="COMP 101",
        start=time(9),
        end=time(12),
    )
    second = _conflict_section(
        term_id=term.academic_term_id,
        course_code="COMP 102",
        start=time(12),
        end=time(15),
    )

    result = check_schedule_conflict(student, term, [first, second])

    assert result.conflict_free is True


def test_ac_enrl_001_9_available_slots_is_documented_as_advisory() -> None:
    from main import app

    schema = app.openapi()["components"]["schemas"]["AvailableCourseRead"]
    description = schema["properties"]["availableSlots"]["description"].lower()
    assert "advisory" in description
    assert "stale" in description


async def test_ac_enrl_001_9_endpoint_returns_every_section_field(
    client: AsyncClient, db: AsyncSession, grant
) -> None:
    program, curriculum, courses = await _curriculum(
        db, code="ENRL168-A", course_codes=["ENR168A"]
    )
    term = await _term(db)
    number = "2026-00168-MN-0"
    student = await _student(db, program=program, curriculum=curriculum, number=number)
    other = await _student(
        db, program=program, curriculum=curriculum, number="2026-00169-MN-0"
    )
    faculty = User(
        email="faculty-168@example.com",
        password_hash=hash_password(PASSWORD),
        full_name="Ada Faculty",
        is_active=True,
    )
    db.add(faculty)
    await db.flush()
    section = await _section(
        db,
        term=term,
        course=courses[0],
        name="BSCS 3-1",
        start=time(9),
        end=time(12),
        faculty_id=faculty.id,
    )
    await _enroll(db, student=other, term=term, section=section)
    await db.commit()
    await grant(student.id, READ_ENROLLMENT)

    response = await client.get(
        URL,
        params={"term": "2026-2027-First Semester"},
        headers=await _headers(client, number),
    )

    assert response.status_code == 200, response.text
    assert len(response.json()) == 1
    item = response.json()[0]
    assert set(item) == {
        "sectionId",
        "courseCode",
        "title",
        "units",
        "schedule",
        "faculty",
        "capacity",
        "availableSlots",
        "prerequisitesSatisfied",
        "blockReason",
        "blocks",
        "waiverAvailable",
        "conflictsWith",
    }
    assert item["sectionId"] == str(section.id)
    assert item["courseCode"] == "ENR168A"
    assert Decimal(item["units"]) == Decimal("3")
    assert item["schedule"] == [
        {"day": 0, "start": "09:00:00", "end": "12:00:00", "room": "R101"}
    ]
    assert item["faculty"] == "Ada Faculty"
    assert item["capacity"] == 2
    assert item["availableSlots"] == 1
    assert item["prerequisitesSatisfied"] is True
    assert item["blockReason"] is None
    assert item["blocks"] == []
    assert item["waiverAvailable"] is False
    assert item["conflictsWith"] == []


async def test_academic_preview_batches_independent_pe_and_omits_off_curriculum(
    client, db, grant, monkeypatch
):
    from unittest.mock import AsyncMock

    from cbpupsis_shared.domains.enrollment import academic
    from cbpupsis_shared.domains.sections import client as sections

    program, curriculum, courses = await _curriculum(
        db, code="PR213-PE", course_codes=["DEMO-PE-1", "DEMO-PE-2"]
    )
    courses[0].pe_sequence, courses[1].pe_sequence = 1, 2
    term = await _term(db)
    number = "2026-00213-MN-0"
    student = await _student(db, program=program, curriculum=curriculum, number=number)
    off_course = Course(course_code="DEMO-OUTSIDE", title="Outside curriculum")
    db.add(off_course)
    await db.flush()
    for course in [*courses, off_course]:
        await _section(
            db, term=term, course=course, name="A", start=time(9), end=time(10)
        )
    await db.commit()
    await grant(student.id, READ_ENROLLMENT)
    headers = await _headers(client, number)
    original = sections.available_sections

    async def include_drift(db, *, academic_term_id, course_ids):
        return await original(
            db,
            academic_term_id=academic_term_id,
            course_ids={*course_ids, off_course.id},
        )

    monkeypatch.setattr(sections, "available_sections", include_drift)
    load = AsyncMock(wraps=academic.load_facts)
    monkeypatch.setattr(academic, "load_facts", load)
    params = {"term": "2026-2027-First Semester"}
    response = await client.get(URL, params=params, headers=headers)
    assert response.status_code == 200, response.text
    rows = response.json()
    assert {row["courseCode"] for row in rows} == {"DEMO-PE-1", "DEMO-PE-2"}
    assert all(row["blocks"] == [] for row in rows)
    load.assert_awaited_once()

    courses[0].pe_sequence = None
    courses[0].is_nstp = None
    await db.commit()
    response = await client.get(URL, params=params, headers=headers)
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "ENROLL_ACADEMIC_DATA_UNAVAILABLE"


async def test_ac_enrl_001_9_scopes_program_term_and_student_conflicts(
    client: AsyncClient, db: AsyncSession, grant
) -> None:
    program, curriculum, courses = await _curriculum(
        db, code="ENRL168-B", course_codes=["ENR168B", "ENR168C"]
    )
    foreign_program, foreign_curriculum, foreign_courses = await _curriculum(
        db, code="ENRL168-C", course_codes=["ENR168D"]
    )
    term = await _term(db)
    other_term = await _term(db, academic_year="2027-2028", semester="First Semester")
    caller_number = "2026-00170-MN-0"
    caller = await _student(
        db, program=program, curriculum=curriculum, number=caller_number
    )
    other = await _student(
        db, program=program, curriculum=curriculum, number="2026-00171-MN-0"
    )
    candidate = await _section(
        db,
        term=term,
        course=courses[0],
        name="Candidate",
        start=time(9),
        end=time(12),
    )
    own_enrolled = await _section(
        db,
        term=term,
        course=courses[1],
        name="Own Enrolled",
        start=time(10),
        end=time(11),
    )
    other_enrolled = await _section(
        db,
        term=term,
        course=courses[1],
        name="Other Student Enrolled",
        start=time(9, 30),
        end=time(10, 30),
    )
    foreign_section = await _section(
        db,
        term=term,
        course=foreign_courses[0],
        name="Foreign Program",
        start=time(9),
        end=time(12),
    )
    wrong_term_section = await _section(
        db,
        term=other_term,
        course=courses[0],
        name="Wrong Term",
        start=time(9),
        end=time(12),
    )
    await _enroll(db, student=caller, term=term, section=own_enrolled)
    await _enroll(db, student=other, term=term, section=other_enrolled)
    await db.commit()
    await grant(caller.id, READ_ENROLLMENT)

    response = await client.get(
        URL,
        params={"term": "2026-2027-First Semester"},
        headers=await _headers(client, caller_number),
    )

    assert response.status_code == 200, response.text
    by_id = {item["sectionId"]: item for item in response.json()}
    assert str(candidate.id) in by_id
    assert str(own_enrolled.id) in by_id
    assert str(other_enrolled.id) in by_id
    assert str(foreign_section.id) not in by_id
    assert str(wrong_term_section.id) not in by_id
    assert by_id[str(candidate.id)]["conflictsWith"] == [str(own_enrolled.id)]
    assert str(other_enrolled.id) not in by_id[str(candidate.id)]["conflictsWith"]
    assert foreign_curriculum.program_id == foreign_program.id


async def test_ac_enrl_001_9_term_without_sections_returns_empty_list(
    client: AsyncClient, db: AsyncSession, grant
) -> None:
    program, curriculum, _courses = await _curriculum(
        db, code="ENRL168-D", course_codes=["ENR168E"]
    )
    await _term(db)
    number = "2026-00172-MN-0"
    student = await _student(db, program=program, curriculum=curriculum, number=number)
    await db.commit()
    await grant(student.id, READ_ENROLLMENT)

    response = await client.get(
        URL,
        params={"term": "2026-2027-First Semester"},
        headers=await _headers(client, number),
    )

    assert response.status_code == 200, response.text
    assert response.json() == []


async def test_ac_enrl_001_9_requires_read_enrollment(
    client: AsyncClient, db: AsyncSession
) -> None:
    program, curriculum, _courses = await _curriculum(
        db, code="ENRL168-E", course_codes=["ENR168F"]
    )
    await _term(db)
    number = "2026-00173-MN-0"
    await _student(db, program=program, curriculum=curriculum, number=number)
    await db.commit()

    response = await client.get(
        URL,
        params={"term": "2026-2027-First Semester"},
        headers=await _headers(client, number),
    )

    assert response.status_code == 403
