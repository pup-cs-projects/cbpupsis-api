"""US-ENRL-005 (#187) admin catalog, curriculum, section, and substitution tests.

Covers AC-ENRL-005.1 through AC-ENRL-005.9:
- Course creation (201) and editing (200)
- Prerequisite and co-requisite cycle refusal (422 CATALOG_PREREQUISITE_CYCLE) with
  direct self-reference and three-course cycle
- Publishing curriculum versions with effectiveAdmissionYear (201) preserving
  prescribed load on GET /api/v1/student/enrollment/eligibility for earlier admits
- Deleting curriculum versions with bound students (409 CATALOG_VERSION_IN_USE)
  vs. unused versions (204)
- Creating (201) and editing (200) course sections, refusing capacity below enrolled
  count (409 SECTION_CAPACITY_BELOW_ENROLLED with details.enrolled)
- Approving course substitutions (201) and counting the substitute toward degree
  progress for named students only
- Role and data scope enforcement for Chair, Dean, Registrar, and Student
- Audit trail entries on every write operation (actor, old values, new values, time)
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_database.models.academics import AcademicTerm
from cbpupsis_database.models.audit import AuditEntry
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
from cbpupsis_database.models.grading import StudentGrade
from cbpupsis_database.models.iam import Group
from cbpupsis_database.models.sections import CourseSection
from cbpupsis_database.models.users import (
    AdminProfile,
    FacultyProfile,
    StudentProfile,
    User,
)
from cbpupsis_shared.domains.auth.constants import (
    ROLE_ADMIN,
    ROLE_STUDENT,
)
from cbpupsis_shared.domains.auth.security import hash_password
from cbpupsis_shared.domains.auth.service.tokens import issue_role_session
from cbpupsis_shared.domains.iam import service as iam_service
from cbpupsis_shared.domains.iam.constants import (
    ADMIN_GROUP,
    FACULTY_GROUP,
    STUDENT_GROUP,
)
from scripts import seed_iam

ADMIN_CATALOG = "/api/v1/admin/catalog"
PASSWORD = "Catalog-Test-Password-123!"


async def _group_id(db: AsyncSession, name: str) -> uuid.UUID:
    group = await db.scalar(select(Group).where(Group.name == name))
    assert group is not None
    return group.id


async def _make_admin(
    db: AsyncSession,
    *,
    email: str,
    position: str,
    department: str | None = None,
    college: str | None = None,
) -> tuple[User, dict[str, str]]:
    user = User(
        email=email,
        full_name=f"Admin {position.title()}",
        password_hash=hash_password(PASSWORD),
        is_active=True,
        email_verified_at=datetime.now(UTC),
    )
    db.add(user)
    await db.flush()
    db.add(
        AdminProfile(
            user_id=user.id,
            position=position,
            department=department,
            college=college,
            is_active=True,
        )
    )
    admin_group_id = await _group_id(db, ADMIN_GROUP)
    await iam_service.add_user_to_group(db, user.id, admin_group_id)
    await iam_service.stage_admin_position_policies(db, [(user.id, position)])
    await db.commit()
    pair = await issue_role_session(
        db,
        user.id,
        role=ROLE_ADMIN,
        ip_address="127.0.0.1",
        mfa=True,
        claims={
            "position": position,
            "department_id": department,
            "college_id": college,
        },
    )
    return user, {"Authorization": f"Bearer {pair.access_token}"}


async def _make_faculty(
    db: AsyncSession,
    *,
    email: str,
    employee_id: str,
    department: str = "Department of Computer and Information Sciences",
) -> User:
    user = User(
        email=email,
        full_name="Faculty Member",
        password_hash=hash_password(PASSWORD),
        is_active=True,
        email_verified_at=datetime.now(UTC),
    )
    db.add(user)
    await db.flush()
    db.add(
        FacultyProfile(
            user_id=user.id,
            employee_id=employee_id,
            department=department,
            is_active=True,
        )
    )
    faculty_group_id = await _group_id(db, FACULTY_GROUP)
    await iam_service.add_user_to_group(db, user.id, faculty_group_id)
    await db.commit()
    return user


async def _make_student(
    db: AsyncSession,
    *,
    email: str,
    student_number: str,
    program_code: str = "BSCS",
    year_level: int = 1,
    curriculum_id: uuid.UUID | None = None,
) -> tuple[User, dict[str, str]]:
    program = await db.scalar(select(Program).where(Program.code == program_code))
    assert program is not None
    user = User(
        email=email,
        full_name=f"Student {student_number}",
        password_hash=hash_password(PASSWORD),
        is_active=True,
        email_verified_at=datetime.now(UTC),
    )
    db.add(user)
    await db.flush()
    db.add(
        StudentProfile(
            user_id=user.id,
            student_number=student_number,
            program_id=program.id,
            year_level=year_level,
            curriculum_id=curriculum_id,
            enrollment_status="Active",
            admission_status="continuing",
        )
    )
    student_group_id = await _group_id(db, STUDENT_GROUP)
    await iam_service.add_user_to_group(db, user.id, student_group_id)
    await db.commit()
    pair = await issue_role_session(
        db,
        user.id,
        role=ROLE_STUDENT,
        ip_address="127.0.0.1",
    )
    return user, {"Authorization": f"Bearer {pair.access_token}"}


def _course_row(
    *,
    code: str,
    title: str,
    units: Decimal,
    department: str,
    college: str,
) -> Course:
    return Course(
        course_code=code,
        title=title,
        lecture_units=units,
        lab_units=Decimal("0.0"),
        department=department,
        college=college,
        is_active=True,
    )


@pytest.fixture
async def catalog_env(db: AsyncSession):
    """Seed IAM permissions/policies and base programs/terms/actors."""
    await seed_iam.seed(db)

    dcis = "Department of Computer and Information Sciences"
    dmath = "Department of Mathematics and Statistics"
    ccis = "College of Computer and Information Sciences"
    cs_college = "College of Science"
    cba = "College of Business Administration"
    dacc = "Department of Accountancy"

    bscs = Program(
        code="BSCS",
        name="Bachelor of Science in Computer Science",
        department=dcis,
        college=ccis,
        total_units_required=Decimal("150.0"),
        is_active=True,
    )
    bsit = Program(
        code="BSIT",
        name="Bachelor of Science in Information Technology",
        department=dcis,
        college=ccis,
        total_units_required=Decimal("148.0"),
        is_active=True,
    )
    bsmath = Program(
        code="BSMATH",
        name="Bachelor of Science in Mathematics",
        department=dmath,
        college=cs_college,
        total_units_required=Decimal("144.0"),
        is_active=True,
    )
    bsa = Program(
        code="BSA",
        name="Bachelor of Science in Accountancy",
        department=dacc,
        college=cba,
        total_units_required=Decimal("160.0"),
        is_active=True,
    )
    now = datetime.now(UTC)
    term = AcademicTerm(
        academic_year="2026-2027",
        semester="1",
        is_current=True,
        start_date=date(2026, 8, 15),
        end_date=date(2026, 12, 20),
        regular_enrollment_start=now,
        regular_enrollment_end=now,
        add_drop_deadline=now,
        grade_submission_deadline=now,
    )
    db.add_all([bscs, bsit, bsmath, bsa, term])
    await db.commit()

    chair_user, chair_headers = await _make_admin(
        db,
        email="chair.dcis@pup.edu.ph",
        position="chairperson",
        department=dcis,
        college=ccis,
    )
    other_chair_user, other_chair_headers = await _make_admin(
        db,
        email="chair.math@pup.edu.ph",
        position="chairperson",
        department=dmath,
        college=cs_college,
    )
    dean_user, dean_headers = await _make_admin(
        db,
        email="dean.ccis@pup.edu.ph",
        position="dean",
        college=ccis,
    )
    registrar_user, registrar_headers = await _make_admin(
        db,
        email="registrar@pup.edu.ph",
        position="registrar",
    )
    finance_user, finance_headers = await _make_admin(
        db,
        email="finance@pup.edu.ph",
        position="finance",
    )
    faculty = await _make_faculty(
        db,
        email="prof.santos@pup.edu.ph",
        employee_id="2020-00101-MN-0",
        department=dcis,
    )

    return {
        "dcis": dcis,
        "dmath": dmath,
        "ccis": ccis,
        "cs_college": cs_college,
        "cba": cba,
        "dacc": dacc,
        "bscs": bscs,
        "bsit": bsit,
        "bsmath": bsmath,
        "bsa": bsa,
        "term": term,
        "chair_user": chair_user,
        "chair_headers": chair_headers,
        "other_chair_user": other_chair_user,
        "other_chair_headers": other_chair_headers,
        "dean_user": dean_user,
        "dean_headers": dean_headers,
        "registrar_user": registrar_user,
        "registrar_headers": registrar_headers,
        "finance_user": finance_user,
        "finance_headers": finance_headers,
        "faculty": faculty,
    }


async def test_ac_enrl_005_1_course_create_and_edit(
    client: AsyncClient, catalog_env
) -> None:
    """Course create returns 201 and edit returns 200 in the Chair's department."""
    headers = catalog_env["chair_headers"]

    create_resp = await client.post(
        f"{ADMIN_CATALOG}/courses",
        headers=headers,
        json={
            "code": "COMP 20013",
            "title": "Data Structures and Algorithms",
            "description": "Fundamental data structures and algorithmic analysis.",
            "units": 3.0,
            "lectureHours": 2,
            "labHours": 3,
            "termOffered": "First Semester",
        },
    )
    assert create_resp.status_code == 201, create_resp.text
    created = create_resp.json()
    assert created["code"] == "COMP 20013"
    assert created["title"] == "Data Structures and Algorithms"
    assert float(created["units"]) == 3.0
    assert created["lectureHours"] == 2
    assert created["labHours"] == 3
    assert created["termOffered"] == "First Semester"
    assert created["department"] == catalog_env["dcis"]
    assert created["college"] == catalog_env["ccis"]
    assert created["isActive"] is True

    course_id = created["id"]
    patch_resp = await client.patch(
        f"{ADMIN_CATALOG}/courses/{course_id}",
        headers=headers,
        json={
            "title": "Advanced Data Structures and Algorithms",
            "units": 4.0,
            "lectureHours": 3,
            "labHours": 3,
        },
    )
    assert patch_resp.status_code == 200, patch_resp.text
    updated = patch_resp.json()
    assert updated["id"] == course_id
    assert updated["title"] == "Advanced Data Structures and Algorithms"
    assert float(updated["units"]) == 4.0
    assert updated["lectureHours"] == 3


async def test_ac_enrl_005_2_prerequisite_and_corequisite_cycle_refusal(
    client: AsyncClient, catalog_env
) -> None:
    """Cycle refusal is tested with a direct self-reference and a three-course cycle."""
    headers = catalog_env["chair_headers"]

    # Create three courses in the Chair's department
    for code, title in [
        ("CS101", "Intro to Computing"),
        ("CS102", "Intermediate Programming"),
        ("CS103", "Software Engineering"),
    ]:
        resp = await client.post(
            f"{ADMIN_CATALOG}/courses",
            headers=headers,
            json={"code": code, "title": title, "units": 3.0},
        )
        assert resp.status_code == 201, resp.text

    # 1. Direct self-reference cycle via course edit
    cs101_list = (
        await client.get(
            f"{ADMIN_CATALOG}/courses", params={"search": "CS101"}, headers=headers
        )
    ).json()["items"]
    cs101_id = cs101_list[0]["id"]

    self_ref_resp = await client.patch(
        f"{ADMIN_CATALOG}/courses/{cs101_id}",
        headers=headers,
        json={"prerequisiteCourseCodes": ["CS101"]},
    )
    assert self_ref_resp.status_code == 422, self_ref_resp.text
    self_ref_body = self_ref_resp.json()
    assert self_ref_body["code"] == "CATALOG_PREREQUISITE_CYCLE"
    assert self_ref_body["details"]["cycle"] == ["CS101", "CS101"]
    assert "CS101" in self_ref_body["message"]

    # 2. Direct self-reference via co-requisite on course create
    self_coreq_resp = await client.post(
        f"{ADMIN_CATALOG}/courses",
        headers=headers,
        json={
            "code": "CS104",
            "title": "Operating Systems",
            "units": 3.0,
            "corequisiteCourseCodes": ["CS104"],
        },
    )
    assert self_coreq_resp.status_code == 422, self_coreq_resp.text
    assert self_coreq_resp.json()["code"] == "CATALOG_PREREQUISITE_CYCLE"
    assert self_coreq_resp.json()["details"]["cycle"] == ["CS104", "CS104"]

    # 3. Three-course cycle on curriculum publish (CS101 -> CS102 -> CS103 -> CS101)
    three_cycle_resp = await client.post(
        f"{ADMIN_CATALOG}/curricula/BSCS/versions",
        headers=headers,
        json={
            "version": "2026-CYCLE",
            "effectiveAdmissionYear": 2026,
            "courses": [
                {
                    "courseCode": "CS101",
                    "yearLevel": 1,
                    "semester": "1",
                    "prerequisiteCourseCodes": ["CS103"],
                },
                {
                    "courseCode": "CS102",
                    "yearLevel": 1,
                    "semester": "2",
                    "prerequisiteCourseCodes": ["CS101"],
                },
                {
                    "courseCode": "CS103",
                    "yearLevel": 2,
                    "semester": "1",
                    "corequisiteCourseCodes": ["CS102"],
                },
            ],
        },
    )
    assert three_cycle_resp.status_code == 422, three_cycle_resp.text
    cycle_body = three_cycle_resp.json()
    assert cycle_body["code"] == "CATALOG_PREREQUISITE_CYCLE"
    cycle_codes = cycle_body["details"]["cycle"]
    assert len(cycle_codes) == 4
    assert cycle_codes[0] == cycle_codes[-1]
    assert set(cycle_codes) == {"CS101", "CS102", "CS103"}
    for c_code in ("CS101", "CS102", "CS103"):
        assert c_code in cycle_body["message"]


async def test_ac_enrl_005_3_publish_curriculum_version_preserves_earlier_student_load(
    client: AsyncClient, db: AsyncSession, catalog_env
) -> None:
    """A student admitted earlier keeps the old curriculum version and load."""
    headers = catalog_env["chair_headers"]
    bscs: Program = catalog_env["bscs"]

    # Create courses for old and new curriculum versions
    courses_data = [
        ("COMP 101", "Intro to CS", Decimal("3.0")),
        ("COMP 102", "Programming 1", Decimal("3.0")),
        ("COMP 103", "Discrete Structures", Decimal("3.0")),
    ]
    created_courses: dict[str, Course] = {}
    for code, title, units in courses_data:
        c = _course_row(
            code=code,
            title=title,
            units=units,
            department=catalog_env["dcis"],
            college=catalog_env["ccis"],
        )
        db.add(c)
        created_courses[code] = c
    await db.flush()

    # Old curriculum version (2023, 6 units in Year 1 Sem 1)
    old_curr = Curriculum(
        program_id=bscs.id,
        curriculum_year="2023",
        effective_admission_year=2023,
        is_active=True,
    )
    db.add(old_curr)
    await db.flush()
    db.add_all(
        [
            CurriculumCourse(
                curriculum_id=old_curr.id,
                course_id=created_courses["COMP 101"].id,
                year_level=1,
                semester="1",
            ),
            CurriculumCourse(
                curriculum_id=old_curr.id,
                course_id=created_courses["COMP 102"].id,
                year_level=1,
                semester="1",
            ),
        ]
    )
    await db.commit()

    # Student admitted in 2023 (bound to old_curr) and student admitted in 2026
    _, student_2023_headers = await _make_student(
        db,
        email="student2023@pup.edu.ph",
        student_number="2023-00001-MN-0",
        program_code="BSCS",
        year_level=1,
        curriculum_id=old_curr.id,
    )
    _, student_2026_headers = await _make_student(
        db,
        email="student2026@pup.edu.ph",
        student_number="2026-00001-MN-0",
        program_code="BSCS",
        year_level=1,
        curriculum_id=old_curr.id,
    )

    # Check 2023 student's eligibility before publishing 2026 curriculum version
    before_resp = await client.get(
        "/api/v1/student/enrollment/eligibility", headers=student_2023_headers
    )
    assert before_resp.status_code == 200, before_resp.text
    before_data = before_resp.json()
    assert before_data["curriculumVersion"] == "2023"
    assert float(before_data["prescribedLoad"]) == 6.0

    # Chair publishes a new 2026 curriculum version with 9.0 units in Year 1 Sem 1
    pub_resp = await client.post(
        f"{ADMIN_CATALOG}/curricula/BSCS/versions",
        headers=headers,
        json={
            "version": "2026",
            "effectiveAcademicYear": "2026-2027",
            "effectiveAdmissionYear": 2026,
            "courses": [
                {"courseCode": "COMP 101", "yearLevel": 1, "semester": "1"},
                {
                    "courseCode": "COMP 102",
                    "yearLevel": 1,
                    "semester": "1",
                    "corequisiteCourseCodes": ["COMP 101"],
                },
                {"courseCode": "COMP 103", "yearLevel": 1, "semester": "1"},
            ],
        },
    )
    assert pub_resp.status_code == 201, pub_resp.text
    pub_body = pub_resp.json()
    assert pub_body["version"] == "2026"
    assert pub_body["effectiveAdmissionYear"] == 2026
    assert len(pub_body["courses"]) == 3

    # Earlier student (2023) keeps old version and prescribedLoad = 6.0
    after_2023_resp = await client.get(
        "/api/v1/student/enrollment/eligibility", headers=student_2023_headers
    )
    assert after_2023_resp.status_code == 200, after_2023_resp.text
    after_2023_data = after_2023_resp.json()
    assert after_2023_data["curriculumId"] == str(old_curr.id)
    assert after_2023_data["curriculumVersion"] == "2023"
    assert float(after_2023_data["prescribedLoad"]) == 6.0

    # Student admitted in 2026 gets the newly published 2026 version (load = 9.0)
    after_2026_resp = await client.get(
        "/api/v1/student/enrollment/eligibility", headers=student_2026_headers
    )
    assert after_2026_resp.status_code == 200, after_2026_resp.text
    after_2026_data = after_2026_resp.json()
    assert after_2026_data["curriculumId"] == pub_body["id"]
    assert after_2026_data["curriculumVersion"] == "2026"
    assert float(after_2026_data["prescribedLoad"]) == 9.0


async def test_ac_enrl_005_4_delete_curriculum_version_in_use_and_unused(
    client: AsyncClient, db: AsyncSession, catalog_env
) -> None:
    """Deleting a version with bound students returns 409 CATALOG_VERSION_IN_USE."""
    headers = catalog_env["chair_headers"]

    course = _course_row(
        code="COMP 301",
        title="Automata Theory",
        units=Decimal("3.0"),
        department=catalog_env["dcis"],
        college=catalog_env["ccis"],
    )
    db.add(course)
    await db.commit()

    # Publish a version that binds a 2025 student
    await _make_student(
        db,
        email="student2025@pup.edu.ph",
        student_number="2025-00010-MN-0",
        program_code="BSCS",
        year_level=1,
    )
    bound_ver_resp = await client.post(
        f"{ADMIN_CATALOG}/curricula/BSCS/versions",
        headers=headers,
        json={
            "version": "2025-BOUND",
            "effectiveAdmissionYear": 2025,
            "courses": [{"courseCode": "COMP 301", "yearLevel": 1, "semester": "1"}],
        },
    )
    assert bound_ver_resp.status_code == 201, bound_ver_resp.text
    bound_version_id = bound_ver_resp.json()["id"]

    # Deleting the bound version must fail with 409 CATALOG_VERSION_IN_USE
    del_bound = await client.delete(
        f"{ADMIN_CATALOG}/curricula/BSCS/versions/{bound_version_id}",
        headers=headers,
    )
    assert del_bound.status_code == 409, del_bound.text
    assert del_bound.json()["code"] == "CATALOG_VERSION_IN_USE"

    # Publish an unused version (e.g. future admission year 2030 with no students)
    unused_ver_resp = await client.post(
        f"{ADMIN_CATALOG}/curricula/BSCS/versions",
        headers=headers,
        json={
            "version": "2030-UNUSED",
            "effectiveAdmissionYear": 2030,
            "courses": [{"courseCode": "COMP 301", "yearLevel": 1, "semester": "1"}],
        },
    )
    assert unused_ver_resp.status_code == 201, unused_ver_resp.text
    unused_version_id = unused_ver_resp.json()["id"]

    del_unused = await client.delete(
        f"{ADMIN_CATALOG}/curricula/BSCS/versions/{unused_version_id}",
        headers=headers,
    )
    assert del_unused.status_code == 204, del_unused.text


async def test_ac_enrl_005_5_section_create_edit_and_capacity_below_enrolled(
    client: AsyncClient, db: AsyncSession, catalog_env
) -> None:
    """Section create (201), edit (200), and capacity below enrolled count (409)."""
    headers = catalog_env["chair_headers"]
    term: AcademicTerm = catalog_env["term"]
    faculty: User = catalog_env["faculty"]

    course = _course_row(
        code="COMP 202",
        title="Object-Oriented Programming",
        units=Decimal("3.0"),
        department=catalog_env["dcis"],
        college=catalog_env["ccis"],
    )
    db.add(course)
    await db.commit()

    create_resp = await client.post(
        f"{ADMIN_CATALOG}/sections",
        headers=headers,
        json={
            "academicTermId": str(term.id),
            "courseId": str(course.id),
            "facultyId": str(faculty.id),
            "sectionName": "BSCS 2-1",
            "room": "S501",
            "capacity": 40,
            "meetings": [
                {
                    "dayOfWeek": "Monday",
                    "startTime": "08:00:00",
                    "endTime": "10:00:00",
                    "room": "S501",
                },
                {
                    "dayOfWeek": "Wednesday",
                    "startTime": "08:00:00",
                    "endTime": "09:00:00",
                    "room": "S502-LAB",
                },
            ],
        },
    )
    assert create_resp.status_code == 201, create_resp.text
    section_body = create_resp.json()
    section_id = section_body["id"]
    assert section_body["sectionName"] == "BSCS 2-1"
    assert section_body["room"] == "S501"
    assert section_body["capacity"] == 40
    assert section_body["enrolled"] == 0
    assert section_body["seatsLeft"] == 40
    assert len(section_body["meetings"]) == 2

    # Enroll 3 students in this section
    for idx in range(1, 4):
        student_user, _ = await _make_student(
            db,
            email=f"sec.student{idx}@pup.edu.ph",
            student_number=f"2025-0010{idx}-MN-0",
        )
        te = TermEnrollment(
            student_id=student_user.id,
            academic_term_id=term.id,
            enrollment_status="enrolled",
        )
        db.add(te)
        await db.flush()
        db.add(
            StudentCourseEnrollment(
                term_enrollment_id=te.id,
                academic_term_id=term.id,
                section_id=uuid.UUID(section_id),
                status="enrolled",
            )
        )
    await db.commit()

    # Editing capacity to 2 (< 3 enrolled) returns 409 SECTION_CAPACITY_BELOW_ENROLLED
    low_cap_resp = await client.patch(
        f"{ADMIN_CATALOG}/sections/{section_id}",
        headers=headers,
        json={"capacity": 2},
    )
    assert low_cap_resp.status_code == 409, low_cap_resp.text
    low_cap_body = low_cap_resp.json()
    assert low_cap_body["code"] == "SECTION_CAPACITY_BELOW_ENROLLED"
    assert low_cap_body["details"]["enrolled"] == 3
    assert "3" in low_cap_body["message"]

    # Editing capacity to 35 (>= 3 enrolled) and updating room/meetings returns 200
    edit_resp = await client.patch(
        f"{ADMIN_CATALOG}/sections/{section_id}",
        headers=headers,
        json={
            "capacity": 35,
            "room": "S503",
            "meetings": [
                {
                    "dayOfWeek": "Tuesday",
                    "startTime": "10:00:00",
                    "endTime": "13:00:00",
                    "room": "S503",
                }
            ],
        },
    )
    assert edit_resp.status_code == 200, edit_resp.text
    edited = edit_resp.json()
    assert edited["capacity"] == 35
    assert edited["room"] == "S503"
    assert edited["enrolled"] == 3
    assert edited["seatsLeft"] == 32
    assert len(edited["meetings"]) == 1
    assert edited["meetings"][0]["dayOfWeek"] == 1


async def test_ac_enrl_005_6_substitution_approval_updates_named_students_progress_only(
    client: AsyncClient, db: AsyncSession, catalog_env
) -> None:
    """Substitute counts for named students' degree progress; other unchanged."""
    headers = catalog_env["chair_headers"]
    bscs: Program = catalog_env["bscs"]
    term: AcademicTerm = catalog_env["term"]

    orig_course = _course_row(
        code="COMP 401",
        title="Legacy Elective",
        units=Decimal("3.0"),
        department=catalog_env["dcis"],
        college=catalog_env["ccis"],
    )
    sub_course = _course_row(
        code="COMP 402",
        title="Modern AI Elective",
        units=Decimal("3.0"),
        department=catalog_env["dcis"],
        college=catalog_env["ccis"],
    )
    core_course = _course_row(
        code="COMP 400",
        title="Thesis Writing 1",
        units=Decimal("3.0"),
        department=catalog_env["dcis"],
        college=catalog_env["ccis"],
    )
    db.add_all([orig_course, sub_course, core_course])
    await db.flush()

    curr = Curriculum(
        program_id=bscs.id,
        curriculum_year="2024-SUB",
        effective_admission_year=2024,
        is_active=True,
    )
    db.add(curr)
    await db.flush()
    db.add_all(
        [
            CurriculumCourse(
                curriculum_id=curr.id,
                course_id=core_course.id,
                year_level=4,
                semester="1",
            ),
            CurriculumCourse(
                curriculum_id=curr.id,
                course_id=orig_course.id,
                year_level=4,
                semester="1",
            ),
        ]
    )
    sub_section = CourseSection(
        academic_term_id=term.id,
        course_id=sub_course.id,
        faculty_id=catalog_env["faculty"].id,
        section_name="BSCS 4-1",
        capacity=40,
    )
    db.add(sub_section)
    await db.commit()

    # Two students in the same curriculum, both completed sub_course ("COMP 402")
    named_student, _ = await _make_student(
        db,
        email="named.student@pup.edu.ph",
        student_number="2024-00001-MN-0",
        program_code="BSCS",
        year_level=4,
        curriculum_id=curr.id,
    )
    other_student, _ = await _make_student(
        db,
        email="other.student@pup.edu.ph",
        student_number="2024-00002-MN-0",
        program_code="BSCS",
        year_level=4,
        curriculum_id=curr.id,
    )

    for stu in (named_student, other_student):
        te = TermEnrollment(
            student_id=stu.id,
            academic_term_id=term.id,
            enrollment_status="enrolled",
        )
        db.add(te)
        await db.flush()
        sce = StudentCourseEnrollment(
            term_enrollment_id=te.id,
            academic_term_id=term.id,
            section_id=sub_section.id,
            status="enrolled",
        )
        db.add(sce)
        await db.flush()
        db.add(
            StudentGrade(
                enrollment_item_id=sce.id,
                final_grade="1.50",
                remarks="Passed",
                is_draft=False,
                is_locked=True,
            )
        )
    await db.commit()

    # Approve substitution of orig_course -> sub_course for named_student ONLY
    sub_resp = await client.post(
        f"{ADMIN_CATALOG}/substitutions",
        headers=headers,
        json={
            "originalCourseId": str(orig_course.id),
            "substituteCourseId": str(sub_course.id),
            "studentIds": [str(named_student.id)],
        },
    )
    assert sub_resp.status_code == 201, sub_resp.text
    sub_body = sub_resp.json()
    assert sub_body["originalCourseId"] == str(orig_course.id)
    assert sub_body["substituteCourseId"] == str(sub_course.id)
    assert sub_body["studentIds"] == [str(named_student.id)]

    # Check degree progress for named_student: COMP 401 satisfied via COMP 402
    named_prog = await client.get(
        f"{ADMIN_CATALOG}/students/{named_student.id}/degree-progress",
        headers=headers,
    )
    assert named_prog.status_code == 200, named_prog.text
    named_data = named_prog.json()
    assert float(named_data["completedUnits"]) == 3.0
    assert float(named_data["totalUnitsRequired"]) == 6.0
    by_code_named = {c["courseCode"]: c for c in named_data["courses"]}
    assert by_code_named["COMP 401"]["completed"] is True
    assert by_code_named["COMP 401"]["substitutedByCourseCode"] == "COMP 402"
    assert by_code_named["COMP 400"]["completed"] is False

    # Check degree progress for other_student: COMP 401 is NOT satisfied
    other_prog = await client.get(
        f"{ADMIN_CATALOG}/students/{other_student.id}/degree-progress",
        headers=headers,
    )
    assert other_prog.status_code == 200, other_prog.text
    other_data = other_prog.json()
    assert float(other_data["completedUnits"]) == 0.0
    by_code_other = {c["courseCode"]: c for c in other_data["courses"]}
    assert by_code_other["COMP 401"]["completed"] is False
    assert by_code_other["COMP 401"]["substitutedByCourseCode"] is None

    # Check degree progress scope enforcement: foreign department student returns 404
    foreign_student, _ = await _make_student(
        db,
        email="foreign.student@pup.edu.ph",
        student_number="2024-99999-MN-0",
        program_code="BSMATH",
        year_level=2,
    )
    foreign_prog = await client.get(
        f"{ADMIN_CATALOG}/students/{foreign_student.id}/degree-progress",
        headers=headers,
    )
    assert foreign_prog.status_code == 404


async def test_ac_enrl_005_7_chair_scope_courses_and_cross_department_404(
    client: AsyncClient, db: AsyncSession, catalog_env
) -> None:
    """Chair lists only own-department courses; PATCH on foreign course -> 404."""
    chair_headers = catalog_env["chair_headers"]

    own_course = _course_row(
        code="COMP 501",
        title="Compiler Design",
        units=Decimal("3.0"),
        department=catalog_env["dcis"],
        college=catalog_env["ccis"],
    )
    other_dept_course = _course_row(
        code="MATH 501",
        title="Real Analysis",
        units=Decimal("3.0"),
        department=catalog_env["dmath"],
        college=catalog_env["cs_college"],
    )
    db.add_all([own_course, other_dept_course])
    await db.commit()

    list_resp = await client.get(f"{ADMIN_CATALOG}/courses", headers=chair_headers)
    assert list_resp.status_code == 200, list_resp.text
    listed_codes = [c["code"] for c in list_resp.json()["items"]]
    assert "COMP 501" in listed_codes
    assert "MATH 501" not in listed_codes

    patch_foreign = await client.patch(
        f"{ADMIN_CATALOG}/courses/{other_dept_course.id}",
        headers=chair_headers,
        json={"title": "Unauthorized Edit"},
    )
    assert patch_foreign.status_code == 404, patch_foreign.text
    assert patch_foreign.json()["code"] == "RESOURCE_NOT_FOUND"

    create_foreign_college = await client.post(
        f"{ADMIN_CATALOG}/courses",
        headers=chair_headers,
        json={
            "courseCode": "COMP 599",
            "title": "Invalid College Course",
            "lectureUnits": 3,
            "labUnits": 0,
            "college": catalog_env["cs_college"],
        },
    )
    assert create_foreign_college.status_code == 422


async def test_ac_enrl_005_8_dean_registrar_and_student_scope_and_write_refusals(
    client: AsyncClient, db: AsyncSession, catalog_env
) -> None:
    """Dean lists own-college, Registrar lists all; non-Chair writes get 403."""
    ccis_course = _course_row(
        code="COMP 601",
        title="Cloud Computing",
        units=Decimal("3.0"),
        department=catalog_env["dcis"],
        college=catalog_env["ccis"],
    )
    cba_course = _course_row(
        code="ACCO 601",
        title="Financial Accounting",
        units=Decimal("3.0"),
        department=catalog_env["dacc"],
        college=catalog_env["cba"],
    )
    db.add_all([ccis_course, cba_course])
    await db.flush()
    section = CourseSection(
        academic_term_id=catalog_env["term"].id,
        course_id=ccis_course.id,
        faculty_id=catalog_env["faculty"].id,
        section_name="BSCS 3-1",
        capacity=40,
    )
    db.add(section)
    await db.commit()

    student_user, student_headers = await _make_student(
        db,
        email="scope.student@pup.edu.ph",
        student_number="2025-00201-MN-0",
    )

    # 1. Dean scope: GET shows only own-college courses (COMP 601, not ACCO 601)
    dean_headers = catalog_env["dean_headers"]
    dean_list = await client.get(f"{ADMIN_CATALOG}/courses", headers=dean_headers)
    assert dean_list.status_code == 200, dean_list.text
    dean_codes = [c["code"] for c in dean_list.json()["items"]]
    assert "COMP 601" in dean_codes
    assert "ACCO 601" not in dean_codes

    # 2. Registrar scope: GET shows every course across all colleges
    registrar_headers = catalog_env["registrar_headers"]
    reg_list = await client.get(f"{ADMIN_CATALOG}/courses", headers=registrar_headers)
    assert reg_list.status_code == 200, reg_list.text
    reg_codes = [c["code"] for c in reg_list.json()["items"]]
    assert "COMP 601" in reg_codes
    assert "ACCO 601" in reg_codes

    # 3. Student and Finance admin scope: GET on admin catalog route returns 403
    stu_list = await client.get(f"{ADMIN_CATALOG}/courses", headers=student_headers)
    assert stu_list.status_code == 403, stu_list.text
    assert stu_list.json()["code"] == "AUTH_INSUFFICIENT_ROLE"

    finance_headers = catalog_env["finance_headers"]
    fin_list = await client.get(f"{ADMIN_CATALOG}/courses", headers=finance_headers)
    assert fin_list.status_code == 403, fin_list.text
    assert fin_list.json()["code"] == "AUTH_INSUFFICIENT_ROLE"

    # 4. Every write returns 403 AUTH_INSUFFICIENT_ROLE for Dean, Registrar, Student
    write_calls = [
        (
            "POST",
            f"{ADMIN_CATALOG}/courses",
            {"code": "COMP 999", "title": "Blocked", "units": 3.0},
        ),
        (
            "PATCH",
            f"{ADMIN_CATALOG}/courses/{ccis_course.id}",
            {"title": "Blocked Edit"},
        ),
        (
            "POST",
            f"{ADMIN_CATALOG}/curricula/BSCS/versions",
            {
                "version": "2029",
                "effectiveAdmissionYear": 2029,
                "courses": [
                    {"courseCode": "COMP 601", "yearLevel": 1, "semester": "1"}
                ],
            },
        ),
        (
            "DELETE",
            f"{ADMIN_CATALOG}/curricula/BSCS/versions/{uuid.uuid4()}",
            None,
        ),
        (
            "POST",
            f"{ADMIN_CATALOG}/sections",
            {
                "academicTermId": str(catalog_env["term"].id),
                "courseId": str(ccis_course.id),
                "sectionName": "BSCS 1-9",
                "capacity": 30,
            },
        ),
        (
            "PATCH",
            f"{ADMIN_CATALOG}/sections/{section.id}",
            {"capacity": 35},
        ),
        (
            "POST",
            f"{ADMIN_CATALOG}/substitutions",
            {
                "originalCourseId": str(ccis_course.id),
                "substituteCourseId": str(cba_course.id),
                "studentIds": [str(student_user.id)],
            },
        ),
    ]

    for role_name, hdrs in [
        ("dean", dean_headers),
        ("registrar", registrar_headers),
        ("finance", finance_headers),
        ("student", student_headers),
    ]:
        for method, path, payload in write_calls:
            resp = await client.request(method, path, headers=hdrs, json=payload)
            assert resp.status_code == 403, f"{role_name} {method} {path}: {resp.text}"
            assert resp.json()["code"] == "AUTH_INSUFFICIENT_ROLE"


async def test_ac_enrl_005_9_each_write_adds_one_audit_row(
    client: AsyncClient, db: AsyncSession, catalog_env
) -> None:
    """Every admin catalog write adds one audit row with actor, old, new, time."""
    headers = catalog_env["chair_headers"]
    chair_id = catalog_env["chair_user"].id
    term: AcademicTerm = catalog_env["term"]
    faculty: User = catalog_env["faculty"]

    student_user, _ = await _make_student(
        db,
        email="audit.student@pup.edu.ph",
        student_number="2025-00301-MN-0",
    )

    async def _audit_rows() -> list[AuditEntry]:
        result = await db.scalars(
            select(AuditEntry)
            .where(AuditEntry.actor_id == chair_id)
            .order_by(AuditEntry.occurred_at.asc(), AuditEntry.id.asc())
        )
        return list(result.all())

    initial_count = len(await _audit_rows())

    # 1. Create course #1
    r1 = await client.post(
        f"{ADMIN_CATALOG}/courses",
        headers=headers,
        json={"code": "AUD 101", "title": "Audit Course 1", "units": 3.0},
    )
    assert r1.status_code == 201, r1.text
    course1_id = r1.json()["id"]
    rows = await _audit_rows()
    assert len(rows) == initial_count + 1
    assert not rows[-1].prior_state
    assert rows[-1].new_state is not None
    assert rows[-1].new_state["code"] == "AUD 101"
    assert rows[-1].occurred_at is not None

    # 2. Edit course #1
    r2 = await client.patch(
        f"{ADMIN_CATALOG}/courses/{course1_id}",
        headers=headers,
        json={"title": "Audit Course 1 Updated"},
    )
    assert r2.status_code == 200, r2.text
    rows = await _audit_rows()
    assert len(rows) == initial_count + 2
    assert rows[-1].prior_state is not None
    assert rows[-1].prior_state["title"] == "Audit Course 1"
    assert rows[-1].new_state is not None
    assert rows[-1].new_state["title"] == "Audit Course 1 Updated"
    assert rows[-1].occurred_at is not None

    # 3. Create course #2 (for substitution)
    r3 = await client.post(
        f"{ADMIN_CATALOG}/courses",
        headers=headers,
        json={"code": "AUD 102", "title": "Audit Course 2", "units": 3.0},
    )
    assert r3.status_code == 201, r3.text
    course2_id = r3.json()["id"]
    rows = await _audit_rows()
    assert len(rows) == initial_count + 3

    # 4. Publish curriculum version
    r4 = await client.post(
        f"{ADMIN_CATALOG}/curricula/BSCS/versions",
        headers=headers,
        json={
            "version": "2031-AUD",
            "effectiveAdmissionYear": 2031,
            "courses": [{"courseCode": "AUD 101", "yearLevel": 1, "semester": "1"}],
        },
    )
    assert r4.status_code == 201, r4.text
    version_id = r4.json()["id"]
    rows = await _audit_rows()
    assert len(rows) == initial_count + 4
    assert not rows[-1].prior_state
    assert rows[-1].new_state is not None
    assert rows[-1].new_state["version"] == "2031-AUD"

    # 5. Delete unused curriculum version
    r5 = await client.delete(
        f"{ADMIN_CATALOG}/curricula/BSCS/versions/{version_id}",
        headers=headers,
    )
    assert r5.status_code == 204, r5.text
    rows = await _audit_rows()
    assert len(rows) == initial_count + 5
    assert rows[-1].prior_state is not None
    assert rows[-1].prior_state["version"] == "2031-AUD"
    assert not rows[-1].new_state

    # 6. Create section
    r6 = await client.post(
        f"{ADMIN_CATALOG}/sections",
        headers=headers,
        json={
            "academicTermId": str(term.id),
            "courseId": course1_id,
            "facultyId": str(faculty.id),
            "sectionName": "BSCS AUD-1",
            "room": "E401",
            "capacity": 30,
        },
    )
    assert r6.status_code == 201, r6.text
    section_id = r6.json()["id"]
    rows = await _audit_rows()
    assert len(rows) == initial_count + 6
    assert not rows[-1].prior_state
    assert rows[-1].new_state is not None
    assert rows[-1].new_state["section_name"] == "BSCS AUD-1"

    # 7. Edit section
    r7 = await client.patch(
        f"{ADMIN_CATALOG}/sections/{section_id}",
        headers=headers,
        json={"capacity": 45, "room": "E402"},
    )
    assert r7.status_code == 200, r7.text
    rows = await _audit_rows()
    assert len(rows) == initial_count + 7
    assert rows[-1].prior_state is not None
    assert rows[-1].prior_state["capacity"] == 30
    assert rows[-1].new_state is not None
    assert rows[-1].new_state["capacity"] == 45

    # 8. Approve substitution
    r8 = await client.post(
        f"{ADMIN_CATALOG}/substitutions",
        headers=headers,
        json={
            "originalCourseId": course1_id,
            "substituteCourseId": course2_id,
            "studentIds": [str(student_user.id)],
        },
    )
    assert r8.status_code == 201, r8.text
    rows = await _audit_rows()
    assert len(rows) == initial_count + 8
    assert not rows[-1].prior_state
    assert rows[-1].new_state is not None
    assert rows[-1].new_state["original_course_id"] == course1_id
    assert rows[-1].new_state["substitute_course_id"] == course2_id
