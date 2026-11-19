"""#146 evidence through merged finalize and correction HTTP workflows.

These tests use synthetic SQLite metadata, not a migrated/deployed database.
The merged fr004 revision and validation results are documented in README.md.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from io import BytesIO
from zoneinfo import ZoneInfo

import pytest
from pypdf import PdfWriter
from sqlalchemy import select

from cbpupsis_database.models.academics import AcademicTerm
from cbpupsis_database.models.curriculum import Course
from cbpupsis_database.models.enrollment import StudentCourseEnrollment, TermEnrollment
from cbpupsis_database.models.grading import StudentGrade
from cbpupsis_database.models.sections import CourseSection
from cbpupsis_database.models.users import AdminProfile, User
from cbpupsis_shared.domains.auth.security import hash_password, totp_at
from cbpupsis_shared.domains.iam import service as iam
from cbpupsis_shared.domains.iam.constants import ADMIN_GROUP
from tests.domains.student_grades.paths import STUDENT_RECORDS

PASSWORD = "Synthetic-Correction-Admin-123!"


@pytest.fixture
async def producer_records(db, student_account, faculty_account):
    now = datetime.now(UTC)
    today = now.astimezone(ZoneInfo("Asia/Manila")).date()
    term = AcademicTerm(
        academic_year="acceptance-completed",
        semester="First",
        term_type="REGULAR",
        start_date=today - timedelta(days=200),
        end_date=today - timedelta(days=30),
        is_current=True,
        regular_enrollment_start=now - timedelta(days=210),
        regular_enrollment_end=now - timedelta(days=200),
        add_drop_deadline=now - timedelta(days=180),
        grade_submission_deadline=now + timedelta(days=14),
    )
    # Corrections use the real next regular semester, not a mocked window.
    following = AcademicTerm(
        academic_year="acceptance-following",
        semester="Second",
        term_type="REGULAR",
        start_date=today + timedelta(days=30),
        end_date=today + timedelta(days=180),
        regular_enrollment_start=now + timedelta(days=15),
        regular_enrollment_end=now + timedelta(days=30),
        add_drop_deadline=now + timedelta(days=45),
        grade_submission_deadline=now + timedelta(days=190),
    )
    db.add_all([term, following])
    await db.flush()
    enrollment = TermEnrollment(student_id=student_account.id, academic_term_id=term.id)
    db.add(enrollment)
    await db.flush()
    records = {"student_id": student_account.id, "term_id": term.id}
    for code in ("ANCHOR146", "TARGET146"):
        course = Course(
            course_code=code,
            title=f"Synthetic {code}",
            lecture_units=Decimal("3.0"),
            lab_units=Decimal("0.0"),
            department="Computer Science",
            college="CCIS",
            is_nstp=False,
        )
        db.add(course)
        await db.flush()
        section = CourseSection(
            academic_term_id=term.id,
            course_id=course.id,
            faculty_id=faculty_account.id,
            section_name=code,
            capacity=40,
        )
        db.add(section)
        await db.flush()
        attempt = StudentCourseEnrollment(
            term_enrollment_id=enrollment.id,
            academic_term_id=term.id,
            section_id=section.id,
            status="enrolled",
        )
        db.add(attempt)
        await db.flush()
        records[code] = {"section_id": section.id, "enrollment_id": attempt.id}
    await db.commit()
    return records


async def save_draft(client, headers, record, mark):
    path = f"/api/v1/faculty/sections/{record['section_id']}/grades"
    current = await client.get(path, headers=headers)
    assert current.status_code == 200, current.text
    response = await client.put(
        f"{path}/draft",
        headers=headers,
        json={
            "version": current.json()["version"],
            "grades": [
                {"enrollmentId": str(record["enrollment_id"]), "finalGrade": mark}
            ],
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["version"]


async def finalize(client, headers, record, version):
    response = await client.post(
        f"/api/v1/faculty/sections/{record['section_id']}/grades/finalize",
        headers=headers,
        json={"version": version},
    )
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "FINAL"


async def read_record(client, headers, suffix="grades"):
    response = await client.get(f"{STUDENT_RECORDS}/{suffix}", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


async def test_ac_stu_004_3_real_finalize_creates_365_day_inc_deadline(
    client, db, student_headers, producer_headers, producer_records
):
    anchor = producer_records["ANCHOR146"]
    target = producer_records["TARGET146"]
    version = await save_draft(client, producer_headers, anchor, "1.00")
    await finalize(client, producer_headers, anchor, version)
    version = await save_draft(client, producer_headers, target, "INC")
    draft_read = await read_record(client, student_headers)
    assert [row["code"] for row in draft_read["courses"]] == ["ANCHOR146"]
    assert draft_read["gwa"] == "1.00"

    await finalize(client, producer_headers, target, version)
    record = await read_record(client, student_headers)
    inc = next(row for row in record["courses"] if row["code"] == "TARGET146")
    assert inc["final_grade"] == "Inc" and inc["status"] == "Incomplete"
    posted = datetime.fromisoformat(inc["posted_at"])
    deadline = datetime.fromisoformat(inc["completion_deadline"])
    assert deadline - posted == timedelta(days=365)
    assert record["gwa"] == "1.00"
    assert Decimal(record["units_earned"]) == Decimal("3.0")
    transcript = await read_record(client, student_headers, "transcript")
    assert transcript["cumulative_gwa"] == "1.00"
    assert Decimal(transcript["total_units_earned"]) == Decimal("3.0")
    stored = await db.scalar(
        select(StudentGrade).where(
            StudentGrade.enrollment_item_id == target["enrollment_id"]
        )
    )
    assert stored.is_locked and not stored.is_draft
    assert stored.final_grade == "INC"


@pytest.fixture
async def correction_approvers(client, db):
    await iam.create_permission(db, action="ApproveGradeCorrection")
    policy = await iam.create_policy(
        db,
        name="acceptance-correction-review",
        permission_actions=["ApproveGradeCorrection"],
    )
    group = await iam.create_group(db, name=ADMIN_GROUP)
    password_hash = hash_password(PASSWORD)
    headers = {}
    for position in ("chairperson", "dean", "registrar"):
        user = User(
            email=f"acceptance-{position}@example.com",
            password_hash=password_hash,
            email_verified_at=datetime.now(UTC),
        )
        db.add(user)
        await db.flush()
        db.add(
            AdminProfile(
                user_id=user.id,
                position=position,
                department="Computer Science",
                college="CCIS",
                is_active=True,
            )
        )
        await db.commit()
        await iam.add_user_to_group(db, user.id, group.id)
        await iam.attach_policy_to_user(db, user.id, policy.id)
        response = await client.post(
            "/api/v1/auth/admin/login", json={"email": user.email, "password": PASSWORD}
        )
        assert response.status_code == 200, response.text
        challenge = response.json()["challenge_token"]
        response = await client.post(
            "/api/v1/auth/admin/mfa/totp/enroll", json={"challenge_token": challenge}
        )
        assert response.status_code == 200, response.text
        code = totp_at(
            response.json()["secret"], int(datetime.now(UTC).timestamp()) // 30
        )
        response = await client.post(
            "/api/v1/auth/admin/mfa/totp/confirm",
            json={"challenge_token": challenge, "code": code},
        )
        assert response.status_code == 200, response.text
        headers[position] = {
            "Authorization": f"Bearer {response.json()['access_token']}"
        }
    return headers


async def test_ac_stu_004_7_real_correction_approval_updates_student_grade_and_gwa(
    client, student_headers, producer_headers, producer_records, correction_approvers
):
    for code, mark in (("ANCHOR146", "1.00"), ("TARGET146", "3.00")):
        record = producer_records[code]
        version = await save_draft(client, producer_headers, record, mark)
        await finalize(client, producer_headers, record, version)
    assert (await read_record(client, student_headers))["gwa"] == "2.00"

    sheet = BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.write(sheet)
    target = producer_records["TARGET146"]
    response = await client.post(
        f"/api/v1/faculty/sections/{target['section_id']}/grade-corrections",
        headers=producer_headers,
        data={
            "student_id": str(producer_records["student_id"]),
            "proposed_mark": "1.50",
            "reason": "Synthetic corrected computation",
        },
        files={
            "grading_sheet": (
                "synthetic-sheet.pdf",
                sheet.getvalue(),
                "application/pdf",
            )
        },
    )
    assert response.status_code == 201, response.text
    correction = response.json()
    assert correction["state"] == "PENDING_CHAIR"
    for position, action, state in (
        ("chairperson", "ENDORSE", "PENDING_DEAN"),
        ("dean", "ENDORSE", "PENDING_REGISTRAR"),
        ("registrar", "COMMIT", "COMMITTED"),
    ):
        # Filing and endorsements must not expose an uncommitted proposed mark.
        assert (await read_record(client, student_headers))["gwa"] == "2.00"
        response = await client.patch(
            f"/api/v1/admin/grade-corrections/{correction['id']}",
            headers=correction_approvers[position],
            json={"action": action, "version": correction["version"]},
        )
        assert response.status_code == 200, response.text
        correction = response.json()
        assert correction["state"] == state

    record = await read_record(client, student_headers)
    target_course = next(row for row in record["courses"] if row["code"] == "TARGET146")
    assert target_course["final_grade"] == "1.50"
    assert record["gwa"] == "1.25"
    transcript = await read_record(client, student_headers, "transcript")
    assert transcript["terms"][0]["gwa"] == "1.25"
    assert transcript["cumulative_gwa"] == "1.25"
    assert Decimal(transcript["total_units_earned"]) == Decimal("6.0")
