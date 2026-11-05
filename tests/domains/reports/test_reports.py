"""Acceptance and integration tests for US-RPT-001 (#197 / #198).

Enrollment Statistics Analysis Report and File Export.
Covers AC-RPT-001.1 through AC-RPT-001.6, all rows in Responses table,
scope enforcement for Chair, Dean, Registrar, Finance, privacy guarantees,
export equality, and 15,000 enrollment query performance.
"""

from __future__ import annotations

import csv
import io
import time
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from io import BytesIO

import pytest
from httpx import AsyncClient
from pypdf import PdfReader
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_database.models.academics import AcademicTerm
from cbpupsis_database.models.curriculum import Program
from cbpupsis_database.models.enrollment import TermEnrollment
from cbpupsis_database.models.iam import (
    Group,
    Permission,
    Policy,
    PolicyPermission,
    UserGroup,
    UserPolicy,
)
from cbpupsis_database.models.users import (
    AdminPosition,
    AdminProfile,
    StudentProfile,
    User,
    UserProfile,
)
from cbpupsis_shared.domains.auth.constants import ROLE_ADMIN
from cbpupsis_shared.domains.auth.security import hash_password
from cbpupsis_shared.domains.auth.service.tokens import issue_role_session
from cbpupsis_shared.domains.iam.constants import ADMIN_GROUP
from cbpupsis_shared.domains.reports.constants import READ_ENROLLMENT_REPORT

REPORT_URL = "/api/v1/admin/reports/enrollment"
EXPORT_URL = "/api/v1/admin/reports/enrollment/export"
TEST_PASSWORD = "ReportPassword123!"


async def _create_admin(
    db: AsyncSession,
    *,
    email: str,
    position: str,
    department: str | None = None,
    college: str | None = None,
    grant_report_permission: bool = True,
) -> tuple[User, dict[str, str]]:
    """Create an admin account, profile, group, permission grant, and session token."""
    user = User(
        email=email,
        full_name=f"Admin {position.title()}",
        password_hash=hash_password(TEST_PASSWORD),
        is_active=True,
        email_verified_at=datetime.now(UTC),
    )
    db.add(user)
    await db.flush()

    profile = AdminProfile(
        user_id=user.id,
        position=position,
        department=department,
        college=college,
        is_active=True,
    )
    db.add(profile)

    group = await db.scalar(select(Group).where(Group.name == ADMIN_GROUP))
    if group is None:
        group = Group(name=ADMIN_GROUP)
        db.add(group)
        await db.flush()
    db.add(UserGroup(user_id=user.id, group_id=group.id))

    if grant_report_permission:
        perm = await db.scalar(
            select(Permission).where(Permission.action == READ_ENROLLMENT_REPORT)
        )
        if perm is None:
            perm = Permission(
                action=READ_ENROLLMENT_REPORT,
                description="Read enrollment counts and statistics report",
            )
            db.add(perm)
            await db.flush()

        pol = await db.scalar(
            select(Policy).where(Policy.name == "EnrollmentReportReader")
        )
        if pol is None:
            pol = Policy(name="EnrollmentReportReader")
            db.add(pol)
            await db.flush()
            db.add(PolicyPermission(policy_id=pol.id, permission_id=perm.id))

        db.add(UserPolicy(user_id=user.id, policy_id=pol.id))

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


async def _create_term(
    db: AsyncSession,
    academic_year: str = "2026-2027",
    semester: str = "FIRST_SEMESTER",
) -> AcademicTerm:
    term = AcademicTerm(
        academic_year=academic_year,
        semester=semester,
        start_date=date(2026, 8, 1),
        end_date=date(2026, 12, 15),
        regular_enrollment_start=datetime(2026, 7, 1, tzinfo=UTC),
        regular_enrollment_end=datetime(2026, 7, 31, tzinfo=UTC),
        add_drop_deadline=datetime(2026, 8, 15, tzinfo=UTC),
        grade_submission_deadline=datetime(2026, 12, 30, tzinfo=UTC),
        is_current=True,
    )
    db.add(term)
    await db.commit()
    await db.refresh(term)
    return term


async def _create_program(
    db: AsyncSession,
    *,
    code: str,
    name: str,
    department: str,
    college: str | None = None,
) -> Program:
    prog = Program(
        code=code,
        name=name,
        department=department,
        college=college,
        total_units_required=Decimal("140.0"),
        is_active=True,
    )
    db.add(prog)
    await db.commit()
    await db.refresh(prog)
    return prog


async def _create_student_enrollment(
    db: AsyncSession,
    *,
    term_id: uuid.UUID,
    program_id: uuid.UUID,
    year_level: int = 1,
    section: str = "1-1",
    gender: str = "Female",
    status: str = "enrolled",
    student_number_suffix: int = 1,
) -> tuple[User, StudentProfile, TermEnrollment]:
    user = User(
        email=f"student_{student_number_suffix}@example.com",
        full_name=f"Student User {student_number_suffix}",
        password_hash=hash_password(TEST_PASSWORD),
        is_active=True,
        email_verified_at=datetime.now(UTC),
    )
    db.add(user)
    await db.flush()

    s_num = f"2026-{student_number_suffix:05d}-MN-0"
    profile = StudentProfile(
        user_id=user.id,
        student_number=s_num,
        program_id=program_id,
        year_level=year_level,
        section=section,
        enrollment_status=status,
    )
    db.add(profile)

    user_profile = UserProfile(
        user_id=user.id,
        first_name="Student",
        last_name=f"User{student_number_suffix}",
        gender=gender,
    )
    db.add(user_profile)

    enrl = TermEnrollment(
        student_id=user.id,
        academic_term_id=term_id,
        enrollment_status=status,
    )
    db.add(enrl)
    await db.commit()
    return user, profile, enrl


# =========================================================================== #
# Acceptance Tests (AC-RPT-001.1 - AC-RPT-001.6) & Responses Table Tests     #
# =========================================================================== #


@pytest.mark.asyncio
async def test_ac_rpt_001_1_generate_counts_by_program_status_year_level_sex(
    client: AsyncClient, db: AsyncSession
) -> None:
    """AC-RPT-001.1: Counts by program, status, year level, and sex with totals."""
    term = await _create_term(db, "2026-2027", "1-AC1")
    prog_cs = await _create_program(
        db,
        code="BSCS-AC1",
        name="Computer Science",
        department="Computer Science",
        college="CCIS",
    )
    prog_it = await _create_program(
        db,
        code="BSIT-AC1",
        name="Information Technology",
        department="Information Technology",
        college="CCIS",
    )

    # Seed 3 CS students and 2 IT students
    await _create_student_enrollment(
        db,
        term_id=term.id,
        program_id=prog_cs.id,
        year_level=1,
        gender="Female",
        status="enrolled",
        student_number_suffix=101,
    )
    await _create_student_enrollment(
        db,
        term_id=term.id,
        program_id=prog_cs.id,
        year_level=2,
        gender="Male",
        status="enrolled",
        student_number_suffix=102,
    )
    await _create_student_enrollment(
        db,
        term_id=term.id,
        program_id=prog_cs.id,
        year_level=2,
        gender="Female",
        status="pending",
        student_number_suffix=103,
    )
    await _create_student_enrollment(
        db,
        term_id=term.id,
        program_id=prog_it.id,
        year_level=1,
        gender="Male",
        status="enrolled",
        student_number_suffix=104,
    )
    await _create_student_enrollment(
        db,
        term_id=term.id,
        program_id=prog_it.id,
        year_level=3,
        gender="Female",
        status="enrolled",
        student_number_suffix=105,
    )

    _, headers = await _create_admin(
        db, email="registrar_ac1@example.com", position=AdminPosition.registrar
    )

    # Group by program
    res = await client.get(
        f"{REPORT_URL}?termId={term.id}&groupBy=program", headers=headers
    )
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["total"] == 5
    groups = {r["group"]: r["count"] for r in data["rows"]}
    assert groups["BSCS-AC1"] == 3
    assert groups["BSIT-AC1"] == 2

    # Group by status
    res = await client.get(
        f"{REPORT_URL}?termId={term.id}&groupBy=status", headers=headers
    )
    assert res.status_code == 200
    data = res.json()
    assert data["total"] == 5
    groups = {r["group"]: r["count"] for r in data["rows"]}
    assert groups["enrolled"] == 4
    assert groups["pending"] == 1

    # Group by yearLevel
    res = await client.get(
        f"{REPORT_URL}?termId={term.id}&groupBy=yearLevel", headers=headers
    )
    assert res.status_code == 200
    data = res.json()
    assert data["total"] == 5
    groups = {r["group"]: r["count"] for r in data["rows"]}
    assert groups["1"] == 2
    assert groups["2"] == 2
    assert groups["3"] == 1

    # Group by sex
    res = await client.get(
        f"{REPORT_URL}?termId={term.id}&groupBy=sex", headers=headers
    )
    assert res.status_code == 200
    data = res.json()
    assert data["total"] == 5
    groups = {r["group"]: r["count"] for r in data["rows"]}
    assert groups["Female"] == 3
    assert groups["Male"] == 2


@pytest.mark.asyncio
async def test_ac_rpt_001_2_drill_down_to_year_level_and_section(
    client: AsyncClient, db: AsyncSession
) -> None:
    """AC-RPT-001.2: Drill down from program to year level and section."""
    term = await _create_term(db, "2026-2027", "1-AC2")
    prog_cs = await _create_program(
        db,
        code="BSCS-AC2",
        name="Computer Science",
        department="Computer Science",
        college="CCIS",
    )
    prog_it = await _create_program(
        db,
        code="BSIT-AC2",
        name="Information Technology",
        department="Information Technology",
        college="CCIS",
    )

    await _create_student_enrollment(
        db,
        term_id=term.id,
        program_id=prog_cs.id,
        year_level=1,
        section="BSCS 1-1",
        student_number_suffix=201,
    )
    await _create_student_enrollment(
        db,
        term_id=term.id,
        program_id=prog_cs.id,
        year_level=1,
        section="BSCS 1-1",
        student_number_suffix=202,
    )
    await _create_student_enrollment(
        db,
        term_id=term.id,
        program_id=prog_cs.id,
        year_level=2,
        section="BSCS 2-1",
        student_number_suffix=203,
    )
    await _create_student_enrollment(
        db,
        term_id=term.id,
        program_id=prog_it.id,
        year_level=1,
        section="BSIT 1-1",
        student_number_suffix=204,
    )

    _, headers = await _create_admin(
        db, email="registrar_ac2@example.com", position=AdminPosition.registrar
    )

    # Drill down: BSCS by yearLevel
    res = await client.get(
        f"{REPORT_URL}?termId={term.id}&programCode=BSCS-AC2&groupBy=yearLevel",
        headers=headers,
    )
    assert res.status_code == 200
    data = res.json()
    assert data["total"] == 3
    groups = {r["group"]: r["count"] for r in data["rows"]}
    assert groups["1"] == 2
    assert groups["2"] == 1

    # Drill down: BSCS by section
    res = await client.get(
        f"{REPORT_URL}?termId={term.id}&programCode=BSCS-AC2&groupBy=section",
        headers=headers,
    )
    assert res.status_code == 200
    data = res.json()
    assert data["total"] == 3
    groups = {r["group"]: r["count"] for r in data["rows"]}
    assert groups["BSCS 1-1"] == 2
    assert groups["BSCS 2-1"] == 1


@pytest.mark.asyncio
async def test_ac_rpt_001_3_export_numbers_match_report_numbers(
    client: AsyncClient, db: AsyncSession
) -> None:
    """AC-RPT-001.3: Export to CSV and PDF returns the exact numbers shown on screen."""
    term = await _create_term(db, "2026-2027", "1-AC3")
    prog = await _create_program(
        db,
        code="BSCS-AC3",
        name="CS",
        department="Computer Science",
        college="CCIS",
    )
    await _create_student_enrollment(
        db,
        term_id=term.id,
        program_id=prog.id,
        year_level=1,
        student_number_suffix=301,
    )
    await _create_student_enrollment(
        db,
        term_id=term.id,
        program_id=prog.id,
        year_level=2,
        student_number_suffix=302,
    )

    _, headers = await _create_admin(
        db, email="registrar_ac3@example.com", position=AdminPosition.registrar
    )

    # 1. Fetch JSON report numbers
    rep_res = await client.get(
        f"{REPORT_URL}?termId={term.id}&groupBy=yearLevel", headers=headers
    )
    assert rep_res.status_code == 200
    rep_data = rep_res.json()
    expected_rows = {r["group"]: r["count"] for r in rep_data["rows"]}
    expected_total = rep_data["total"]

    # 2. Export to CSV and compare numbers
    csv_res = await client.get(
        f"{EXPORT_URL}?termId={term.id}&groupBy=yearLevel&format=csv",
        headers=headers,
    )
    assert csv_res.status_code == 200
    assert "text/csv" in csv_res.headers["content-type"]
    reader = csv.reader(io.StringIO(csv_res.text))
    csv_rows = list(reader)
    # Format: [["group", "count"], ["1", "1"], ["2", "1"], ["Total", "2"]]
    assert csv_rows[0] == ["group", "count"]
    csv_dict = {row[0]: int(row[1]) for row in csv_rows[1:-1]}
    assert csv_dict == expected_rows
    assert int(csv_rows[-1][1]) == expected_total

    # 3. Export to PDF and compare numbers
    pdf_res = await client.get(
        f"{EXPORT_URL}?termId={term.id}&groupBy=yearLevel&format=pdf",
        headers=headers,
    )
    assert pdf_res.status_code == 200
    assert "application/pdf" in pdf_res.headers["content-type"]
    assert pdf_res.content.startswith(b"%PDF")

    pdf_reader = PdfReader(BytesIO(pdf_res.content))
    extracted_text = "".join(page.extract_text() for page in pdf_reader.pages)
    for group_name, count in expected_rows.items():
        assert group_name in extracted_text
        assert str(count) in extracted_text
    assert str(expected_total) in extracted_text


@pytest.mark.asyncio
async def test_ac_rpt_001_4_scope_chair_dean_registrar_finance(
    client: AsyncClient, db: AsyncSession
) -> None:
    """AC-RPT-001.4: Scopes on one seeded term with two colleges.

    - Chair: Only own-department students
    - Dean: Only own-college students
    - Registrar: Every student
    - Finance: 403 AUTH_INSUFFICIENT_ROLE
    """
    term = await _create_term(db, "2026-2027", "1-AC4")

    # College 1: CCIS (2 departments: Computer Science, Information Technology)
    prog_cs = await _create_program(
        db,
        code="BSCS-AC4",
        name="CS",
        department="Computer Science",
        college="CCIS",
    )
    prog_it = await _create_program(
        db,
        code="BSIT-AC4",
        name="IT",
        department="Information Technology",
        college="CCIS",
    )

    # College 2: COE (1 department: Civil Engineering)
    prog_ce = await _create_program(
        db,
        code="BSCE-AC4",
        name="CE",
        department="Civil Engineering",
        college="COE",
    )

    # Seed students:
    # 5 in Computer Science (CCIS)
    for i in range(5):
        await _create_student_enrollment(
            db,
            term_id=term.id,
            program_id=prog_cs.id,
            student_number_suffix=410 + i,
        )
    # 3 in Information Technology (CCIS)
    for i in range(3):
        await _create_student_enrollment(
            db,
            term_id=term.id,
            program_id=prog_it.id,
            student_number_suffix=420 + i,
        )
    # 4 in Civil Engineering (COE)
    for i in range(4):
        await _create_student_enrollment(
            db,
            term_id=term.id,
            program_id=prog_ce.id,
            student_number_suffix=430 + i,
        )

    # 1. Department Chair: Computer Science
    _, chair_headers = await _create_admin(
        db,
        email="chair_cs@example.com",
        position=AdminPosition.chairperson,
        department="Computer Science",
        college="CCIS",
    )
    res_chair = await client.get(
        f"{REPORT_URL}?termId={term.id}&groupBy=program", headers=chair_headers
    )
    assert res_chair.status_code == 200
    chair_data = res_chair.json()
    assert chair_data["total"] == 5
    assert len(chair_data["rows"]) == 1
    assert chair_data["rows"][0]["group"] == "BSCS-AC4"
    assert chair_data["rows"][0]["count"] == 5

    # 2. College Dean: CCIS
    _, dean_headers = await _create_admin(
        db,
        email="dean_ccis@example.com",
        position=AdminPosition.dean,
        department=None,
        college="CCIS",
    )
    res_dean = await client.get(
        f"{REPORT_URL}?termId={term.id}&groupBy=program", headers=dean_headers
    )
    assert res_dean.status_code == 200
    dean_data = res_dean.json()
    assert dean_data["total"] == 8  # 5 CS + 3 IT
    dean_groups = {r["group"]: r["count"] for r in dean_data["rows"]}
    assert dean_groups == {"BSCS-AC4": 5, "BSIT-AC4": 3}
    assert "BSCE-AC4" not in dean_groups

    # 3. Registrar: Institution-wide
    _, reg_headers = await _create_admin(
        db, email="registrar_all@example.com", position=AdminPosition.registrar
    )
    res_reg = await client.get(
        f"{REPORT_URL}?termId={term.id}&groupBy=program", headers=reg_headers
    )
    assert res_reg.status_code == 200
    reg_data = res_reg.json()
    assert reg_data["total"] == 12  # 5 CS + 3 IT + 4 CE
    reg_groups = {r["group"]: r["count"] for r in reg_data["rows"]}
    assert reg_groups == {"BSCS-AC4": 5, "BSIT-AC4": 3, "BSCE-AC4": 4}

    # 4. Finance session: Forbidden 403
    _, fin_headers = await _create_admin(
        db,
        email="finance_user@example.com",
        position=AdminPosition.finance,
        grant_report_permission=False,
    )
    res_fin = await client.get(
        f"{REPORT_URL}?termId={term.id}&groupBy=program", headers=fin_headers
    )
    assert res_fin.status_code == 403
    assert res_fin.json()["code"] == "AUTH_INSUFFICIENT_ROLE"

    res_fin_export = await client.get(
        f"{EXPORT_URL}?termId={term.id}&groupBy=program", headers=fin_headers
    )
    assert res_fin_export.status_code == 403
    assert res_fin_export.json()["code"] == "AUTH_INSUFFICIENT_ROLE"


@pytest.mark.asyncio
async def test_ac_rpt_001_5_filter_matches_no_students(
    client: AsyncClient, db: AsyncSession
) -> None:
    """AC-RPT-001.5: Filter matching no students returns empty state, no error."""
    term = await _create_term(db, "2026-2027", "1-AC5")
    prog = await _create_program(
        db,
        code="BSCS-AC5",
        name="CS",
        department="Computer Science",
        college="CCIS",
    )
    await _create_student_enrollment(
        db,
        term_id=term.id,
        program_id=prog.id,
        status="enrolled",
        student_number_suffix=501,
    )

    _, headers = await _create_admin(
        db, email="registrar_ac5@example.com", position=AdminPosition.registrar
    )

    # Filter with non-matching programCode
    res = await client.get(
        f"{REPORT_URL}?termId={term.id}&programCode=NONEXISTENT",
        headers=headers,
    )
    assert res.status_code == 200
    data = res.json()
    assert data["rows"] == []
    assert data["total"] == 0

    # Filter with non-matching status
    res_status = await client.get(
        f"{REPORT_URL}?termId={term.id}&status=withdrawn",
        headers=headers,
    )
    assert res_status.status_code == 200
    data_status = res_status.json()
    assert data_status["rows"] == []
    assert data_status["total"] == 0


@pytest.mark.asyncio
async def test_ac_rpt_001_6_no_names_student_numbers_emails_in_any_response(
    client: AsyncClient, db: AsyncSession
) -> None:
    """AC-RPT-001.6: Assert no name, student number, or email in any response."""
    term = await _create_term(db, "2026-2027", "1-AC6")
    prog = await _create_program(
        db,
        code="BSCS-AC6",
        name="CS",
        department="Computer Science",
        college="CCIS",
    )

    sensitive_first = "SecretStudentFirstName"
    sensitive_last = "ConfidentialLastName"
    sensitive_email = "very_identifiable_student_601@example.com"
    sensitive_student_number = "2026-99999-MN-0"

    user = User(
        email=sensitive_email,
        full_name=f"{sensitive_first} {sensitive_last}",
        password_hash=hash_password(TEST_PASSWORD),
        is_active=True,
        email_verified_at=datetime.now(UTC),
    )
    db.add(user)
    await db.flush()

    db.add(
        StudentProfile(
            user_id=user.id,
            student_number=sensitive_student_number,
            program_id=prog.id,
            year_level=1,
            section="BSCS 1-1",
            enrollment_status="enrolled",
        )
    )
    db.add(
        UserProfile(
            user_id=user.id,
            first_name=sensitive_first,
            last_name=sensitive_last,
            gender="Female",
        )
    )
    db.add(
        TermEnrollment(
            student_id=user.id,
            academic_term_id=term.id,
            enrollment_status="enrolled",
        )
    )
    await db.commit()

    _, headers = await _create_admin(
        db, email="registrar_ac6@example.com", position=AdminPosition.registrar
    )

    sensitive_tokens = [
        sensitive_first,
        sensitive_last,
        sensitive_email,
        sensitive_student_number,
    ]

    # Check JSON responses for all groupBys
    for group_by in ("program", "status", "yearLevel", "sex", "section"):
        res = await client.get(
            f"{REPORT_URL}?termId={term.id}&groupBy={group_by}", headers=headers
        )
        assert res.status_code == 200
        content_str = res.text
        for token in sensitive_tokens:
            assert token not in content_str

    # Check CSV export
    csv_res = await client.get(
        f"{EXPORT_URL}?termId={term.id}&groupBy=program&format=csv",
        headers=headers,
    )
    assert csv_res.status_code == 200
    csv_text = csv_res.text
    for token in sensitive_tokens:
        assert token not in csv_text

    # Check PDF export
    pdf_res = await client.get(
        f"{EXPORT_URL}?termId={term.id}&groupBy=program&format=pdf",
        headers=headers,
    )
    assert pdf_res.status_code == 200
    pdf_reader = PdfReader(BytesIO(pdf_res.content))
    pdf_text = "".join(page.extract_text() for page in pdf_reader.pages)
    for token in sensitive_tokens:
        assert token not in pdf_text


# =========================================================================== #
# Responses Table Error Scenarios                                             #
# =========================================================================== #


@pytest.mark.asyncio
async def test_ac_rpt_001_responses_unknown_term_id_404(
    client: AsyncClient, db: AsyncSession
) -> None:
    """Responses table: Unknown termId returns 404 RESOURCE_NOT_FOUND."""
    _, headers = await _create_admin(
        db, email="registrar_err1@example.com", position=AdminPosition.registrar
    )

    # Unknown UUID
    unknown_uuid = uuid.uuid4()
    res = await client.get(f"{REPORT_URL}?termId={unknown_uuid}", headers=headers)
    assert res.status_code == 404
    assert res.json()["code"] == "RESOURCE_NOT_FOUND"

    # Malformed termId string
    res_malformed = await client.get(
        f"{REPORT_URL}?termId=not-a-valid-uuid", headers=headers
    )
    assert res_malformed.status_code == 404
    assert res_malformed.json()["code"] == "RESOURCE_NOT_FOUND"

    # Missing termId
    res_missing = await client.get(REPORT_URL, headers=headers)
    assert res_missing.status_code == 404
    assert res_missing.json()["code"] == "RESOURCE_NOT_FOUND"


@pytest.mark.asyncio
async def test_ac_rpt_001_responses_unknown_group_by_400(
    client: AsyncClient, db: AsyncSession
) -> None:
    """Responses table: Unknown groupBy returns 400 INVALID_INPUT."""
    term = await _create_term(db, "2026-2027", "1-ERR2")
    _, headers = await _create_admin(
        db, email="registrar_err2@example.com", position=AdminPosition.registrar
    )

    res = await client.get(
        f"{REPORT_URL}?termId={term.id}&groupBy=unknown_field", headers=headers
    )
    assert res.status_code == 400
    assert res.json()["code"] == "INVALID_INPUT"

    # Export with unknown groupBy also returns 400
    res_export = await client.get(
        f"{EXPORT_URL}?termId={term.id}&groupBy=invalid", headers=headers
    )
    assert res_export.status_code == 400
    assert res_export.json()["code"] == "INVALID_INPUT"


# =========================================================================== #
# Performance Benchmark: 15,000 Enrollments under 3 Seconds                   #
# =========================================================================== #


@pytest.mark.asyncio
async def test_ac_rpt_001_performance_15000_enrollments_under_3_seconds(
    client: AsyncClient, db: AsyncSession
) -> None:
    """Quality target: 15,000 seeded enrollments returns in at most 3 seconds in CI."""
    term = await _create_term(db, "2026-2027", "1-PERF")
    prog1 = await _create_program(
        db,
        code="BSCS-PERF",
        name="CS",
        department="Computer Science",
        college="CCIS",
    )
    prog2 = await _create_program(
        db,
        code="BSIT-PERF",
        name="IT",
        department="Information Technology",
        college="CCIS",
    )
    prog3 = await _create_program(
        db,
        code="BSCE-PERF",
        name="CE",
        department="Civil Engineering",
        college="COE",
    )

    total_records = 15000
    programs = [prog1.id, prog2.id, prog3.id]

    # Fast bulk insertion of 15,000 users, student_profiles, user_profiles,
    # and term_enrollments
    user_rows = []
    student_profile_rows = []
    user_profile_rows = []
    enrollment_rows = []

    now = datetime.now(UTC)
    for i in range(total_records):
        u_id = uuid.uuid4()
        user_rows.append(
            {
                "id": u_id,
                "email": f"bulk_student_{i}@example.com",
                "password_hash": "hash",
                "is_active": True,
                "session_version": 0,
                "created_at": now,
                "updated_at": now,
            }
        )
        student_profile_rows.append(
            {
                "user_id": u_id,
                "student_number": f"2026-{i:06d}-MN-0",
                "program_id": programs[i % 3],
                "year_level": (i % 4) + 1,
                "section": f"SEC-{(i % 10) + 1}",
                "enrollment_status": "enrolled" if i % 10 != 0 else "pending",
            }
        )
        user_profile_rows.append(
            {
                "user_id": u_id,
                "first_name": "Bulk",
                "last_name": f"Student{i}",
                "gender": "Female" if i % 2 == 0 else "Male",
                "updated_at": now,
            }
        )
        enrollment_rows.append(
            {
                "id": uuid.uuid4(),
                "student_id": u_id,
                "academic_term_id": term.id,
                "enrollment_status": "enrolled" if i % 10 != 0 else "pending",
                "enrolled_at": now,
            }
        )

    # Execute in chunks for SQLite batch limit safety
    chunk_size = 2000
    for offset in range(0, total_records, chunk_size):
        await db.execute(insert(User), user_rows[offset : offset + chunk_size])
        await db.execute(
            insert(StudentProfile), student_profile_rows[offset : offset + chunk_size]
        )
        await db.execute(
            insert(UserProfile), user_profile_rows[offset : offset + chunk_size]
        )
        await db.execute(
            insert(TermEnrollment), enrollment_rows[offset : offset + chunk_size]
        )
    await db.commit()

    _, headers = await _create_admin(
        db, email="registrar_perf@example.com", position=AdminPosition.registrar
    )

    # Benchmark report generation over 15,000 records
    start = time.perf_counter()
    res = await client.get(
        f"{REPORT_URL}?termId={term.id}&groupBy=program", headers=headers
    )
    duration = time.perf_counter() - start

    assert res.status_code == 200, res.text
    data = res.json()
    assert data["total"] == total_records
    assert len(data["rows"]) == 3
    # Quality target: under 3 seconds in CI
    assert duration < 3.0, f"Query took {duration:.3f}s, expected < 3.0s"
