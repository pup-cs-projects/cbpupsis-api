"""Pass/Fail grading mode tests (US-FAC-003: AC-FAC-003.1 to AC-FAC-003.6)."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_database.models.academics import AcademicTerm
from cbpupsis_database.models.curriculum import Course, Program
from cbpupsis_database.models.enrollment import StudentCourseEnrollment, TermEnrollment
from cbpupsis_database.models.grading import StudentGrade, StudentTermGPA
from cbpupsis_database.models.sections import CourseSection
from cbpupsis_database.models.users import (
    FacultyProfile,
    StudentProfile,
    User,
    UserProfile,
)
from cbpupsis_shared.domains.auth.security import hash_password, totp_at
from cbpupsis_shared.domains.curriculum import service as curriculum_service
from cbpupsis_shared.domains.curriculum.schemas import CatalogCourseUpdate
from cbpupsis_shared.domains.grading.constants import (
    NUMERIC_ALLOWED_MARKS,
    PASS_FAIL_ALLOWED_MARKS,
    READ_GRADE,
    SUBMIT_GRADE,
    UPDATE_GRADE,
)
from cbpupsis_shared.domains.grading.exceptions import GradeModeLockedError
from cbpupsis_shared.domains.grading.service import (
    _evaluate_term_standing,
)
from cbpupsis_shared.domains.iam import service as iam_service
from cbpupsis_shared.domains.sections.constants import READ_COURSE_SECTION

FACULTY_AUTH = "/api/v1/auth/faculty"
STUDENT_AUTH = "/api/v1/auth/student"
FACULTY_PASSWORD = "Faculty-PF-Password-123!"
STUDENT_PASSWORD = "Student-PF-Password-123!"
STUDENT_BIRTHDATE = date(2004, 5, 14)


async def _faculty(db: AsyncSession, employee_id: str, email: str) -> User:
    user = User(
        email=email,
        password_hash=hash_password(FACULTY_PASSWORD),
        is_active=True,
        email_verified_at=datetime.now(UTC),
    )
    db.add(user)
    await db.flush()
    db.add(
        FacultyProfile(
            user_id=user.id,
            employee_id=employee_id,
            department="Computer Science",
        )
    )
    await db.commit()
    return user


async def _faculty_headers(client: AsyncClient, employee_id: str) -> dict[str, str]:
    challenge = (
        await client.post(
            f"{FACULTY_AUTH}/login",
            json={"employee_id": employee_id, "password": FACULTY_PASSWORD},
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
) -> User:
    parts = name.split(" ", 1)
    user = User(
        email=email,
        password_hash=hash_password(STUDENT_PASSWORD),
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
) -> StudentCourseEnrollment:
    term_enrollment = await db.scalar(
        select(TermEnrollment).where(
            TermEnrollment.student_id == student_id,
            TermEnrollment.academic_term_id == term_id,
        )
    )
    if term_enrollment is None:
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
        status="enrolled",
    )
    db.add(item)
    await db.flush()
    return item


@pytest.fixture
async def pass_fail_setup(db: AsyncSession, grant):
    faculty = await _faculty(db, "2021-00921-MN-0", "pf.grader@pup.edu.ph")
    other_faculty = await _faculty(db, "2021-00922-MN-0", "pf.other@pup.edu.ph")
    for perm in (READ_COURSE_SECTION, UPDATE_GRADE, SUBMIT_GRADE):
        await grant(faculty.id, perm)
    for perm in (READ_COURSE_SECTION, UPDATE_GRADE, SUBMIT_GRADE):
        from cbpupsis_database.models.iam import Policy

        policy = await db.scalar(select(Policy).where(Policy.name == f"policy-{perm}"))
        assert policy is not None
        await iam_service.attach_policy_to_user(db, other_faculty.id, policy.id)

    now = datetime.now(UTC)
    term = AcademicTerm(
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
    program = Program(
        code="BSCS",
        name="Bachelor of Science in Computer Science",
        department="Computer Science",
        college="CCIS",
        total_units_required=Decimal("150.0"),
    )
    numeric_course = Course(
        course_code="COMP 301",
        title="Algorithms and Complexity",
        department="Computer Science",
        college="CCIS",
        lecture_units=Decimal("3.0"),
        lab_units=Decimal("0.0"),
        grading_mode="NUMERIC",
        is_nstp=False,
    )
    pf_course = Course(
        course_code="COMP 399",
        title="CS Seminar",
        department="Computer Science",
        college="CCIS",
        lecture_units=Decimal("3.0"),
        lab_units=Decimal("0.0"),
        grading_mode="PASS_FAIL",
        is_nstp=False,
    )
    db.add_all([term, program, numeric_course, pf_course])
    await db.flush()

    numeric_section = CourseSection(
        academic_term_id=term.id,
        course_id=numeric_course.id,
        faculty_id=faculty.id,
        section_name="BSCS 3-1",
        capacity=50,
    )
    pf_section = CourseSection(
        academic_term_id=term.id,
        course_id=pf_course.id,
        faculty_id=faculty.id,
        section_name="BSCS 3-PF",
        capacity=50,
    )
    foreign_pf_section = CourseSection(
        academic_term_id=term.id,
        course_id=pf_course.id,
        faculty_id=other_faculty.id,
        section_name="BSCS 3-PF-B",
        capacity=50,
    )
    db.add_all([numeric_section, pf_section, foreign_pf_section])
    await db.flush()

    alice = await _create_student(
        db,
        program_id=program.id,
        number="2024-00201-MN-0",
        name="Alice Alcantara",
        email="alice.pf@iskolarngbayan.pup.edu.ph",
    )
    bob = await _create_student(
        db,
        program_id=program.id,
        number="2024-00202-MN-0",
        name="Bob Bonifacio",
        email="bob.pf@iskolarngbayan.pup.edu.ph",
    )
    control = await _create_student(
        db,
        program_id=program.id,
        number="2024-00203-MN-0",
        name="Cara Castro",
        email="cara.pf@iskolarngbayan.pup.edu.ph",
    )
    await grant(alice.id, READ_GRADE)
    from cbpupsis_database.models.iam import Policy

    read_grade_policy = await db.scalar(
        select(Policy).where(Policy.name == f"policy-{READ_GRADE}")
    )
    assert read_grade_policy is not None
    await iam_service.attach_policy_to_user(db, bob.id, read_grade_policy.id)
    await iam_service.attach_policy_to_user(db, control.id, read_grade_policy.id)

    alice_num_enr = await _enroll(
        db, student_id=alice.id, term_id=term.id, section_id=numeric_section.id
    )
    alice_pf_enr = await _enroll(
        db, student_id=alice.id, term_id=term.id, section_id=pf_section.id
    )
    bob_pf_enr = await _enroll(
        db, student_id=bob.id, term_id=term.id, section_id=pf_section.id
    )
    control_num_enr = await _enroll(
        db, student_id=control.id, term_id=term.id, section_id=numeric_section.id
    )
    await db.commit()

    return {
        "faculty_id": faculty.id,
        "other_faculty_id": other_faculty.id,
        "term_id": term.id,
        "program_id": program.id,
        "numeric_course_id": numeric_course.id,
        "pf_course_id": pf_course.id,
        "numeric_section_id": numeric_section.id,
        "pf_section_id": pf_section.id,
        "foreign_pf_section_id": foreign_pf_section.id,
        "alice_id": alice.id,
        "bob_id": bob.id,
        "control_id": control.id,
        "alice_num_enr_id": alice_num_enr.id,
        "alice_pf_enr_id": alice_pf_enr.id,
        "bob_pf_enr_id": bob_pf_enr.id,
        "control_num_enr_id": control_num_enr.id,
    }


async def test_ac_fac_003_1_get_grades_returns_grading_mode_and_allowed_marks(
    client: AsyncClient, pass_fail_setup
) -> None:
    setup = pass_fail_setup
    headers = await _faculty_headers(client, "2021-00921-MN-0")

    numeric_resp = await client.get(
        f"/api/v1/faculty/sections/{setup['numeric_section_id']}/grades",
        headers=headers,
    )
    assert numeric_resp.status_code == 200, numeric_resp.text
    numeric_body = numeric_resp.json()
    assert numeric_body["gradingMode"] == "NUMERIC"
    assert numeric_body["allowedMarks"] == list(NUMERIC_ALLOWED_MARKS)

    pf_resp = await client.get(
        f"/api/v1/faculty/sections/{setup['pf_section_id']}/grades",
        headers=headers,
    )
    assert pf_resp.status_code == 200, pf_resp.text
    pf_body = pf_resp.json()
    assert pf_body["gradingMode"] == "PASS_FAIL"
    assert pf_body["allowedMarks"] == list(PASS_FAIL_ALLOWED_MARKS)


@pytest.mark.parametrize("bad_mark", ["1.25", "3.00", "5.00", "0.50", "INC", 1.5])
async def test_ac_fac_003_2_number_on_pass_fail_section_returns_422_and_saves_nothing(
    client: AsyncClient, db: AsyncSession, pass_fail_setup, bad_mark
) -> None:
    setup = pass_fail_setup
    headers = await _faculty_headers(client, "2021-00921-MN-0")
    pf_id = setup["pf_section_id"]

    # Seed an existing valid PASS draft for Alice so we verify nothing is overwritten.
    seed = await client.put(
        f"/api/v1/faculty/sections/{pf_id}/grades/draft",
        headers=headers,
        json={
            "version": 1,
            "grades": [
                {"enrollmentId": str(setup["alice_pf_enr_id"]), "finalGrade": "PASS"}
            ],
        },
    )
    assert seed.status_code == 200

    bad = await client.put(
        f"/api/v1/faculty/sections/{pf_id}/grades/draft",
        headers=headers,
        json={
            "version": 2,
            "grades": [
                {"enrollmentId": str(setup["alice_pf_enr_id"]), "finalGrade": "FAIL"},
                {"enrollmentId": str(setup["bob_pf_enr_id"]), "finalGrade": bad_mark},
            ],
        },
    )
    assert bad.status_code == 422, bad.text
    body = bad.json()
    assert body["code"] == "GRADE_MODE_MISMATCH"
    assert body["gradingMode"] == "PASS_FAIL"
    assert body["allowedMarks"] == list(PASS_FAIL_ALLOWED_MARKS)

    reread = await client.get(
        f"/api/v1/faculty/sections/{pf_id}/grades", headers=headers
    )
    assert reread.json()["version"] == 2
    by_num = {g["studentNumber"]: g for g in reread.json()["grades"]}
    assert by_num["2024-00201-MN-0"]["finalGrade"] == "PASS"
    assert by_num["2024-00202-MN-0"]["finalGrade"] is None


async def test_ac_fac_003_2_finalize_with_stored_numeric_draft_on_pass_fail_returns_422(
    client: AsyncClient, db: AsyncSession, pass_fail_setup
) -> None:
    setup = pass_fail_setup
    headers = await _faculty_headers(client, "2021-00921-MN-0")
    pf_id = setup["pf_section_id"]
    now = datetime.now(UTC)

    db.add_all(
        [
            StudentGrade(
                enrollment_item_id=setup["alice_pf_enr_id"],
                final_grade="1.25",
                is_draft=True,
                is_locked=False,
                updated_at=now,
            ),
            StudentGrade(
                enrollment_item_id=setup["bob_pf_enr_id"],
                final_grade="PASS",
                is_draft=True,
                is_locked=False,
                updated_at=now,
            ),
        ]
    )
    await db.commit()

    resp = await client.post(
        f"/api/v1/faculty/sections/{pf_id}/grades/finalize",
        headers=headers,
    )
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body["code"] == "GRADE_MODE_MISMATCH"
    assert body["gradingMode"] == "PASS_FAIL"
    assert body["allowedMarks"] == list(PASS_FAIL_ALLOWED_MARKS)

    reread = await client.get(
        f"/api/v1/faculty/sections/{pf_id}/grades", headers=headers
    )
    assert reread.json()["state"] == "DRAFT"


@pytest.mark.parametrize("bad_mark", ["PASS", "FAIL", "Pass", "fail"])
async def test_ac_fac_003_3_pass_fail_on_numeric_section_returns_422_and_saves_nothing(
    client: AsyncClient, pass_fail_setup, bad_mark: str
) -> None:
    setup = pass_fail_setup
    headers = await _faculty_headers(client, "2021-00921-MN-0")
    num_id = setup["numeric_section_id"]

    seed = await client.put(
        f"/api/v1/faculty/sections/{num_id}/grades/draft",
        headers=headers,
        json={
            "version": 1,
            "grades": [
                {"enrollmentId": str(setup["alice_num_enr_id"]), "finalGrade": "1.75"}
            ],
        },
    )
    assert seed.status_code == 200

    bad = await client.put(
        f"/api/v1/faculty/sections/{num_id}/grades/draft",
        headers=headers,
        json={
            "version": 2,
            "grades": [
                {"enrollmentId": str(setup["alice_num_enr_id"]), "finalGrade": "1.25"},
                {
                    "enrollmentId": str(setup["control_num_enr_id"]),
                    "finalGrade": bad_mark,
                },
            ],
        },
    )
    assert bad.status_code == 422, bad.text
    body = bad.json()
    assert body["code"] == "GRADE_MODE_MISMATCH"
    assert body["gradingMode"] == "NUMERIC"
    assert body["allowedMarks"] == list(NUMERIC_ALLOWED_MARKS)

    reread = await client.get(
        f"/api/v1/faculty/sections/{num_id}/grades", headers=headers
    )
    assert reread.json()["version"] == 2
    by_num = {g["studentNumber"]: g for g in reread.json()["grades"]}
    assert by_num["2024-00201-MN-0"]["finalGrade"] == "1.75"
    assert by_num["2024-00203-MN-0"]["finalGrade"] is None


async def test_ac_fac_003_4_finalize_pass_keeps_gwa_and_grows_units_earned(
    client: AsyncClient, pass_fail_setup
) -> None:
    setup = pass_fail_setup
    headers = await _faculty_headers(client, "2021-00921-MN-0")

    # Finalize numeric section first: Alice 1.50 (3 units), Control 2.00 (3 units).
    assert (
        await client.put(
            f"/api/v1/faculty/sections/{setup['numeric_section_id']}/grades/draft",
            headers=headers,
            json={
                "version": 1,
                "grades": [
                    {
                        "enrollmentId": str(setup["alice_num_enr_id"]),
                        "finalGrade": "1.50",
                    },
                    {
                        "enrollmentId": str(setup["control_num_enr_id"]),
                        "finalGrade": "2.00",
                    },
                ],
            },
        )
    ).status_code == 200
    assert (
        await client.post(
            f"/api/v1/faculty/sections/{setup['numeric_section_id']}/grades/finalize",
            headers=headers,
        )
    ).status_code == 200

    alice_headers = await _student_headers(client, "2024-00201-MN-0")
    before = (await client.get("/api/v1/student/grades", headers=alice_headers)).json()
    assert before["gwa"] == "1.50"
    assert Decimal(before["units_earned"]) == Decimal("3.0")

    # Finalize PASS for Alice and Bob in the 3-unit PASS_FAIL course.
    assert (
        await client.put(
            f"/api/v1/faculty/sections/{setup['pf_section_id']}/grades/draft",
            headers=headers,
            json={
                "version": 1,
                "grades": [
                    {
                        "enrollmentId": str(setup["alice_pf_enr_id"]),
                        "finalGrade": "PASS",
                    },
                    {
                        "enrollmentId": str(setup["bob_pf_enr_id"]),
                        "finalGrade": "PASS",
                    },
                ],
            },
        )
    ).status_code == 200
    assert (
        await client.post(
            f"/api/v1/faculty/sections/{setup['pf_section_id']}/grades/finalize",
            headers=headers,
        )
    ).status_code == 200

    after = (await client.get("/api/v1/student/grades", headers=alice_headers)).json()
    assert after["gwa"] == "1.50"
    assert Decimal(after["units_earned"]) == Decimal("6.0")
    assert after["academic_standing"] == "Good Standing"
    assert after["standing_state"] == "evaluated"
    pf_row = next(c for c in after["courses"] if c["code"] == "COMP 399")
    assert pf_row["final_grade"] == "PASS"
    assert pf_row["status"] == "Passed"

    # Student with only PASS has null GWA and 3.0 earned units.
    bob_headers = await _student_headers(client, "2024-00202-MN-0")
    bob_grades = (
        await client.get("/api/v1/student/grades", headers=bob_headers)
    ).json()
    assert bob_grades["gwa"] is None
    assert Decimal(bob_grades["units_earned"]) == Decimal("3.0")
    assert bob_grades["academic_standing"] == "Good Standing"
    assert bob_grades["standing_state"] == "evaluated"


async def test_ac_fac_003_5_finalize_fail_counts_3_units_in_standing_same_as_5_00(
    client: AsyncClient, db: AsyncSession, pass_fail_setup
) -> None:
    setup = pass_fail_setup
    headers = await _faculty_headers(client, "2021-00921-MN-0")

    # Add a second numeric 3-unit course so Control has 1.50 (3u) + 5.00 (3u).
    control_fail_course = Course(
        course_code="COMP 302",
        title="Operating Systems",
        department="Computer Science",
        college="CCIS",
        lecture_units=Decimal("3.0"),
        lab_units=Decimal("0.0"),
        grading_mode="NUMERIC",
        is_nstp=False,
    )
    db.add(control_fail_course)
    await db.flush()
    control_fail_section = CourseSection(
        academic_term_id=setup["term_id"],
        course_id=control_fail_course.id,
        faculty_id=setup["faculty_id"],
        section_name="BSCS 3-OS",
        capacity=30,
    )
    db.add(control_fail_section)
    await db.flush()
    control_fail_enr = await _enroll(
        db,
        student_id=setup["control_id"],
        term_id=setup["term_id"],
        section_id=control_fail_section.id,
    )
    await db.commit()

    # Numeric section: Alice 1.50, Control 1.50.
    assert (
        await client.put(
            f"/api/v1/faculty/sections/{setup['numeric_section_id']}/grades/draft",
            headers=headers,
            json={
                "version": 1,
                "grades": [
                    {
                        "enrollmentId": str(setup["alice_num_enr_id"]),
                        "finalGrade": "1.50",
                    },
                    {
                        "enrollmentId": str(setup["control_num_enr_id"]),
                        "finalGrade": "1.50",
                    },
                ],
            },
        )
    ).status_code == 200
    assert (
        await client.post(
            f"/api/v1/faculty/sections/{setup['numeric_section_id']}/grades/finalize",
            headers=headers,
        )
    ).status_code == 200

    # Control gets 5.00 on the 3-unit COMP 302 course.
    assert (
        await client.put(
            f"/api/v1/faculty/sections/{control_fail_section.id}/grades/draft",
            headers=headers,
            json={
                "version": 1,
                "grades": [
                    {"enrollmentId": str(control_fail_enr.id), "finalGrade": "5.00"}
                ],
            },
        )
    ).status_code == 200
    assert (
        await client.post(
            f"/api/v1/faculty/sections/{control_fail_section.id}/grades/finalize",
            headers=headers,
        )
    ).status_code == 200

    # Alice gets FAIL on the 3-unit PASS_FAIL COMP 399 course; Bob also gets FAIL.
    assert (
        await client.put(
            f"/api/v1/faculty/sections/{setup['pf_section_id']}/grades/draft",
            headers=headers,
            json={
                "version": 1,
                "grades": [
                    {
                        "enrollmentId": str(setup["alice_pf_enr_id"]),
                        "finalGrade": "FAIL",
                    },
                    {
                        "enrollmentId": str(setup["bob_pf_enr_id"]),
                        "finalGrade": "FAIL",
                    },
                ],
            },
        )
    ).status_code == 200
    assert (
        await client.post(
            f"/api/v1/faculty/sections/{setup['pf_section_id']}/grades/finalize",
            headers=headers,
        )
    ).status_code == 200

    alice_eval = await db.scalar(
        select(StudentTermGPA).where(
            StudentTermGPA.student_id == setup["alice_id"],
            StudentTermGPA.academic_term_id == setup["term_id"],
        )
    )
    control_eval = await db.scalar(
        select(StudentTermGPA).where(
            StudentTermGPA.student_id == setup["control_id"],
            StudentTermGPA.academic_term_id == setup["term_id"],
        )
    )
    bob_eval = await db.scalar(
        select(StudentTermGPA).where(
            StudentTermGPA.student_id == setup["bob_id"],
            StudentTermGPA.academic_term_id == setup["term_id"],
        )
    )
    assert alice_eval is not None and control_eval is not None and bob_eval is not None
    # 3 failed units out of 6 total units = 50% -> Probation, matching 5.00.
    assert alice_eval.academic_standing == control_eval.academic_standing == "Probation"
    # 3 failed units out of 3 total units = 100% -> University Dismissal.
    assert bob_eval.academic_standing == "University Dismissal"

    # Student grade reads reflect finalized standing without manual recompute.
    alice_headers = await _student_headers(client, "2024-00201-MN-0")
    alice_grades = (
        await client.get("/api/v1/student/grades", headers=alice_headers)
    ).json()
    assert alice_grades["gwa"] == "1.50"
    assert Decimal(alice_grades["units_earned"]) == Decimal("3.0")
    assert alice_grades["academic_standing"] == "Probation"
    assert alice_grades["standing_state"] == "evaluated"
    alice_pf = next(c for c in alice_grades["courses"] if c["code"] == "COMP 399")
    assert alice_pf["final_grade"] == "FAIL"
    assert alice_pf["status"] == "Failed"

    control_headers = await _student_headers(client, "2024-00203-MN-0")
    control_grades = (
        await client.get("/api/v1/student/grades", headers=control_headers)
    ).json()
    assert control_grades["academic_standing"] == "Probation"
    assert control_grades["standing_state"] == "evaluated"
    control_fail = next(c for c in control_grades["courses"] if c["code"] == "COMP 302")
    assert control_fail["final_grade"] == "5.00"
    assert control_fail["status"] == "Failed"

    bob_headers = await _student_headers(client, "2024-00202-MN-0")
    bob_grades = (
        await client.get("/api/v1/student/grades", headers=bob_headers)
    ).json()
    assert bob_grades["gwa"] is None
    assert Decimal(bob_grades["units_earned"]) == Decimal("0.0")
    assert bob_grades["academic_standing"] == "University Dismissal"
    assert bob_grades["standing_state"] == "evaluated"
    assert bob_grades["courses"][0]["final_grade"] == "FAIL"
    assert bob_grades["courses"][0]["status"] == "Failed"

    assert _evaluate_term_standing([]) == "Good Standing"


async def test_ac_fac_003_6_pass_fail_draft_finalize_then_write_returns_409_locked(
    client: AsyncClient, pass_fail_setup
) -> None:
    setup = pass_fail_setup
    headers = await _faculty_headers(client, "2021-00921-MN-0")
    pf_id = setup["pf_section_id"]

    draft = await client.put(
        f"/api/v1/faculty/sections/{pf_id}/grades/draft",
        headers=headers,
        json={
            "version": 1,
            "grades": [
                {"enrollmentId": str(setup["alice_pf_enr_id"]), "finalGrade": "Pass"},
                {"enrollmentId": str(setup["bob_pf_enr_id"]), "finalGrade": "Fail"},
            ],
        },
    )
    assert draft.status_code == 200, draft.text
    by_num = {g["studentNumber"]: g for g in draft.json()["grades"]}
    assert by_num["2024-00201-MN-0"]["finalGrade"] == "PASS"
    assert by_num["2024-00202-MN-0"]["finalGrade"] == "FAIL"

    finalized = await client.post(
        f"/api/v1/faculty/sections/{pf_id}/grades/finalize",
        headers=headers,
        json={"version": 2},
    )
    assert finalized.status_code == 200, finalized.text
    assert finalized.json()["state"] == "FINAL"

    after_draft = await client.put(
        f"/api/v1/faculty/sections/{pf_id}/grades/draft",
        headers=headers,
        json={
            "version": 3,
            "grades": [
                {"enrollmentId": str(setup["alice_pf_enr_id"]), "finalGrade": "FAIL"}
            ],
        },
    )
    assert after_draft.status_code == 409
    assert after_draft.json()["code"] == "GRADES_LOCKED"

    after_finalize = await client.post(
        f"/api/v1/faculty/sections/{pf_id}/grades/finalize",
        headers=headers,
    )
    assert after_finalize.status_code == 409
    assert after_finalize.json()["code"] == "GRADES_LOCKED"


async def test_changing_grading_mode_with_marks_returns_409_grade_mode_locked(
    client: AsyncClient, db: AsyncSession, pass_fail_setup
) -> None:
    setup = pass_fail_setup
    headers = await _faculty_headers(client, "2021-00921-MN-0")

    # Changing mode before any marks exist succeeds cleanly.
    flipped = await curriculum_service.update_catalog_course(
        db,
        course_id=setup["pf_course_id"],
        payload=CatalogCourseUpdate(grading_mode="NUMERIC"),
        actor_id=setup["faculty_id"],
        position="chairperson",
        department_id="Computer Science",
        college_id="CCIS",
    )
    assert flipped.grading_mode == "NUMERIC"

    restored = await curriculum_service.update_catalog_course(
        db,
        course_id=setup["pf_course_id"],
        payload=CatalogCourseUpdate(grading_mode="PASS_FAIL"),
        actor_id=setup["faculty_id"],
        position="chairperson",
        department_id="Computer Science",
        college_id="CCIS",
    )
    assert restored.grading_mode == "PASS_FAIL"

    # Save a draft mark on the section, then attempt to change grading mode.
    draft = await client.put(
        f"/api/v1/faculty/sections/{setup['pf_section_id']}/grades/draft",
        headers=headers,
        json={
            "version": 1,
            "grades": [
                {"enrollmentId": str(setup["alice_pf_enr_id"]), "finalGrade": "PASS"}
            ],
        },
    )
    assert draft.status_code == 200

    with pytest.raises(GradeModeLockedError) as exc_info:
        await curriculum_service.update_catalog_course(
            db,
            course_id=setup["pf_course_id"],
            payload=CatalogCourseUpdate(grading_mode="NUMERIC"),
            actor_id=setup["faculty_id"],
            position="chairperson",
            department_id="Computer Science",
            college_id="CCIS",
        )
    assert exc_info.value.status_code == 409
    assert exc_info.value.code == "GRADE_MODE_LOCKED"

    # Updating unrelated metadata without changing grading mode remains allowed.
    unchanged_mode = await curriculum_service.update_catalog_course(
        db,
        course_id=setup["pf_course_id"],
        payload=CatalogCourseUpdate(
            title="CS Seminar Updated", grading_mode="PASS_FAIL"
        ),
        actor_id=setup["faculty_id"],
        position="chairperson",
        department_id="Computer Science",
        college_id="CCIS",
    )
    assert unchanged_mode.title == "CS Seminar Updated"
    assert unchanged_mode.grading_mode == "PASS_FAIL"


async def test_foreign_faculty_on_pass_fail_section_returns_404_resource_not_found(
    client: AsyncClient, pass_fail_setup
) -> None:
    setup = pass_fail_setup
    headers = await _faculty_headers(client, "2021-00921-MN-0")
    foreign_id = setup["foreign_pf_section_id"]

    for method, suffix, json_body in (
        ("GET", "/grades", None),
        ("PUT", "/grades/draft", {"version": 1, "grades": []}),
        ("POST", "/grades/finalize", None),
    ):
        resp = await client.request(
            method,
            f"/api/v1/faculty/sections/{foreign_id}{suffix}",
            headers=headers,
            json=json_body,
        )
        assert resp.status_code == 404
        assert resp.json()["code"] == "RESOURCE_NOT_FOUND"
