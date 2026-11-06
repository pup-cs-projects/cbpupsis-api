from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

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
from cbpupsis_database.models.grading import StudentTermGPA
from cbpupsis_database.models.users import StudentProfile, User, UserProfile
from cbpupsis_shared.domains.auth.security import hash_password
from cbpupsis_shared.domains.enrollment.constants import READ_ENROLLMENT
from cbpupsis_shared.domains.enrollment.exceptions import (
    OverloadCapExceededError,
    SummerSubjectLimitError,
    UnitCeilingExceededError,
)
from cbpupsis_shared.domains.enrollment.schemas import (
    CeilingSection,
    CeilingStudent,
    CeilingTerm,
)
from cbpupsis_shared.domains.enrollment.service import (
    check_unit_ceiling,
    compute_effective_ceiling,
)

URL = "/api/v1/student/enrollment/eligibility"
LOGIN_URL = "/api/v1/auth/student/login"
PASSWORD = "StudentPassword123!"


async def _curriculum(
    db: AsyncSession, *, prescribed_load: Decimal = Decimal("26")
) -> tuple[Program, Curriculum, Course]:
    program = Program(
        code=f"ENRL-{uuid.uuid4().hex[:8]}",
        name="Enrollment Test Program",
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
    course = Course(
        course_code=f"ENRL{uuid.uuid4().hex[:6]}",
        title="Curriculum Load Template",
        lecture_units=prescribed_load,
        lab_units=Decimal("0"),
        is_active=True,
    )
    db.add(course)
    await db.flush()
    db.add(
        CurriculumCourse(
            curriculum_id=curriculum.id,
            course_id=course.id,
            year_level=3,
            semester="First Semester",
        )
    )
    return program, curriculum, course


async def _term(
    db: AsyncSession,
    *,
    semester: str = "First Semester",
    is_open: bool = True,
) -> AcademicTerm:
    now = datetime.now(UTC)
    term = AcademicTerm(
        academic_year="2026-2027",
        semester=semester,
        start_date=date(2026, 8, 1),
        end_date=date(2027, 1, 1),
        is_current=True,
        regular_enrollment_start=now - timedelta(days=1),
        regular_enrollment_end=(
            now + timedelta(days=1) if is_open else now - timedelta(hours=1)
        ),
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
    admission_status: str = "REGULAR",
    financial_hold: bool = False,
) -> User:
    user = User(
        email=f"{number.replace('-', '_')}@example.com",
        password_hash=hash_password(PASSWORD),
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
            year_level=3,
            enrollment_status="PENDING",
            admission_status=admission_status,
            has_financial_hold=financial_hold,
        )
    )
    await db.commit()
    return user


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


def _sections(*units: str, back_subject: bool = False) -> list[CeilingSection]:
    return [
        CeilingSection(units=Decimal(unit), is_back_subject=back_subject)
        for unit in units
    ]


def test_ac_enrl_001_1_curriculum_prescribed_ceiling() -> None:
    student = CeilingStudent(prescribed_load=Decimal("26"))
    result = check_unit_ceiling(
        student, CeilingTerm(term_type="REGULAR"), _sections("26")
    )
    assert result.effective_ceiling == Decimal("26")


def test_ac_enrl_001_2_unit_ceiling_refusal_details() -> None:
    with pytest.raises(UnitCeilingExceededError) as caught:
        check_unit_ceiling(
            CeilingStudent(prescribed_load=Decimal("26")),
            CeilingTerm(term_type="REGULAR"),
            _sections("29"),
        )
    error = caught.value
    assert error.code == "ENROLL_UNIT_CEILING_EXCEEDED"
    assert error.response_fields["details"]["effectiveCeiling"] == Decimal("26")
    assert error.response_fields["details"]["attemptedUnits"] == Decimal("29")


def test_ac_enrl_001_3_approved_overload_example_ceiling() -> None:
    student = CeilingStudent(
        prescribed_load=Decimal("26"), approved_overload_units=Decimal("9")
    )
    assert compute_effective_ceiling(student) == Decimal("35")
    assert check_unit_ceiling(
        student, CeilingTerm(term_type="REGULAR"), _sections("35")
    )


def test_ac_enrl_001_4_overload_cap_refusal() -> None:
    with pytest.raises(OverloadCapExceededError) as caught:
        check_unit_ceiling(
            CeilingStudent(
                prescribed_load=Decimal("26"),
                approved_overload_units=Decimal("9"),
            ),
            CeilingTerm(term_type="REGULAR"),
            _sections("36"),
        )
    assert caught.value.code == "ENROLL_OVERLOAD_CAP_EXCEEDED"


def test_ac_enrl_001_6_warning_level_2_reduces_ceiling() -> None:
    student = CeilingStudent(
        prescribed_load=Decimal("26"), standing_reduction=Decimal("3")
    )
    assert compute_effective_ceiling(student) == Decimal("23")
    with pytest.raises(UnitCeilingExceededError):
        check_unit_ceiling(student, CeilingTerm(term_type="REGULAR"), _sections("24"))


def test_ac_enrl_001_7_probation_reduces_ceiling() -> None:
    student = CeilingStudent(
        prescribed_load=Decimal("26"), standing_reduction=Decimal("6")
    )
    assert compute_effective_ceiling(student) == Decimal("20")
    with pytest.raises(UnitCeilingExceededError):
        check_unit_ceiling(student, CeilingTerm(term_type="REGULAR"), _sections("21"))


@pytest.mark.parametrize("admission_status", ["TRANSFEREE", "SHIFTEE", "RETURNEE"])
def test_ac_enrl_001_8_restricted_summer_subject_limit(
    admission_status: str,
) -> None:
    with pytest.raises(SummerSubjectLimitError) as caught:
        check_unit_ceiling(
            CeilingStudent(
                prescribed_load=Decimal("12"), admission_status=admission_status
            ),
            CeilingTerm(term_type="SUMMER"),
            _sections("3", "3", "3"),
        )
    assert caught.value.code == "ENROLL_SUMMER_SUBJECT_LIMIT"


def test_ac_enrl_001_8_back_subject_uses_summer_limit() -> None:
    with pytest.raises(SummerSubjectLimitError):
        check_unit_ceiling(
            CeilingStudent(prescribed_load=Decimal("12")),
            CeilingTerm(term_type="SUMMER"),
            _sections("3", "3", "3", back_subject=True),
        )


def test_ac_enrl_001_15_regular_student_has_no_summer_subject_cap() -> None:
    result = check_unit_ceiling(
        CeilingStudent(prescribed_load=Decimal("12")),
        CeilingTerm(term_type="SUMMER"),
        _sections("3", "3", "3"),
    )
    assert result.attempted_units == Decimal("9")


@pytest.mark.asyncio
async def test_ac_enrl_001_1_eligibility_returns_every_field(
    client: AsyncClient, db: AsyncSession, grant
) -> None:
    program, curriculum, _course = await _curriculum(db)
    await _term(db)
    number = "2026-00167-MN-0"
    user = await _student(db, program=program, curriculum=curriculum, number=number)
    await grant(user.id, READ_ENROLLMENT)
    response = await client.get(URL, headers=await _headers(client, number))
    assert response.status_code == 200, response.text
    data = response.json()
    expected = {
        "isEligible",
        "termType",
        "curriculumId",
        "curriculumVersion",
        "prescribedLoad",
        "approvedOverloadUnits",
        "standingReduction",
        "effectiveCeiling",
        "academicStanding",
        "overloadEligible",
        "summerSubjectCap",
        "holds",
        "academicPeriod",
        "reasons",
    }
    assert set(data) == expected
    assert data["isEligible"] is True
    assert Decimal(data["effectiveCeiling"]) == Decimal("26")


@pytest.mark.asyncio
async def test_ac_enrl_001_1_curriculum_database_change_changes_ceiling(
    client: AsyncClient, db: AsyncSession, grant
) -> None:
    program, curriculum, course = await _curriculum(db)
    await _term(db)
    number = "2026-00168-MN-0"
    user = await _student(db, program=program, curriculum=curriculum, number=number)
    await grant(user.id, READ_ENROLLMENT)
    headers = await _headers(client, number)
    first = await client.get(URL, headers=headers)
    assert Decimal(first.json()["effectiveCeiling"]) == Decimal("26")

    course.lecture_units = Decimal("24")
    await db.commit()
    second = await client.get(URL, headers=headers)
    assert second.status_code == 200, second.text
    assert Decimal(second.json()["effectiveCeiling"]) == Decimal("24")


@pytest.mark.asyncio
async def test_ac_enrl_001_6_endpoint_uses_stored_standing(
    client: AsyncClient, db: AsyncSession, grant
) -> None:
    program, curriculum, _course = await _curriculum(db)
    term = await _term(db)
    number = "2026-00169-MN-0"
    user = await _student(db, program=program, curriculum=curriculum, number=number)
    db.add(
        StudentTermGPA(
            student_id=user.id,
            academic_term_id=term.id,
            total_gpa_units=Decimal("18"),
            term_gpa=Decimal("2.25"),
            cumulative_gpa=Decimal("2.25"),
            academic_standing="Warning Level 2",
        )
    )
    await db.commit()
    await grant(user.id, READ_ENROLLMENT)
    response = await client.get(URL, headers=await _headers(client, number))
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["academicStanding"] == "Warning Level 2"
    assert Decimal(data["standingReduction"]) == Decimal("3")
    assert Decimal(data["effectiveCeiling"]) == Decimal("23")


@pytest.mark.asyncio
async def test_ineligible_state_is_200_with_reasons(
    client: AsyncClient, db: AsyncSession, grant
) -> None:
    program, curriculum, _course = await _curriculum(db)
    await _term(db, is_open=False)
    number = "2026-00170-MN-0"
    user = await _student(
        db,
        program=program,
        curriculum=curriculum,
        number=number,
        financial_hold=True,
    )
    await grant(user.id, READ_ENROLLMENT)
    response = await client.get(URL, headers=await _headers(client, number))
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["isEligible"] is False
    assert {reason["code"] for reason in data["reasons"]} == {
        "ENROLL_FINANCIAL_HOLD",
        "ENROLL_PERIOD_CLOSED",
    }


@pytest.mark.asyncio
async def test_student_without_read_enrollment_gets_403(
    client: AsyncClient, db: AsyncSession
) -> None:
    program, curriculum, _course = await _curriculum(db)
    await _term(db)
    number = "2026-00171-MN-0"
    await _student(db, program=program, curriculum=curriculum, number=number)
    response = await client.get(URL, headers=await _headers(client, number))
    assert response.status_code == 403
    assert "ReadEnrollment" in response.json()["detail"]


def test_no_fixed_legacy_ceiling_literal_in_enrollment_domain() -> None:
    domain = (
        Path(__file__).resolve().parents[3]
        / "packages"
        / "shared"
        / "src"
        / "cbpupsis_shared"
        / "domains"
        / "enrollment"
    )
    assert all('"21"' not in path.read_text() for path in domain.glob("*.py"))
