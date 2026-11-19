"""Synthetic persisted records; only the root fixture's database is substituted."""

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from cbpupsis_database.models.academics import AcademicTerm
from cbpupsis_database.models.curriculum import Course
from cbpupsis_database.models.enrollment import (
    StudentCourseEnrollment,
    TermEnrollment,
)
from cbpupsis_database.models.grading import StudentGrade
from cbpupsis_database.models.sections import CourseSection
from cbpupsis_database.models.users import (
    FacultyProfile,
    StudentProfile,
    User,
    UserProfile,
)
from cbpupsis_shared.domains.auth.security import hash_password, totp_at

PASSWORD = "Synthetic-Grades-Password-123!"
BIRTHDATE = date(2003, 5, 14)
POSTED_AT = datetime(2024, 2, 29, 8, 15, tzinfo=UTC)


@pytest.fixture
async def student_account(db, active_program):
    user = User(
        email="grades-student@example.com",
        password_hash=hash_password(PASSWORD),
        full_name="Synthetic Grades Student",
    )
    db.add(user)
    await db.flush()
    db.add_all(
        [
            UserProfile(
                user_id=user.id,
                first_name="Synthetic",
                last_name="Student",
                birthdate=BIRTHDATE,
            ),
            StudentProfile(
                user_id=user.id,
                student_number="2021-00146-MN-0",
                program_id=active_program.id,
                year_level=1,
                enrollment_status="enrolled",
            ),
        ]
    )
    await db.commit()
    return user


@pytest.fixture
async def student_headers(client, student_account, grant):
    for action in ("ReadGrade", "ReadTranscript"):
        await grant(student_account.id, action)
    response = await client.post(
        "/api/v1/auth/student/login",
        json={
            "student_number": "2021-00146-MN-0",
            "birthdate": BIRTHDATE.isoformat(),
            "password": PASSWORD,
        },
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


@pytest.fixture
async def faculty_account(db):
    user = User(
        email="grades-faculty@example.com",
        password_hash=hash_password(PASSWORD),
        email_verified_at=datetime.now(UTC),
    )
    db.add(user)
    await db.flush()
    db.add(
        FacultyProfile(user_id=user.id, employee_id="2021-00147-MN-0", department="CS")
    )
    await db.commit()
    return user


@pytest.fixture
async def faculty_headers(client, faculty_account, grant):
    # Even a faculty principal holding these permissions must fail the role gate.
    for action in ("ReadGrade", "ReadTranscript"):
        await grant(faculty_account.id, action)
    return await faculty_login(client)


@pytest.fixture
async def producer_headers(client, faculty_account, grant):
    for action in (
        "ReadCourseSection",
        "UpdateGrade",
        "SubmitGrade",
        "CreateGradeCorrection",
    ):
        await grant(faculty_account.id, action)
    return await faculty_login(client)


async def faculty_login(client):
    response = await client.post(
        "/api/v1/auth/faculty/login",
        json={"employee_id": "2021-00147-MN-0", "password": PASSWORD},
    )
    assert response.status_code == 200, response.text
    challenge = response.json()["challenge_token"]
    response = await client.post(
        "/api/v1/auth/faculty/mfa/totp/enroll", json={"challenge_token": challenge}
    )
    assert response.status_code == 200, response.text
    code = totp_at(response.json()["secret"], int(datetime.now(UTC).timestamp()) // 30)
    response = await client.post(
        "/api/v1/auth/faculty/mfa/totp/confirm",
        json={"challenge_token": challenge, "code": code},
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


@pytest.fixture
def make_term(db):
    async def create(year=2024, *, end_date=None):
        start = datetime(year, 1, 1, tzinfo=UTC)
        term = AcademicTerm(
            academic_year=f"{year}-{year + 1}",
            semester="first",
            start_date=start.date(),
            end_date=end_date or date(year, 5, 31),
            regular_enrollment_start=start - timedelta(days=14),
            regular_enrollment_end=start,
            add_drop_deadline=start + timedelta(days=14),
            grade_submission_deadline=start + timedelta(days=160),
        )
        db.add(term)
        await db.commit()
        return term

    return create


@pytest.fixture
def post_mark(db, student_account):
    async def create(
        term,
        mark="1.50",
        *,
        code=None,
        units="3.0",
        course=None,
        student_id=None,
        is_draft=False,
        is_locked=True,
        submitted_at=POSTED_AT,
        inc_expires_at=None,
    ):
        owner = student_id or student_account.id
        # Reuse an existing term enrollment so every fixture is relationally valid.
        from sqlalchemy import select

        enrollment = await db.scalar(
            select(TermEnrollment).where(
                TermEnrollment.student_id == owner,
                TermEnrollment.academic_term_id == term.id,
            )
        )
        if enrollment is None:
            enrollment = TermEnrollment(student_id=owner, academic_term_id=term.id)
            db.add(enrollment)
            await db.flush()
        if course is None:
            course = Course(
                course_code=code or f"COMP{uuid.uuid4().hex[:8]}",
                title="Synthetic Course",
                lecture_units=Decimal(units),
                lab_units=Decimal("0.0"),
            )
            db.add(course)
            await db.flush()
        section = CourseSection(
            academic_term_id=term.id,
            course_id=course.id,
            section_name=f"S{uuid.uuid4().hex[:8]}",
            capacity=40,
        )
        db.add(section)
        await db.flush()
        attempt = StudentCourseEnrollment(
            term_enrollment_id=enrollment.id,
            academic_term_id=term.id,
            section_id=section.id,
            status="withdrawn" if mark == "W" else "enrolled",
        )
        db.add(attempt)
        await db.flush()
        grade = StudentGrade(
            enrollment_item_id=attempt.id,
            final_grade=mark,
            is_draft=is_draft,
            is_locked=is_locked,
            submitted_at=submitted_at,
            inc_expires_at=inc_expires_at,
        )
        db.add(grade)
        await db.commit()
        return grade, course

    return create
