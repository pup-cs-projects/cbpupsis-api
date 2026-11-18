"""Issue #103 acceptance evidence using the real #177 producer and shared math."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from io import BytesIO

import pytest
from PIL import Image
from pypdf import PdfWriter
from sqlalchemy import func, select

from cbpupsis_api_admin.domains.admin_auth.dependencies import (
    AdminSession,
    require_admin_session,
)
from cbpupsis_api_faculty.domains.faculty_auth.dependencies import (
    require_faculty_session,
)
from cbpupsis_core.config import settings
from cbpupsis_database.models.academics import AcademicTerm
from cbpupsis_database.models.audit import AuditEntry
from cbpupsis_database.models.curriculum import Course
from cbpupsis_database.models.enrollment import StudentCourseEnrollment
from cbpupsis_database.models.grading import (
    GradeCorrection,
    StudentGrade,
    StudentTermGPA,
)
from cbpupsis_database.models.iam import Policy
from cbpupsis_database.models.sections import CourseSection
from cbpupsis_database.models.users import User
from cbpupsis_shared.domains.auth.dependencies import CurrentUser
from cbpupsis_shared.domains.grading import corrections
from cbpupsis_shared.domains.grading import service as grades
from cbpupsis_shared.domains.grading.attachments import (
    MAX_GRADING_SHEET_BYTES,
    inspect_grading_sheet,
)
from cbpupsis_shared.domains.grading.correction_schemas import CorrectionDecision
from cbpupsis_shared.domains.grading.schemas import (
    FinalizeGradesRequest,
    SaveGradeDraftRequest,
)
from cbpupsis_shared.domains.iam import service as iam
from main import app
from scripts.seed_iam import seed
from tests.domains.grading.test_faculty_grades import grading_setup as grading_setup

FACULTY = "/api/v1/faculty"
ADMIN = "/api/v1/admin/grade-corrections"
NOW = datetime(2027, 1, 16, 12, tzinfo=UTC)


def sheet(kind="PNG"):
    stream = BytesIO()
    if kind == "PDF":
        writer = PdfWriter()
        writer.add_blank_page(width=100, height=100)
        writer.write(stream)
    else:
        Image.new("RGB", (3, 3), "white").save(stream, format=kind)
    return stream.getvalue()


def term(year, semester, start, end, kind="REGULAR"):
    return AcademicTerm(
        academic_year=year,
        semester=semester,
        term_type=kind,
        start_date=start,
        end_date=end,
        regular_enrollment_start=NOW,
        regular_enrollment_end=NOW,
        add_drop_deadline=NOW,
        grade_submission_deadline=NOW + timedelta(days=30),
    )


@pytest.fixture
async def setup(db, grading_setup, monkeypatch):
    data = dict(grading_setup)
    await seed(db)
    course = await db.get(Course, data["course_id"])
    course.department, course.college = "Computer Science", "CCIS"
    db.add_all(
        [
            term(
                "2026-2027", "Summer", date(2026, 12, 16), date(2026, 12, 31), "SUMMER"
            ),
            term("2026-2027", "Second", date(2027, 1, 15), date(2027, 5, 31)),
            term("2027-2028", "First", date(2027, 8, 1), date(2027, 12, 15)),
        ]
    )
    # Execute the actual merged prerequisite, not a fake finalized database row.
    initial = await grades.get_section_grades(
        db, faculty_id=data["faculty_id"], section_id=data["own_id"]
    )
    draft = await grades.save_section_grade_draft(
        db,
        faculty_id=data["faculty_id"],
        section_id=data["own_id"],
        payload=SaveGradeDraftRequest(
            version=initial.version,
            grades=[
                {"enrollmentId": str(data["alice_enr_id"]), "finalGrade": "2.00"},
                {"enrollmentId": str(data["bob_enr_id"]), "finalGrade": "2.50"},
            ],
        ),
    )
    await grades.finalize_section_grades(
        db,
        faculty_id=data["faculty_id"],
        section_id=data["own_id"],
        payload=FinalizeGradesRequest(version=draft.version),
    )
    db.add(
        StudentTermGPA(
            student_id=data["alice_id"],
            academic_term_id=data["open_term_id"],
            total_gpa_units=Decimal("3"),
            term_gpa=Decimal("2"),
            cumulative_gpa=Decimal("2"),
            academic_standing="Warning Level 1",
        )
    )
    for position in ("chairperson", "dean", "registrar", "finance"):
        actor = User(
            email=f"correction-{position}@example.com", password_hash="!fixture"
        )
        db.add(actor)
        await db.flush()
        data[position] = actor.id
        if position != "finance":
            policy = await db.scalar(
                select(Policy).where(Policy.name == "GradeCorrectionAdmin")
            )
            await iam.attach_policy_to_user(db, actor.id, policy.id)
    author = await db.scalar(
        select(Policy).where(Policy.name == "GradeCorrectionAuthor")
    )
    await iam.attach_policy_to_user(db, data["faculty_id"], author.id)
    await db.commit()
    monkeypatch.setattr(corrections, "_now", lambda: NOW)
    return data


def faculty_override(data):
    app.dependency_overrides[require_faculty_session] = lambda: CurrentUser(
        id=data["faculty_id"],
        email="fixture@example.test",
        role="faculty",
        claims={"mfa": True},
    )
    # Permission dependencies use the same verified principal in these scope tests.
    from cbpupsis_shared.domains.auth.dependencies import get_current_user

    app.dependency_overrides[get_current_user] = app.dependency_overrides[
        require_faculty_session
    ]


def admin_override(data, position, department="Computer Science", college="CCIS"):
    app.dependency_overrides[require_admin_session] = lambda: AdminSession(
        id=data[position],
        email="fixture@example.test",
        position=position,
        department_id=department,
        college_id=college,
    )


async def file(db, data, **overrides):
    args = {
        "faculty_id": data["faculty_id"],
        "section_id": data["own_id"],
        "student_id": data["alice_id"],
        "proposed_mark": "1.25",
        "reason": "Synthetic correction reason",
        "content": sheet(),
        "client_ip": "192.0.2.10",
    }
    args.update(overrides)
    return await corrections.file_correction(db, **args)


async def decide(db, data, row, position, action="ENDORSE", **overrides):
    args = {
        "actor_id": data[position],
        "position": position,
        "department_id": "Computer Science",
        "college_id": "CCIS",
        "decision": CorrectionDecision(action=action, version=row.version),
        "client_ip": "192.0.2.11",
    }
    args.update(overrides)
    return await corrections.decide_correction(db, row.id, **args)


async def registrar_pending(db, data):
    row = await file(db, data)
    row = await decide(db, data, row, "chairperson")
    return await decide(db, data, row, "dean")


async def persisted(db, data):
    mark = await db.scalar(
        select(StudentGrade.final_grade).where(
            StudentGrade.enrollment_item_id == data["alice_enr_id"]
        )
    )
    gpa = (
        await db.execute(
            select(StudentTermGPA.term_gpa, StudentTermGPA.cumulative_gpa).where(
                StudentTermGPA.student_id == data["alice_id"]
            )
        )
    ).one()
    return mark, tuple(gpa)


async def test_ac_fac_002_1_file_pending_mark_unchanged(client, db, setup):
    faculty_override(setup)
    response = await client.post(
        f"{FACULTY}/sections/{setup['own_id']}/grade-corrections",
        data={
            "student_id": str(setup["alice_id"]),
            "proposed_mark": "1.25",
            "reason": "Synthetic evidence",
        },
        files={"grading_sheet": ("misleading.txt", sheet(), "text/plain")},
    )
    assert response.status_code == 201, response.text
    assert response.json()["state"] == "PENDING_CHAIR"
    assert response.json()["gradingSheetContentType"] == "image/png"
    assert await persisted(db, setup) == ("2.00", (Decimal("2"), Decimal("2")))
    assert response.headers["cache-control"] == "private, no-store"
    detail = await client.get(f"{FACULTY}/grade-corrections/{response.json()['id']}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["windowClosedAt"] == response.json()["windowClosedAt"]
    row = await db.get(GradeCorrection, uuid.UUID(response.json()["id"]))
    content = await db.scalar(
        select(GradeCorrection.grading_sheet_content).where(
            GradeCorrection.id == row.id
        )
    )
    assert content == sheet()


async def test_ac_fac_002_2_ordered_endorsements(db, setup):
    row = await file(db, setup)
    row = await decide(db, setup, row, "chairperson")
    assert (row.state, row.version) == ("PENDING_DEAN", 2)
    row = await decide(db, setup, row, "dean")
    assert (row.state, row.version) == ("PENDING_REGISTRAR", 3)
    assert await persisted(db, setup) == ("2.00", (Decimal("2"), Decimal("2")))


async def test_ac_fac_002_3_atomic_registrar_commit(db, setup):
    row = await registrar_pending(db, setup)
    row = await decide(db, setup, row, "registrar", "COMMIT")
    assert (row.state, row.version) == ("COMMITTED", 4)
    assert await persisted(db, setup) == ("1.25", (Decimal("1.25"), Decimal("1.25")))
    result = await grades.read_grades(db, student_id=setup["alice_id"])
    assert result.gwa == Decimal("1.25")
    assert result.academic_standing.value == "Warning Level 1"


@pytest.mark.parametrize("failure_point", ["gpa", "audit", "commit"])
async def test_ac_fac_002_3_commit_failure_rolls_back_mark_and_gwa(
    db, setup, monkeypatch, failure_point
):
    row = await registrar_pending(db, setup)
    before = await persisted(db, setup)
    if failure_point == "gpa":
        original = corrections._refresh_gpas

        async def fail(*args):
            await original(*args)
            raise RuntimeError("synthetic failure after GWA flush")

        monkeypatch.setattr(corrections, "_refresh_gpas", fail)
    elif failure_point == "audit":

        def fail(*args, **kwargs):
            raise RuntimeError("synthetic audit failure")

        monkeypatch.setattr(corrections, "_stage_audit", fail)
    else:

        async def fail():
            await db.flush()
            raise RuntimeError("synthetic transaction commit failure")

        monkeypatch.setattr(db, "commit", fail)
    with pytest.raises(RuntimeError):
        await decide(db, setup, row, "registrar", "COMMIT")
    assert await persisted(db, setup) == before
    saved = await corrections.read_correction(
        db, row.id, faculty_id=setup["faculty_id"]
    )
    assert (saved.state, saved.version) == ("PENDING_REGISTRAR", 3)
    assert (
        await db.scalar(
            select(func.count())
            .select_from(AuditEntry)
            .where(AuditEntry.target_id == str(row.id))
        )
        == 3
    )


async def test_ac_fac_002_4_regular_semester_boundary_and_setting(
    db, setup, monkeypatch
):
    row = await file(db, setup)
    end = row.window_closed_at
    assert end.astimezone(corrections.MANILA).date() == date(2027, 5, 31)
    monkeypatch.setattr(corrections, "_now", lambda: end)
    assert (await file(db, setup)).state == "PENDING_CHAIR"
    monkeypatch.setattr(corrections, "_now", lambda: end + timedelta(microseconds=1))
    from cbpupsis_shared.domains.grading.exceptions import CorrectionWindowClosedError

    with pytest.raises(CorrectionWindowClosedError) as error:
        await file(db, setup)
    assert error.value.response_fields["windowClosedAt"] == end
    monkeypatch.setattr(settings, "grade_correction_window_regular_semesters", 2)
    assert (await file(db, setup)).window_closed_at.astimezone(
        corrections.MANILA
    ).date() == date(2027, 12, 15)


async def test_ac_fac_002_4_closed_window_http(client, db, setup, monkeypatch):
    faculty_override(setup)
    monkeypatch.setattr(corrections, "_now", lambda: datetime(2027, 6, 1, tzinfo=UTC))
    response = await client.post(
        f"{FACULTY}/sections/{setup['own_id']}/grade-corrections",
        data={
            "student_id": str(setup["alice_id"]),
            "proposed_mark": "1.25",
            "reason": "Synthetic",
        },
        files={"grading_sheet": ("sheet.png", sheet(), "image/png")},
    )
    assert response.status_code == 403
    assert response.json()["code"] == "CORRECTION_WINDOW_CLOSED"
    assert "windowClosedAt" in response.json()


async def test_ac_fac_002_4_missing_calendar_refuses_without_fixed_day_fallback(
    db, setup
):
    from sqlalchemy import delete

    from cbpupsis_shared.domains.grading.exceptions import GradeRecordUnavailableError

    await db.execute(
        delete(AcademicTerm).where(AcademicTerm.start_date > date(2026, 12, 31))
    )
    await db.commit()
    with pytest.raises(GradeRecordUnavailableError):
        await file(db, setup)


async def test_ac_fac_002_5_filer_and_previous_actor_denied(db, setup):
    from cbpupsis_shared.domains.grading.exceptions import ApprovalSameActorError

    row = await file(db, setup)
    with pytest.raises(ApprovalSameActorError):
        await decide(db, setup, row, "chairperson", actor_id=setup["faculty_id"])
    row = await decide(db, setup, row, "chairperson")
    with pytest.raises(ApprovalSameActorError):
        await decide(db, setup, row, "dean", actor_id=setup["chairperson"])


@pytest.mark.parametrize(
    "position,action",
    [
        ("dean", "ENDORSE"),
        ("registrar", "COMMIT"),
        ("dean", "REJECT"),
        ("chairperson", "COMMIT"),
    ],
)
async def test_ac_fac_002_6_out_of_order_no_mutation(db, setup, position, action):
    from cbpupsis_shared.domains.grading.exceptions import ApprovalOutOfOrderError

    row = await file(db, setup)
    with pytest.raises(ApprovalOutOfOrderError):
        await decide(db, setup, row, position, action)
    saved = await corrections.read_correction(
        db, row.id, faculty_id=setup["faculty_id"]
    )
    assert (saved.state, saved.version) == ("PENDING_CHAIR", 1)
    assert await persisted(db, setup) == ("2.00", (Decimal("2"), Decimal("2")))


@pytest.mark.parametrize("position", ["chairperson", "dean", "registrar"])
async def test_ac_fac_002_7_current_step_rejects_and_filer_sees_remarks(
    client, db, setup, position
):
    row = await file(db, setup)
    if position != "chairperson":
        row = await decide(db, setup, row, "chairperson")
    if position == "registrar":
        row = await decide(db, setup, row, "dean")
    row = await decide(
        db,
        setup,
        row,
        position,
        "REJECT",
        decision=CorrectionDecision(
            action="REJECT", version=row.version, remarks="Synthetic rejection"
        ),
    )
    assert row.state == "REJECTED"
    assert await persisted(db, setup) == ("2.00", (Decimal("2"), Decimal("2")))
    faculty_override(setup)
    response = await client.get(f"{FACULTY}/grade-corrections/{row.id}")
    assert response.status_code == 200, response.text
    assert response.json()["rejectionRemarks"] == "Synthetic rejection"


@pytest.mark.parametrize("remarks", [None, "", "   "])
async def test_ac_fac_002_7_rejection_requires_remarks_http(client, db, setup, remarks):
    row = await file(db, setup)
    admin_override(setup, "chairperson")
    response = await client.patch(
        f"{ADMIN}/{row.id}", json={"action": "REJECT", "version": 1, "remarks": remarks}
    )
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "DECISION_REMARKS_REQUIRED"


async def test_ac_fac_002_8_one_masked_audit_per_successful_step(db, setup):
    row = await registrar_pending(db, setup)
    row = await decide(db, setup, row, "registrar", "COMMIT")
    entries = list(
        await db.scalars(
            select(AuditEntry)
            .where(AuditEntry.target_id == str(row.id))
            .order_by(AuditEntry.occurred_at)
        )
    )
    assert len(entries) == 4
    assert [entry.action for entry in entries] == [
        "grade_correction.file",
        "grade_correction.endorse",
        "grade_correction.endorse",
        "grade_correction.commit",
    ]
    for entry in entries:
        assert entry.actor_id is not None
        assert entry.payload["timestampUtc8"].endswith("+08:00")
        assert entry.payload["clientIp"].startswith("192.0.2.")
        assert entry.payload["reason"] == "[MASKED]"
        assert entry.payload["oldMark"] == "2.00"
        assert entry.payload["newMark"] == "1.25"
        assert "Synthetic correction reason" not in str(entry.payload)


async def test_ac_fac_002_9_foreign_section_and_missing_section_same_404(client, setup):
    faculty_override(setup)
    bodies = []
    for section_id in [setup["foreign_id"], uuid.uuid4()]:
        response = await client.post(
            f"{FACULTY}/sections/{section_id}/grade-corrections",
            data={
                "student_id": str(setup["alice_id"]),
                "proposed_mark": "1.25",
                "reason": "Synthetic",
            },
            files={"grading_sheet": ("sheet.png", sheet(), "image/png")},
        )
        assert response.status_code == 404, response.text
        bodies.append({k: v for k, v in response.json().items() if k != "request_id"})
    assert bodies[0] == bodies[1]


@pytest.mark.parametrize(
    "position,department,college,visible",
    [
        ("chairperson", "Computer Science", "CCIS", True),
        ("chairperson", "Other", "CCIS", False),
        ("dean", "Other", "CCIS", True),
        ("dean", "Computer Science", "Other", False),
        ("registrar", None, None, True),
    ],
)
async def test_admin_list_detail_patch_scope(
    client, db, setup, position, department, college, visible
):
    row = await file(db, setup)
    admin_override(setup, position, department, college)
    listing = await client.get(ADMIN)
    assert listing.status_code == 200, listing.text
    assert listing.json()["total"] == int(visible)
    detail = await client.get(f"{ADMIN}/{row.id}")
    assert detail.status_code == (200 if visible else 404)
    decision = await client.patch(
        f"{ADMIN}/{row.id}", json={"action": "ENDORSE", "version": 1}
    )
    assert decision.status_code == (
        200 if position == "chairperson" and visible else 409 if visible else 404
    ), decision.text


async def test_finance_denied_all_admin_routes(client, db, setup):
    row = await file(db, setup)
    admin_override(setup, "finance")
    for method, path in [
        ("GET", ADMIN),
        ("GET", f"{ADMIN}/{row.id}"),
        ("PATCH", f"{ADMIN}/{row.id}"),
    ]:
        response = await client.request(
            method,
            path,
            **(
                {"json": {"action": "COMMIT", "version": 1}}
                if method == "PATCH"
                else {}
            ),
        )
        assert response.status_code == 403, response.text


async def test_stale_version_and_stale_source_mark_do_not_double_apply(
    client, db, setup
):
    row = await file(db, setup)
    admin_override(setup, "chairperson")
    first = await client.patch(
        f"{ADMIN}/{row.id}", json={"action": "ENDORSE", "version": 1}
    )
    assert first.status_code == 200, first.text
    # Different actor avoids same-person precedence, exercising version conflict.
    admin_override(setup, "dean")
    stale = await client.patch(
        f"{ADMIN}/{row.id}", json={"action": "ENDORSE", "version": 1}
    )
    assert stale.status_code == 409 and stale.json()["code"] == "VERSION_CONFLICT"
    row = await corrections.read_correction(db, row.id, faculty_id=setup["faculty_id"])
    row = await decide(db, setup, row, "dean")
    grade = await db.scalar(
        select(StudentGrade).where(
            StudentGrade.enrollment_item_id == setup["alice_enr_id"]
        )
    )
    grade.updated_at = NOW + timedelta(
        seconds=1
    )  # ABA: same mark, changed version stamp.
    await db.commit()
    from cbpupsis_shared.domains.grading.exceptions import GradeVersionConflictError

    with pytest.raises(GradeVersionConflictError):
        await decide(db, setup, row, "registrar", "COMMIT")
    assert await persisted(db, setup) == ("2.00", (Decimal("2"), Decimal("2")))


@pytest.mark.parametrize(
    "kind,expected",
    [("PNG", "image/png"), ("JPEG", "image/jpeg"), ("PDF", "application/pdf")],
)
def test_grading_sheet_actual_content(kind, expected):
    assert inspect_grading_sheet(sheet(kind)) == expected


@pytest.mark.parametrize(
    "content,status,code",
    [
        (b"plain text disguised as pdf", 415, "UNSUPPORTED_MEDIA_TYPE"),
        (b"%PDF-1.7\nnot a document", 415, "UNSUPPORTED_MEDIA_TYPE"),
        (b"\x89PNG\r\n\x1a\ntruncated", 415, "UNSUPPORTED_MEDIA_TYPE"),
        (b"x" * (MAX_GRADING_SHEET_BYTES + 1), 413, "FILE_TOO_LARGE"),
    ],
    ids=["text", "fake-pdf", "truncated-png", "oversized"],
)
async def test_attachment_refusals_http(client, setup, content, status, code):
    faculty_override(setup)
    response = await client.post(
        f"{FACULTY}/sections/{setup['own_id']}/grade-corrections",
        data={
            "student_id": str(setup["alice_id"]),
            "proposed_mark": "1.25",
            "reason": "Synthetic",
        },
        files={"grading_sheet": ("claimed.pdf", content, "application/pdf")},
    )
    assert response.status_code == status, response.text
    assert response.json()["code"] == code


def test_grading_sheet_exact_size_boundary():
    content = sheet("PDF")
    content += b" " * (MAX_GRADING_SHEET_BYTES - len(content))
    assert inspect_grading_sheet(content) == "application/pdf"


async def real_admin_token(db, data, position, *, live=True):
    from cbpupsis_database.models.iam import Group
    from cbpupsis_database.models.users import AdminProfile
    from cbpupsis_shared.domains.auth import service as auth
    from cbpupsis_shared.domains.auth.security import create_access_token
    from cbpupsis_shared.domains.iam.constants import ADMIN_GROUP

    group = await db.scalar(select(Group).where(Group.name == ADMIN_GROUP))
    await iam.add_user_to_group(db, data[position], group.id)
    db.add(
        AdminProfile(
            user_id=data[position],
            position=position,
            department="Computer Science",
            college="CCIS",
        )
    )
    sid = (
        auth.stage_session(
            db,
            user_id=data[position],
            ip_address="192.0.2.10",
            device_info="Synthetic correction test",
        )
        if live
        else uuid.uuid4()
    )
    await db.commit()
    subject = await db.get(User, data[position], populate_existing=True)
    return create_access_token(
        data[position],
        claims={
            "role": "admin",
            "session_version": subject.session_version,
            "mfa": True,
            "sid": str(sid),
            "position": position,
            "department_id": "Computer Science",
            "college_id": "CCIS",
        },
    )


@pytest.mark.parametrize("role", ["faculty", "admin"])
@pytest.mark.parametrize("condition", ["expired", "mfa"])
async def test_real_session_and_mfa_contracts_on_every_endpoint(
    client, db, setup, role, condition
):
    from cbpupsis_shared.domains.auth.security import (
        create_access_token,
        create_challenge_token,
    )

    row = await file(db, setup)
    subject = setup["faculty_id"] if role == "faculty" else setup["chairperson"]
    if condition == "mfa":
        token, _, _ = create_challenge_token(
            subject, role=role, enrollment_required=False
        )
    elif role == "admin":
        token = await real_admin_token(db, setup, "chairperson", live=False)
    else:
        token = create_access_token(
            subject, claims={"role": role, "mfa": True, "sid": str(uuid.uuid4())}
        )
    headers = {"Authorization": f"Bearer {token}"}
    paths = (
        [
            ("GET", f"{FACULTY}/grade-corrections", {}),
            ("GET", f"{FACULTY}/grade-corrections/{row.id}", {}),
            (
                "POST",
                f"{FACULTY}/sections/{setup['own_id']}/grade-corrections",
                {
                    "data": {
                        "student_id": str(setup["alice_id"]),
                        "proposed_mark": "1.25",
                        "reason": "Synthetic",
                    },
                    "files": {"grading_sheet": ("sheet.png", sheet(), "image/png")},
                },
            ),
        ]
        if role == "faculty"
        else [
            ("GET", ADMIN, {}),
            ("GET", f"{ADMIN}/{row.id}", {}),
            (
                "PATCH",
                f"{ADMIN}/{row.id}",
                {"json": {"action": "ENDORSE", "version": 1}},
            ),
        ]
    )
    for method, path, payload in paths:
        response = await client.request(method, path, headers=headers, **payload)
        assert response.status_code == 401, response.text
        assert response.json()["code"] == (
            "AUTH_SESSION_EXPIRED" if condition == "expired" else "AUTH_MFA_REQUIRED"
        )


async def test_real_faculty_and_admin_sessions_use_permissions_and_positions(
    client, db, setup
):
    from cbpupsis_shared.domains.auth import service as auth
    from cbpupsis_shared.domains.auth.security import create_access_token

    sid = auth.stage_session(
        db,
        user_id=setup["faculty_id"],
        ip_address="192.0.2.10",
        device_info="Synthetic",
    )
    await db.commit()
    faculty_token = create_access_token(
        setup["faculty_id"], claims={"role": "faculty", "mfa": True, "sid": str(sid)}
    )
    response = await client.post(
        f"{FACULTY}/sections/{setup['own_id']}/grade-corrections",
        headers={"Authorization": f"Bearer {faculty_token}"},
        data={
            "student_id": str(setup["alice_id"]),
            "proposed_mark": "1.25",
            "reason": "Synthetic real-session filing",
        },
        files={"grading_sheet": ("sheet.pdf", sheet("PDF"), "application/pdf")},
    )
    assert response.status_code == 201, response.text
    token = await real_admin_token(db, setup, "chairperson")
    decision = await client.patch(
        f"{ADMIN}/{response.json()['id']}",
        headers={"Authorization": f"Bearer {token}"},
        json={"action": "ENDORSE", "version": 1},
    )
    assert decision.status_code == 200, decision.text
    assert decision.json()["state"] == "PENDING_DEAN"


async def test_faculty_list_and_detail_hide_other_filers(client, db, setup):
    row = await file(db, setup)
    faculty_override({**setup, "faculty_id": setup["other_faculty_id"]})
    # Grant read to this second faculty through the same real policy bundle.
    policy = await db.scalar(
        select(Policy).where(Policy.name == "GradeCorrectionAuthor")
    )
    await iam.attach_policy_to_user(db, setup["other_faculty_id"], policy.id)
    assert (await client.get(f"{FACULTY}/grade-corrections")).json()["total"] == 0
    response = await client.get(f"{FACULTY}/grade-corrections/{row.id}")
    assert response.status_code == 404


async def test_same_actor_http_and_terminal_decision_refusal(client, db, setup):
    row = await file(db, setup)
    admin_override({**setup, "chairperson": setup["faculty_id"]}, "chairperson")
    policy = await db.scalar(
        select(Policy).where(Policy.name == "GradeCorrectionAdmin")
    )
    await iam.attach_policy_to_user(db, setup["faculty_id"], policy.id)
    response = await client.patch(
        f"{ADMIN}/{row.id}", json={"action": "ENDORSE", "version": 1}
    )
    assert (
        response.status_code == 403 and response.json()["code"] == "APPROVAL_SAME_ACTOR"
    )
    row = await decide(
        db,
        setup,
        row,
        "chairperson",
        "REJECT",
        decision=CorrectionDecision(action="REJECT", version=1, remarks="Synthetic"),
    )
    admin_override(setup, "dean")
    response = await client.patch(
        f"{ADMIN}/{row.id}", json={"action": "ENDORSE", "version": row.version}
    )
    assert (
        response.status_code == 409
        and response.json()["code"] == "APPROVAL_OUT_OF_ORDER"
    )


async def test_commit_without_standing_cache_uses_derived_gwa_without_fake_standing(
    db, setup
):
    from sqlalchemy import delete

    await db.execute(
        delete(StudentTermGPA).where(StudentTermGPA.student_id == setup["alice_id"])
    )
    await db.commit()
    row = await registrar_pending(db, setup)
    await decide(db, setup, row, "registrar", "COMMIT")
    result = await grades.read_grades(db, student_id=setup["alice_id"])
    assert result.gwa == Decimal("1.25") and result.academic_standing is None
    assert (
        await db.scalar(
            select(func.count())
            .select_from(StudentTermGPA)
            .where(StudentTermGPA.student_id == setup["alice_id"])
        )
        == 0
    )


async def test_correction_to_inc_keeps_undefined_gwa_null_and_release_unchanged(
    db, setup
):
    before = await db.scalar(
        select(StudentGrade.submitted_at).where(
            StudentGrade.enrollment_item_id == setup["alice_enr_id"]
        )
    )
    row = await file(db, setup, proposed_mark="INC")
    row = await decide(db, setup, row, "chairperson")
    row = await decide(db, setup, row, "dean")
    await decide(db, setup, row, "registrar", "COMMIT")
    assert await persisted(db, setup) == ("INC", (None, None))
    result = await grades.read_grades(db, student_id=setup["alice_id"])
    assert result.gwa is None
    assert (
        await grades.get_previous_regular_term_gwa(
            db, student_id=setup["alice_id"], before=date(2027, 1, 1)
        )
        is None
    )
    assert (
        await db.scalar(
            select(StudentGrade.submitted_at).where(
                StudentGrade.enrollment_item_id == setup["alice_enr_id"]
            )
        )
        == before
    )


@pytest.mark.parametrize(
    "mark,reason",
    [
        ("4.00", "Reason"),
        ("NaN", "Reason"),
        ("1.111", "Reason"),
        ("1.25", "   "),
        ("1e999999999", "Reason"),
        ("1e-999999999", "Reason"),
    ],
)
async def test_invalid_filing_no_partial_request(db, setup, mark, reason):
    from cbpupsis_core.exceptions import ValidationError

    with pytest.raises(ValidationError):
        await file(db, setup, proposed_mark=mark, reason=reason)
    assert await db.scalar(select(func.count()).select_from(GradeCorrection)) == 0


async def test_finance_cannot_use_directly_granted_approver_permission(
    client, db, setup
):
    policy = await db.scalar(
        select(Policy).where(Policy.name == "GradeCorrectionAdmin")
    )
    await iam.attach_policy_to_user(db, setup["finance"], policy.id)
    admin_override(setup, "finance")
    response = await client.get(ADMIN)
    assert response.status_code == 403


async def test_inactive_student_login_does_not_block_record_correction(db, setup):
    student = await db.get(User, setup["alice_id"])
    student.is_active = False
    await db.commit()
    row = await registrar_pending(db, setup)
    row = await decide(db, setup, row, "registrar", "COMMIT")
    assert row.state == "COMMITTED"
    assert await persisted(db, setup) == ("1.25", (Decimal("1.25"), Decimal("1.25")))
    from cbpupsis_shared.domains.auth.exceptions import ResourceNotFoundError

    with pytest.raises(ResourceNotFoundError):
        await grades.read_grades(db, student_id=setup["alice_id"])


@pytest.mark.parametrize(
    "field,value",
    [
        ("is_draft", True),
        ("is_locked", False),
        ("final_grade", None),
        ("submitted_at", None),
    ],
)
async def test_filing_requires_published_final_grade(db, setup, field, value):
    from cbpupsis_core.exceptions import ValidationError

    grade = await db.scalar(
        select(StudentGrade).where(
            StudentGrade.enrollment_item_id == setup["alice_enr_id"]
        )
    )
    setattr(grade, field, value)
    await db.commit()
    with pytest.raises(ValidationError):
        await file(db, setup)
    assert await db.scalar(select(func.count()).select_from(GradeCorrection)) == 0


@pytest.mark.parametrize("kind", ["GIF", "BMP"])
def test_other_image_formats_are_not_grading_sheets(kind):
    from cbpupsis_shared.domains.grading.attachments import UnsupportedGradingSheet

    with pytest.raises(UnsupportedGradingSheet):
        inspect_grading_sheet(sheet(kind))


async def test_scope_precedes_pagination_and_registrar_sees_multiple_colleges(
    client, db, setup, monkeypatch
):
    local = await file(db, setup)
    other_course = Course(
        course_code="MATH 103",
        title="Synthetic second-college course",
        department="Mathematics",
        college="Science",
        lecture_units=Decimal("3"),
        lab_units=Decimal("0"),
        is_nstp=False,
    )
    db.add(other_course)
    await db.flush()
    section = CourseSection(
        course_id=other_course.id,
        academic_term_id=setup["open_term_id"],
        faculty_id=setup["faculty_id"],
        section_name="SYNTHETIC-OTHER",
        capacity=40,
    )
    db.add(section)
    await db.flush()
    original = await db.get(StudentCourseEnrollment, setup["alice_enr_id"])
    attempt = StudentCourseEnrollment(
        term_enrollment_id=original.term_enrollment_id,
        section_id=section.id,
        academic_term_id=setup["open_term_id"],
        status="enrolled",
    )
    db.add(attempt)
    await db.commit()
    initial = await grades.get_section_grades(
        db, faculty_id=setup["faculty_id"], section_id=section.id
    )
    draft = await grades.save_section_grade_draft(
        db,
        faculty_id=setup["faculty_id"],
        section_id=section.id,
        payload=SaveGradeDraftRequest(
            version=initial.version,
            grades=[{"enrollmentId": str(attempt.id), "finalGrade": "2.50"}],
        ),
    )
    await grades.finalize_section_grades(
        db,
        faculty_id=setup["faculty_id"],
        section_id=section.id,
        payload=FinalizeGradesRequest(version=draft.version),
    )
    monkeypatch.setattr(corrections, "_now", lambda: NOW + timedelta(seconds=1))
    foreign = await file(db, setup, section_id=section.id)
    # The newest global row is foreign: post-pagination filtering would lose the
    # visible Chair row when limit=1, or report the wrong total.
    admin_override(setup, "chairperson")
    scoped = await client.get(ADMIN, params={"limit": 1})
    assert scoped.status_code == 200, scoped.text
    assert scoped.json()["total"] == 1
    assert scoped.json()["items"][0]["id"] == str(local.id)
    for position in ("chairperson", "dean"):
        admin_override(setup, position)
        assert (await client.get(f"{ADMIN}/{foreign.id}")).status_code == 404
        rejected = await client.patch(
            f"{ADMIN}/{foreign.id}", json={"action": "ENDORSE", "version": 1}
        )
        assert rejected.status_code == 404
    admin_override(setup, "registrar", None, None)
    first = (await client.get(ADMIN, params={"limit": 1})).json()
    second = (await client.get(ADMIN, params={"limit": 1, "offset": 1})).json()
    assert first["total"] == second["total"] == 2
    assert {first["items"][0]["id"], second["items"][0]["id"]} == {
        str(local.id),
        str(foreign.id),
    }
    # Scope follows current section/course ownership, not a filing-time snapshot.
    course = await db.get(Course, setup["course_id"])
    course.department, course.college = "Mathematics", "Science"
    await db.commit()
    admin_override(setup, "chairperson")
    assert (await client.get(ADMIN)).json()["total"] == 0
    admin_override(setup, "dean", None, "Science")
    assert (await client.get(ADMIN)).json()["total"] == 2


@pytest.mark.parametrize("department,college", [(None, None), ("   ", "CCIS")])
async def test_missing_catalog_scope_refuses_without_invented_department(
    db, setup, department, college
):
    from cbpupsis_shared.domains.grading.exceptions import GradeRecordUnavailableError

    course = await db.get(Course, setup["course_id"])
    course.department, course.college = department, college
    await db.commit()
    with pytest.raises(GradeRecordUnavailableError):
        await file(db, setup)
    assert await db.scalar(select(func.count()).select_from(GradeCorrection)) == 0
