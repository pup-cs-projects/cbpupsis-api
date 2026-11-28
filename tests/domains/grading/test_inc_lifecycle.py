"""Incomplete (INC) lifecycle tests (US-FAC-001: AC-FAC-001.3 through AC-FAC-001.8)."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_core.config import settings
from cbpupsis_database.models.academics import AcademicTerm
from cbpupsis_database.models.audit import AuditEntry
from cbpupsis_database.models.curriculum import Course, Program
from cbpupsis_database.models.enrollment import StudentCourseEnrollment, TermEnrollment
from cbpupsis_database.models.grading import (
    IncExpiryRun,
    StudentGrade,
    StudentTermGPA,
)
from cbpupsis_database.models.iam import Group, Permission
from cbpupsis_database.models.outbox import OutboxMessage
from cbpupsis_database.models.sections import CourseSection
from cbpupsis_database.models.users import (
    AdminProfile,
    FacultyProfile,
    StudentProfile,
    User,
    UserProfile,
)
from cbpupsis_shared.domains.auth import service as auth_service
from cbpupsis_shared.domains.auth.security import hash_password, totp_at
from cbpupsis_shared.domains.grading.client import expire_overdue_incompletes
from cbpupsis_shared.domains.grading.constants import (
    CREATE_INCOMPLETE_COMPLETION,
    INC_COMPLETION_DECIDED_EVENT,
    INC_COMPLETION_ENDORSED_ACTION,
    INC_COMPLETION_FILED_ACTION,
    INC_EXPIRED_ACTION,
    INC_EXPIRED_EVENT,
    READ_GRADE,
    SUBMIT_GRADE,
    UPDATE_GRADE,
)
from cbpupsis_shared.domains.iam import service as iam_service
from cbpupsis_shared.domains.iam.constants import ADMIN_GROUP
from cbpupsis_shared.domains.notifications.templates import render
from cbpupsis_shared.domains.sections.constants import READ_COURSE_SECTION
from scripts.expire_incompletes import default_run_key
from scripts.seed_iam import seed as seed_iam

FACULTY_AUTH = "/api/v1/auth/faculty"
STUDENT_AUTH = "/api/v1/auth/student"
FACULTY_PASSWORD = "Faculty-Inc-Password-123!"
STUDENT_PASSWORD = "Student-Inc-Password-123!"
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
    db.add(FacultyProfile(user_id=user.id, employee_id=employee_id, department="CS"))
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
    password_hash: str,
) -> User:
    parts = name.split(" ", 1)
    user = User(
        email=email,
        password_hash=password_hash,
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


async def _create_admin(
    db: AsyncSession,
    *,
    position: str,
    department: str | None = None,
    college: str | None = None,
    grant_from_seed: bool = True,
) -> tuple[User, dict[str, str]]:
    if grant_from_seed:
        await seed_iam(db)
    user = User(
        email=f"{position}-{uuid.uuid4().hex[:8]}@pup.edu.ph",
        password_hash="not-used",
        full_name=f"{position.title()} User",
        is_active=True,
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
    await db.flush()
    admin_group = await db.scalar(select(Group).where(Group.name == ADMIN_GROUP))
    if admin_group is not None:
        await iam_service.add_user_to_group(db, user.id, admin_group.id)
    if grant_from_seed:
        from cbpupsis_database.models.iam import Policy
        from cbpupsis_shared.domains.iam.constants import ADMIN_POSITION_POLICIES

        for policy_name in ADMIN_POSITION_POLICIES.get(position, ()):
            policy = await db.scalar(select(Policy).where(Policy.name == policy_name))
            if policy is not None:
                await iam_service.attach_policy_to_user(db, user.id, policy.id)
    await db.commit()
    pair = await auth_service.issue_role_session(
        db,
        user.id,
        role="admin",
        mfa=True,
        claims={
            "position": position,
            "department_id": department,
            "college_id": college,
        },
        ip_address="127.0.0.1",
    )
    return user, {"Authorization": f"Bearer {pair.access_token}"}


async def _grant_extra(db: AsyncSession, user_id: uuid.UUID, action: str) -> None:
    perm = await db.scalar(select(Permission).where(Permission.action == action))
    if perm is None:
        await iam_service.create_permission(db, action=action)
    suffix = uuid.uuid4().hex[:8]
    policy = await iam_service.create_policy(
        db, name=f"policy-{action}-{suffix}", permission_actions=[action]
    )
    group = await iam_service.create_group(db, name=f"group-{action}-{suffix}")
    await iam_service.attach_policy_to_group(db, group.id, policy.id)
    await iam_service.add_user_to_group(db, user_id, group.id)
    await db.commit()


async def _enroll(
    db: AsyncSession,
    *,
    student_id: uuid.UUID,
    term_id: uuid.UUID,
    section_id: uuid.UUID,
    term_enrollment: TermEnrollment | None = None,
) -> tuple[TermEnrollment, StudentCourseEnrollment]:
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
    return term_enrollment, item


@pytest.fixture
async def inc_setup(db: AsyncSession, grant):
    faculty = await _faculty(db, "2021-00951-MN-0", "inc.faculty@pup.edu.ph")
    other_faculty = await _faculty(db, "2021-00952-MN-0", "other.inc@pup.edu.ph")
    for perm in (
        READ_COURSE_SECTION,
        UPDATE_GRADE,
        SUBMIT_GRADE,
        CREATE_INCOMPLETE_COMPLETION,
    ):
        await grant(faculty.id, perm)
    await _grant_extra(db, other_faculty.id, READ_COURSE_SECTION)
    await _grant_extra(db, other_faculty.id, CREATE_INCOMPLETE_COMPLETION)

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
    ccis_program = Program(
        code="BSCS",
        name="Bachelor of Science in Computer Science",
        department="Computer Science",
        college="CCIS",
        total_units_required=Decimal("150.0"),
    )
    coe_program = Program(
        code="BSCE",
        name="Bachelor of Science in Civil Engineering",
        department="Civil Engineering",
        college="COE",
        total_units_required=Decimal("150.0"),
    )
    course_a = Course(
        course_code="COMP 301",
        title="Algorithms and Complexity",
        lecture_units=Decimal("3.0"),
        lab_units=Decimal("0.0"),
        is_nstp=False,
    )
    course_b = Course(
        course_code="COMP 302",
        title="Operating Systems",
        lecture_units=Decimal("3.0"),
        lab_units=Decimal("0.0"),
        is_nstp=False,
    )
    db.add_all([term, ccis_program, coe_program, course_a, course_b])
    await db.flush()

    section_a = CourseSection(
        academic_term_id=term.id,
        course_id=course_a.id,
        faculty_id=faculty.id,
        section_name="BSCS 3-1",
        capacity=40,
    )
    section_b = CourseSection(
        academic_term_id=term.id,
        course_id=course_b.id,
        faculty_id=faculty.id,
        section_name="BSCS 3-2",
        capacity=40,
    )
    foreign_section = CourseSection(
        academic_term_id=term.id,
        course_id=course_a.id,
        faculty_id=other_faculty.id,
        section_name="BSCS 3-9",
        capacity=40,
    )
    db.add_all([section_a, section_b, foreign_section])
    await db.flush()

    pw_hash = hash_password(STUDENT_PASSWORD)
    alice = await _create_student(
        db,
        program_id=ccis_program.id,
        number="2024-00201-MN-0",
        name="Alice Inc",
        email="alice.inc@iskolarngbayan.pup.edu.ph",
        password_hash=pw_hash,
    )
    bob = await _create_student(
        db,
        program_id=coe_program.id,
        number="2024-00202-MN-0",
        name="Bob Eng",
        email="bob.eng@iskolarngbayan.pup.edu.ph",
        password_hash=pw_hash,
    )
    await _grant_extra(db, alice.id, READ_GRADE)
    await _grant_extra(db, bob.id, READ_GRADE)

    alice_te, alice_enr_a = await _enroll(
        db, student_id=alice.id, term_id=term.id, section_id=section_a.id
    )
    _, alice_enr_b = await _enroll(
        db,
        student_id=alice.id,
        term_id=term.id,
        section_id=section_b.id,
        term_enrollment=alice_te,
    )
    bob_te, bob_enr_a = await _enroll(
        db, student_id=bob.id, term_id=term.id, section_id=section_a.id
    )
    _, foreign_enr = await _enroll(
        db,
        student_id=bob.id,
        term_id=term.id,
        section_id=foreign_section.id,
        term_enrollment=bob_te,
    )
    await db.commit()

    return {
        "faculty_id": faculty.id,
        "other_faculty_id": other_faculty.id,
        "term_id": term.id,
        "section_a_id": section_a.id,
        "section_b_id": section_b.id,
        "foreign_section_id": foreign_section.id,
        "alice_id": alice.id,
        "bob_id": bob.id,
        "alice_enr_a_id": alice_enr_a.id,
        "alice_enr_b_id": alice_enr_b.id,
        "bob_enr_a_id": bob_enr_a.id,
        "foreign_enr_id": foreign_enr.id,
    }


async def test_ac_fac_001_3_draft_with_inc_sets_deadline_and_setting_override(
    client: AsyncClient, inc_setup, monkeypatch
) -> None:
    setup = inc_setup
    headers = await _faculty_headers(client, "2021-00951-MN-0")
    section_id = setup["section_a_id"]

    # Default setting (365 days), accepting "mark": "INC".
    draft_resp = await client.put(
        f"/api/v1/faculty/sections/{section_id}/grades/draft",
        headers=headers,
        json={
            "version": 1,
            "grades": [
                {"enrollmentId": str(setup["alice_enr_a_id"]), "mark": "INC"},
                {"enrollmentId": str(setup["bob_enr_a_id"]), "finalGrade": "1.50"},
            ],
        },
    )
    assert draft_resp.status_code == 200, draft_resp.text
    rows = {g["studentNumber"]: g for g in draft_resp.json()["grades"]}
    alice_row = rows["2024-00201-MN-0"]
    bob_row = rows["2024-00202-MN-0"]
    assert alice_row["finalGrade"] == "INC"
    assert alice_row["completionDeadline"] is not None
    assert bob_row["completionDeadline"] is None

    deadline_default = datetime.fromisoformat(alice_row["completionDeadline"])
    now = datetime.now(UTC)
    assert (
        timedelta(days=364, hours=23)
        <= (deadline_default - now)
        <= timedelta(days=365, minutes=1)
    )

    # Change the 365-day setting to 120 days and save another draft -> new deadline.
    monkeypatch.setattr(settings, "inc_completion_days", 120)
    custom_draft = await client.put(
        f"/api/v1/faculty/sections/{section_id}/grades/draft",
        headers=headers,
        json={
            "version": 2,
            "grades": [
                {"enrollmentId": str(setup["alice_enr_a_id"]), "mark": "INC"},
            ],
        },
    )
    assert custom_draft.status_code == 200, custom_draft.text
    custom_rows = {g["studentNumber"]: g for g in custom_draft.json()["grades"]}
    deadline_custom = datetime.fromisoformat(
        custom_rows["2024-00201-MN-0"]["completionDeadline"]
    )
    assert (
        timedelta(days=119, hours=23)
        <= (deadline_custom - datetime.now(UTC))
        <= timedelta(days=120, minutes=1)
    )


async def test_draft_inc_accepts_a_chosen_deadline_within_one_year(
    client: AsyncClient, inc_setup
) -> None:
    setup = inc_setup
    headers = await _faculty_headers(client, "2021-00951-MN-0")
    section_id = setup["section_a_id"]
    url = f"/api/v1/faculty/sections/{section_id}/grades/draft"
    chosen = (datetime.now(UTC) + timedelta(days=90)).date()

    resp = await client.put(
        url,
        headers=headers,
        json={
            "version": 1,
            "grades": [
                {
                    "enrollmentId": str(setup["alice_enr_a_id"]),
                    "mark": "INC",
                    "completionDeadline": chosen.isoformat(),
                }
            ],
        },
    )
    assert resp.status_code == 200, resp.text
    row = {g["studentNumber"]: g for g in resp.json()["grades"]}["2024-00201-MN-0"]
    deadline = datetime.fromisoformat(row["completionDeadline"])
    assert timedelta(days=89) <= deadline - datetime.now(UTC) <= timedelta(days=91)

    too_late = (datetime.now(UTC) + timedelta(days=400)).date()
    rejected = await client.put(
        url,
        headers=headers,
        json={
            "version": 2,
            "grades": [
                {
                    "enrollmentId": str(setup["alice_enr_a_id"]),
                    "mark": "INC",
                    "completionDeadline": too_late.isoformat(),
                }
            ],
        },
    )
    assert rejected.status_code == 422, rejected.text


async def test_ac_fac_001_4_student_record_excludes_inc_from_gwa_and_lists_incs(
    client: AsyncClient, inc_setup
) -> None:
    setup = inc_setup
    headers = await _faculty_headers(client, "2021-00951-MN-0")

    # Section A: Alice gets 1.50 (3 units), Bob gets INC.
    await client.put(
        f"/api/v1/faculty/sections/{setup['section_a_id']}/grades/draft",
        headers=headers,
        json={
            "version": 1,
            "grades": [
                {"enrollmentId": str(setup["alice_enr_a_id"]), "finalGrade": "1.50"},
                {"enrollmentId": str(setup["bob_enr_a_id"]), "mark": "INC"},
            ],
        },
    )
    fin_a = await client.post(
        f"/api/v1/faculty/sections/{setup['section_a_id']}/grades/finalize",
        headers=headers,
    )
    assert fin_a.status_code == 200

    # Section B: Alice gets INC (3 units).
    await client.put(
        f"/api/v1/faculty/sections/{setup['section_b_id']}/grades/draft",
        headers=headers,
        json={
            "version": 1,
            "grades": [
                {"enrollmentId": str(setup["alice_enr_b_id"]), "mark": "INC"},
            ],
        },
    )
    fin_b = await client.post(
        f"/api/v1/faculty/sections/{setup['section_b_id']}/grades/finalize",
        headers=headers,
    )
    assert fin_b.status_code == 200

    # Student record shows Inc + deadline and leaves Inc out of GWA/earned units.
    alice_headers = await _student_headers(client, "2024-00201-MN-0")
    alice_grades = (
        await client.get("/api/v1/student/grades", headers=alice_headers)
    ).json()
    assert alice_grades["gwa"] == "1.50"
    assert Decimal(alice_grades["units_earned"]) == Decimal("3.0")
    by_code = {c["code"]: c for c in alice_grades["courses"]}
    assert by_code["COMP 301"]["final_grade"] == "1.50"
    assert by_code["COMP 302"]["final_grade"] == "Inc"
    assert by_code["COMP 302"]["status"] == "Incomplete"
    assert by_code["COMP 302"]["completion_deadline"] is not None

    # Section Inc list (Section A has Bob).
    sec_list = await client.get(
        f"/api/v1/faculty/sections/{setup['section_a_id']}/incompletes",
        headers=headers,
    )
    assert sec_list.status_code == 200, sec_list.text
    sec_body = sec_list.json()
    assert sec_body["total"] == 1
    assert sec_body["items"][0]["studentNumber"] == "2024-00202-MN-0"
    assert sec_body["items"][0]["state"] == "PENDING_COMPLETION"
    assert sec_body["items"][0]["daysLeft"] == 365
    assert sec_body["items"][0]["completionDeadline"] is not None

    # Foreign section Inc list returns 404.
    foreign_resp = await client.get(
        f"/api/v1/faculty/sections/{setup['foreign_section_id']}/incompletes",
        headers=headers,
    )
    assert foreign_resp.status_code == 404
    assert foreign_resp.json()["code"] == "RESOURCE_NOT_FOUND"

    # Faculty-wide Inc list has both Bob (COMP 301) and Alice (COMP 302).
    all_list = await client.get("/api/v1/faculty/incompletes", headers=headers)
    assert all_list.status_code == 200, all_list.text
    all_body = all_list.json()
    assert all_body["total"] == 2
    assert {item["courseCode"] for item in all_body["items"]} == {
        "COMP 301",
        "COMP 302",
    }
    assert all(item["state"] == "PENDING_COMPLETION" for item in all_body["items"])


async def test_ac_fac_001_5_completion_validation_and_dean_endorse_recomputes_gwa(
    client: AsyncClient, db: AsyncSession, inc_setup
) -> None:
    setup = inc_setup
    headers = await _faculty_headers(client, "2021-00951-MN-0")

    # Setup Section A: Alice 1.50; Section B: Alice INC.
    await client.put(
        f"/api/v1/faculty/sections/{setup['section_a_id']}/grades/draft",
        headers=headers,
        json={
            "version": 1,
            "grades": [
                {"enrollmentId": str(setup["alice_enr_a_id"]), "finalGrade": "1.50"},
                {"enrollmentId": str(setup["bob_enr_a_id"]), "finalGrade": "2.00"},
            ],
        },
    )
    assert (
        await client.post(
            f"/api/v1/faculty/sections/{setup['section_a_id']}/grades/finalize",
            headers=headers,
        )
    ).status_code == 200

    await client.put(
        f"/api/v1/faculty/sections/{setup['section_b_id']}/grades/draft",
        headers=headers,
        json={
            "version": 1,
            "grades": [
                {"enrollmentId": str(setup["alice_enr_b_id"]), "mark": "INC"},
            ],
        },
    )
    assert (
        await client.post(
            f"/api/v1/faculty/sections/{setup['section_b_id']}/grades/finalize",
            headers=headers,
        )
    ).status_code == 200

    inc_list = (await client.get("/api/v1/faculty/incompletes", headers=headers)).json()
    alice_inc_id = inc_list["items"][0]["incId"]

    # 422 on invalid proposedMark or missing form.
    bad_form = await client.post(
        f"/api/v1/faculty/incompletes/{alice_inc_id}/completion",
        headers=headers,
        json={"proposedMark": "1.25", "completionFormFileId": "   "},
    )
    assert bad_form.status_code == 422
    assert bad_form.json()["code"] == "VALIDATION_ERROR"

    for bad_mark in ("5.00", "3.25", "0.75", "INC"):
        bad_grade = await client.post(
            f"/api/v1/faculty/incompletes/{alice_inc_id}/completion",
            headers=headers,
            json={"proposedMark": bad_mark, "completionFormFileId": "form-001"},
        )
        assert bad_grade.status_code == 422, bad_grade.text
        assert bad_grade.json()["code"] == "VALIDATION_ERROR"

    # Reject without remarks -> 422, then reject with remarks -> 200.
    filed = await client.post(
        f"/api/v1/faculty/incompletes/{alice_inc_id}/completion",
        headers=headers,
        json={
            "proposedMark": "2.00",
            "completionFormFileId": "form-inc-101",
            "remarks": "Submitted defense paper",
        },
    )
    assert filed.status_code == 201, filed.text
    assert filed.json()["state"] == "PENDING_DEAN"
    first_comp_id = filed.json()["completionId"]

    # CCIS Dean tries reject without remarks -> 422 DECISION_REMARKS_REQUIRED.
    _, ccis_dean_headers = await _create_admin(db, position="dean", college="CCIS")
    no_remarks = await client.patch(
        f"/api/v1/admin/incomplete-completions/{first_comp_id}",
        headers=ccis_dean_headers,
        json={"decision": "REJECT", "remarks": "   "},
    )
    assert no_remarks.status_code == 422
    assert no_remarks.json()["code"] == "DECISION_REMARKS_REQUIRED"

    rejected = await client.patch(
        f"/api/v1/admin/incomplete-completions/{first_comp_id}",
        headers=ccis_dean_headers,
        json={"decision": "REJECT", "remarks": "Missing signature on page 2"},
    )
    assert rejected.status_code == 200
    assert rejected.json()["state"] == "REJECTED"

    # Re-file after rejection -> 201 PENDING_DEAN.
    refiled = await client.post(
        f"/api/v1/faculty/incompletes/{alice_inc_id}/completion",
        headers=headers,
        json={
            "proposedMark": "1.25",
            "completionFormFileId": "form-inc-102",
            "remarks": "Signed completion sheet",
        },
    )
    assert refiled.status_code == 201, refiled.text
    second_comp_id = refiled.json()["completionId"]

    # Endorse -> 200, mark becomes 1.25, deadline clears, GWA -> (1.5+1.25)/2 = 1.38.
    endorsed = await client.patch(
        f"/api/v1/admin/incomplete-completions/{second_comp_id}",
        headers=ccis_dean_headers,
        json={"decision": "ENDORSE", "remarks": "Verified and endorsed"},
    )
    assert endorsed.status_code == 200, endorsed.text
    assert endorsed.json()["state"] == "ENDORSED"
    assert endorsed.json()["completionDeadline"] is None

    alice_headers = await _student_headers(client, "2024-00201-MN-0")
    alice_grades = (
        await client.get("/api/v1/student/grades", headers=alice_headers)
    ).json()
    assert alice_grades["gwa"] == "1.38"
    assert Decimal(alice_grades["units_earned"]) == Decimal("6.0")
    comp_302 = next(c for c in alice_grades["courses"] if c["code"] == "COMP 302")
    assert comp_302["final_grade"] == "1.25"
    assert comp_302["status"] == "Passed"
    assert comp_302["completion_deadline"] is None

    # Faculty list now shows COMPLETED with cleared deadline.
    after_list = (
        await client.get(
            f"/api/v1/faculty/sections/{setup['section_b_id']}/incompletes",
            headers=headers,
        )
    ).json()
    assert after_list["items"][0]["state"] == "COMPLETED"
    assert after_list["items"][0]["completionDeadline"] is None

    # Audits + outbox notifications staged.
    filed_audits = list(
        await db.scalars(
            select(AuditEntry).where(AuditEntry.action == INC_COMPLETION_FILED_ACTION)
        )
    )
    endorsed_audits = list(
        await db.scalars(
            select(AuditEntry).where(
                AuditEntry.action == INC_COMPLETION_ENDORSED_ACTION
            )
        )
    )
    assert len(filed_audits) == 2
    assert len(endorsed_audits) == 1
    outbox = list(
        await db.scalars(
            select(OutboxMessage).where(
                OutboxMessage.event_name == INC_COMPLETION_DECIDED_EVENT
            )
        )
    )
    assert len(outbox) >= 2
    rendered = render(
        type(
            "E",
            (),
            {"name": INC_COMPLETION_DECIDED_EVENT, "payload": outbox[-1].payload},
        )()
    )
    assert "COMP 302" in rendered.title


async def test_ac_fac_001_5_admin_role_and_college_scope_and_version_conflict(
    client: AsyncClient, db: AsyncSession, inc_setup
) -> None:
    setup = inc_setup
    headers = await _faculty_headers(client, "2021-00951-MN-0")

    # Section A has Alice (CCIS) and Bob (COE). Both get INC.
    await client.put(
        f"/api/v1/faculty/sections/{setup['section_a_id']}/grades/draft",
        headers=headers,
        json={
            "version": 1,
            "grades": [
                {"enrollmentId": str(setup["alice_enr_a_id"]), "mark": "INC"},
                {"enrollmentId": str(setup["bob_enr_a_id"]), "mark": "INC"},
            ],
        },
    )
    assert (
        await client.post(
            f"/api/v1/faculty/sections/{setup['section_a_id']}/grades/finalize",
            headers=headers,
        )
    ).status_code == 200

    inc_rows = (
        await client.get("/api/v1/faculty/incompletes", headers=headers)
    ).json()["items"]
    by_student = {row["studentNumber"]: row["incId"] for row in inc_rows}

    alice_comp = (
        await client.post(
            f"/api/v1/faculty/incompletes/{by_student['2024-00201-MN-0']}/completion",
            headers=headers,
            json={"proposedMark": "1.75", "completionFormFileId": "form-ccis"},
        )
    ).json()
    bob_comp = (
        await client.post(
            f"/api/v1/faculty/incompletes/{by_student['2024-00202-MN-0']}/completion",
            headers=headers,
            json={"proposedMark": "2.25", "completionFormFileId": "form-coe"},
        )
    ).json()

    _, ccis_dean_headers = await _create_admin(db, position="dean", college="CCIS")
    _, chair_headers = await _create_admin(
        db, position="chairperson", department="Computer Science", grant_from_seed=False
    )
    _, registrar_headers = await _create_admin(
        db, position="registrar", grant_from_seed=False
    )
    _, finance_headers = await _create_admin(
        db, position="finance", grant_from_seed=False
    )

    # CCIS Dean sees ONLY Alice's CCIS completion.
    ccis_list = await client.get(
        "/api/v1/admin/incomplete-completions", headers=ccis_dean_headers
    )
    assert ccis_list.status_code == 200
    assert ccis_list.json()["total"] == 1
    assert ccis_list.json()["items"][0]["completionId"] == alice_comp["completionId"]

    # CCIS Dean gets 404 RESOURCE_NOT_FOUND for COE completion.
    cross_college = await client.patch(
        f"/api/v1/admin/incomplete-completions/{bob_comp['completionId']}",
        headers=ccis_dean_headers,
        json={"decision": "ENDORSE"},
    )
    assert cross_college.status_code == 404
    assert cross_college.json()["code"] == "RESOURCE_NOT_FOUND"

    # Version conflict returns 409 VERSION_CONFLICT.
    stale_ver = await client.patch(
        f"/api/v1/admin/incomplete-completions/{alice_comp['completionId']}",
        headers=ccis_dean_headers,
        json={"decision": "ENDORSE", "version": 99},
    )
    assert stale_ver.status_code == 409
    assert stale_ver.json()["code"] == "VERSION_CONFLICT"

    # Chair, Registrar, Finance sessions receive 403 (missing permission).
    for non_dean_headers in (chair_headers, registrar_headers, finance_headers):
        list_res = await client.get(
            "/api/v1/admin/incomplete-completions", headers=non_dean_headers
        )
        patch_res = await client.patch(
            f"/api/v1/admin/incomplete-completions/{alice_comp['completionId']}",
            headers=non_dean_headers,
            json={"decision": "ENDORSE"},
        )
        assert list_res.status_code == 403
        assert patch_res.status_code == 403


async def test_ac_fac_001_6_daily_job_expires_overdue_incs_and_audits_system(
    client: AsyncClient, db: AsyncSession, inc_setup
) -> None:
    setup = inc_setup
    headers = await _faculty_headers(client, "2021-00951-MN-0")

    # Section A: Alice 1.50; Bob INC. Section B: Alice INC.
    await client.put(
        f"/api/v1/faculty/sections/{setup['section_a_id']}/grades/draft",
        headers=headers,
        json={
            "version": 1,
            "grades": [
                {"enrollmentId": str(setup["alice_enr_a_id"]), "finalGrade": "1.50"},
                {"enrollmentId": str(setup["bob_enr_a_id"]), "mark": "INC"},
            ],
        },
    )
    assert (
        await client.post(
            f"/api/v1/faculty/sections/{setup['section_a_id']}/grades/finalize",
            headers=headers,
        )
    ).status_code == 200

    await client.put(
        f"/api/v1/faculty/sections/{setup['section_b_id']}/grades/draft",
        headers=headers,
        json={
            "version": 1,
            "grades": [
                {"enrollmentId": str(setup["alice_enr_b_id"]), "mark": "INC"},
            ],
        },
    )
    assert (
        await client.post(
            f"/api/v1/faculty/sections/{setup['section_b_id']}/grades/finalize",
            headers=headers,
        )
    ).status_code == 200

    # Expire Alice's COMP 302 INC past its 365-day deadline, keep Bob's unexpired.
    alice_grade = await db.scalar(
        select(StudentGrade).where(
            StudentGrade.enrollment_item_id == setup["alice_enr_b_id"]
        )
    )
    assert alice_grade is not None
    run_at = alice_grade.inc_expires_at + timedelta(hours=1)
    # Keep Bob's deadline 10 days after run_at so it should NOT expire yet.
    bob_grade = await db.scalar(
        select(StudentGrade).where(
            StudentGrade.enrollment_item_id == setup["bob_enr_a_id"]
        )
    )
    assert bob_grade is not None
    bob_grade.inc_expires_at = run_at + timedelta(days=10)
    await db.commit()

    run_key = default_run_key(run_at)
    assert run_key.startswith("inc-expiry-")
    result = await expire_overdue_incompletes(db, run_key=run_key, as_of=run_at)
    assert result.converted == 1
    assert result.skipped_duplicate_run is False

    await db.refresh(alice_grade)
    await db.refresh(bob_grade)
    assert alice_grade.final_grade == "5.00"
    assert alice_grade.completion_status == "EXPIRED"
    assert bob_grade.final_grade == "INC"

    # Alice has 1.50 (3 units) + 5.00 (3 units) -> 50% failed -> Probation, GWA 3.25.
    standing = await db.scalar(
        select(StudentTermGPA).where(
            StudentTermGPA.student_id == setup["alice_id"],
            StudentTermGPA.academic_term_id == setup["term_id"],
        )
    )
    assert standing is not None
    assert standing.term_gpa == Decimal("3.25")
    assert standing.academic_standing == "Probation"

    # Audit row written with actor 'system' (actor_id None + actor payload).
    audits = list(
        await db.scalars(
            select(AuditEntry).where(AuditEntry.action == INC_EXPIRED_ACTION)
        )
    )
    assert len(audits) == 1
    assert audits[0].actor_id is None
    assert audits[0].payload["actor"] == "system"
    assert audits[0].payload["runKey"] == run_key
    assert audits[0].new_state["finalGrade"] == "5.00"

    # Outbox notification queued.
    expired_events = list(
        await db.scalars(
            select(OutboxMessage).where(OutboxMessage.event_name == INC_EXPIRED_EVENT)
        )
    )
    assert len(expired_events) == 1


async def test_ac_fac_001_7_completion_after_expiry_returns_409_and_keeps_500(
    client: AsyncClient, db: AsyncSession, inc_setup
) -> None:
    setup = inc_setup
    headers = await _faculty_headers(client, "2021-00951-MN-0")

    await client.put(
        f"/api/v1/faculty/sections/{setup['section_b_id']}/grades/draft",
        headers=headers,
        json={
            "version": 1,
            "grades": [
                {"enrollmentId": str(setup["alice_enr_b_id"]), "mark": "INC"},
            ],
        },
    )
    assert (
        await client.post(
            f"/api/v1/faculty/sections/{setup['section_b_id']}/grades/finalize",
            headers=headers,
        )
    ).status_code == 200

    alice_grade = await db.scalar(
        select(StudentGrade).where(
            StudentGrade.enrollment_item_id == setup["alice_enr_b_id"]
        )
    )
    assert alice_grade is not None
    run_at = alice_grade.inc_expires_at + timedelta(days=1)
    await expire_overdue_incompletes(db, as_of=run_at)

    # Filing a completion now returns 409 INC_COMPLETION_WINDOW_CLOSED; mark stays 5.00.
    refused = await client.post(
        f"/api/v1/faculty/incompletes/{alice_grade.id}/completion",
        headers=headers,
        json={"proposedMark": "1.25", "completionFormFileId": "form-late"},
    )
    assert refused.status_code == 409, refused.text
    assert refused.json()["code"] == "INC_COMPLETION_WINDOW_CLOSED"

    await db.refresh(alice_grade)
    assert alice_grade.final_grade == "5.00"


async def test_ac_fac_001_8_daily_job_run_twice_same_day_is_idempotent(
    client: AsyncClient, db: AsyncSession, inc_setup
) -> None:
    setup = inc_setup
    headers = await _faculty_headers(client, "2021-00951-MN-0")

    await client.put(
        f"/api/v1/faculty/sections/{setup['section_b_id']}/grades/draft",
        headers=headers,
        json={
            "version": 1,
            "grades": [
                {"enrollmentId": str(setup["alice_enr_b_id"]), "mark": "INC"},
            ],
        },
    )
    assert (
        await client.post(
            f"/api/v1/faculty/sections/{setup['section_b_id']}/grades/finalize",
            headers=headers,
        )
    ).status_code == 200

    alice_grade = await db.scalar(
        select(StudentGrade).where(
            StudentGrade.enrollment_item_id == setup["alice_enr_b_id"]
        )
    )
    assert alice_grade is not None
    run_at = alice_grade.inc_expires_at + timedelta(days=2)
    run_key = default_run_key(run_at)

    first = await expire_overdue_incompletes(db, run_key=run_key, as_of=run_at)
    second = await expire_overdue_incompletes(db, run_key=run_key, as_of=run_at)
    next_day = await expire_overdue_incompletes(
        db, run_key="inc-expiry-next-day-check", as_of=run_at + timedelta(days=1)
    )

    assert first.converted == 1
    assert first.skipped_duplicate_run is False
    assert second.converted == 0
    assert second.skipped_duplicate_run is True
    assert next_day.converted == 0

    audits = list(
        await db.scalars(
            select(AuditEntry).where(AuditEntry.action == INC_EXPIRED_ACTION)
        )
    )
    assert len(audits) == 1
    runs = list(
        await db.scalars(select(IncExpiryRun).where(IncExpiryRun.run_key == run_key))
    )
    assert len(runs) == 1
