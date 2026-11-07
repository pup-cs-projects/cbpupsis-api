from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_core.events import Event
from cbpupsis_database.models.academics import AcademicTerm
from cbpupsis_database.models.audit import AuditEntry
from cbpupsis_database.models.curriculum import (
    Course,
    Curriculum,
    CurriculumCourse,
    Program,
)
from cbpupsis_database.models.enrollment import OverloadRequest
from cbpupsis_database.models.grading import StudentTermGPA
from cbpupsis_database.models.outbox import OutboxMessage
from cbpupsis_database.models.users import (
    AdminProfile,
    StudentProfile,
    User,
    UserProfile,
)
from cbpupsis_shared.domains.auth import service as auth_service
from cbpupsis_shared.domains.auth.security import hash_password
from cbpupsis_shared.domains.enrollment.constants import (
    APPROVE_OVERLOAD_REQUEST,
    CREATE_OVERLOAD_REQUEST,
    OVERLOAD_APPROVED,
    OVERLOAD_PENDING,
    OVERLOAD_REJECTED,
    OVERLOAD_REQUEST_DECIDED_EVENT,
    OVERLOAD_REQUEST_FILED_ACTION,
    READ_ENROLLMENT,
    READ_OVERLOAD_REQUEST,
)
from cbpupsis_shared.domains.enrollment.service import assess_overload_eligibility
from cbpupsis_shared.domains.iam import service as iam_service
from cbpupsis_shared.domains.iam.constants import ADMIN_GROUP
from cbpupsis_shared.domains.notifications.templates import render

STUDENT_URL = "/api/v1/student/enrollment/overload"
ADMIN_URL = "/api/v1/admin/overload-requests"
LOGIN_URL = "/api/v1/auth/student/login"
PASSWORD = "StudentPassword123!"


async def _term(
    db: AsyncSession,
    *,
    year: str,
    semester: str,
    start: date,
    current: bool,
) -> AcademicTerm:
    now = datetime.now(UTC)
    term = AcademicTerm(
        academic_year=year,
        semester=semester,
        start_date=start,
        end_date=start + timedelta(days=120),
        is_current=current,
        regular_enrollment_start=now - timedelta(days=1),
        regular_enrollment_end=now + timedelta(days=1),
        add_drop_deadline=now + timedelta(days=7),
        grade_submission_deadline=now + timedelta(days=30),
    )
    db.add(term)
    await db.flush()
    return term


async def _program(
    db: AsyncSession,
    *,
    code: str,
    department: str,
    college: str,
    prescribed_load: Decimal = Decimal("26"),
) -> tuple[Program, Curriculum]:
    program = Program(
        code=code,
        name=f"{code} Program",
        department=department,
        college=college,
        total_units_required=Decimal("100"),
        is_active=True,
    )
    db.add(program)
    await db.flush()
    curriculum = Curriculum(
        program_id=program.id, curriculum_year="2026", is_active=True
    )
    db.add(curriculum)
    await db.flush()
    course = Course(
        course_code=f"{code}LOAD",
        title="Prescribed Load",
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
            year_level=4,
            semester="First Semester",
        )
    )
    return program, curriculum


async def _student(
    db: AsyncSession,
    *,
    number: str,
    program: Program,
    curriculum: Curriculum,
    graduating: bool = False,
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
            first_name="Overload",
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
            year_level=4,
            enrollment_status="PENDING",
            is_graduating_senior=graduating,
        )
    )
    await db.flush()
    return user


async def _grant_action(db: AsyncSession, *, action: str, users: list[User]) -> None:
    suffix = uuid.uuid4().hex
    await iam_service.create_permission(db, action=action)
    policy = await iam_service.create_policy(
        db, name=f"policy-{suffix}", permission_actions=[action]
    )
    group = await iam_service.create_group(db, name=f"group-{suffix}")
    await iam_service.attach_policy_to_group(db, group.id, policy.id)
    for user in users:
        await iam_service.add_user_to_group(db, user.id, group.id)


async def _student_headers(client: AsyncClient, number: str) -> dict[str, str]:
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


async def _admin(
    db: AsyncSession,
    *,
    position: str,
    department: str | None = None,
    college: str | None = None,
) -> tuple[User, dict[str, str]]:
    user = User(
        email=f"{position}-{uuid.uuid4().hex}@example.com",
        password_hash="not-used",
        full_name=f"{position.title()} Reviewer",
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
    await db.commit()
    group = await db.scalar(select(func.count()).select_from(AdminProfile))
    admin_group = (
        await iam_service.create_group(
            db, name=ADMIN_GROUP if group == 1 else f"unused-{uuid.uuid4().hex}"
        )
        if group == 1
        else None
    )
    if admin_group is None:
        from cbpupsis_database.models.iam import Group

        admin_group = await db.scalar(select(Group).where(Group.name == ADMIN_GROUP))
    assert admin_group is not None
    await iam_service.add_user_to_group(db, user.id, admin_group.id)
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


@pytest.fixture
async def overload_setup(db: AsyncSession):
    previous = await _term(
        db,
        year="2025-2026",
        semester="Second Semester",
        start=date(2025, 1, 1),
        current=False,
    )
    current = await _term(
        db,
        year="2026-2027",
        semester="First Semester",
        start=date(2026, 8, 1),
        current=True,
    )
    program, curriculum = await _program(
        db, code="BSCS", department="Computer Science", college="CCIS"
    )
    student = await _student(
        db,
        number="2026-00170-MN-0",
        program=program,
        curriculum=curriculum,
    )
    db.add(
        StudentTermGPA(
            student_id=student.id,
            academic_term_id=previous.id,
            total_gpa_units=Decimal("18"),
            term_gpa=Decimal("1.75"),
            cumulative_gpa=Decimal("1.75"),
            academic_standing="Good Standing",
        )
    )
    await db.commit()
    await _grant_action(db, action=CREATE_OVERLOAD_REQUEST, users=[student])
    await _grant_action(db, action=READ_ENROLLMENT, users=[student])
    return {
        "previous": previous,
        "current": current,
        "program": program,
        "curriculum": curriculum,
        "student": student,
    }


def _file_body(units: int = 9) -> dict[str, object]:
    return {
        "term": "2026-2027-First Semester",
        "requestedUnits": units,
        "justification": "Final subjects required for graduation.",
    }


def test_ac_enrl_001_3_eighty_five_percent_completion_boundary() -> None:
    boundary = assess_overload_eligibility(
        total_units_earned=Decimal("85"),
        total_units_required=Decimal("100"),
        gwa=None,
    )
    below = assess_overload_eligibility(
        total_units_earned=Decimal("84.9"),
        total_units_required=Decimal("100"),
        gwa=None,
    )

    assert boundary.eligible is True
    assert boundary.is_graduating is True
    assert below.eligible is False
    assert below.is_graduating is False


async def test_ac_enrl_001_3_gwa_175_boundary_files_request(
    client: AsyncClient, db: AsyncSession, overload_setup
) -> None:
    response = await client.post(
        STUDENT_URL,
        headers=await _student_headers(client, "2026-00170-MN-0"),
        json=_file_body(),
    )

    assert response.status_code == 202, response.text
    assert response.json()["state"] == OVERLOAD_PENDING
    assert await db.scalar(select(func.count()).select_from(OverloadRequest)) == 1
    audit = await db.scalar(
        select(AuditEntry).where(AuditEntry.action == OVERLOAD_REQUEST_FILED_ACTION)
    )
    assert audit is not None
    assert audit.actor_id == overload_setup["student"].id
    assert audit.new_state["requestedUnits"] == 9


async def test_ac_enrl_001_4_request_over_nine_is_refused(
    client: AsyncClient, db: AsyncSession, overload_setup
) -> None:
    response = await client.post(
        STUDENT_URL,
        headers=await _student_headers(client, "2026-00170-MN-0"),
        json=_file_body(10),
    )

    assert response.status_code == 409
    assert response.json()["code"] == "ENROLL_OVERLOAD_CAP_EXCEEDED"
    assert await db.scalar(select(func.count()).select_from(OverloadRequest)) == 0


async def test_ac_enrl_001_5_ineligible_student_is_refused(
    client: AsyncClient, db: AsyncSession, overload_setup
) -> None:
    gpa = await db.scalar(select(StudentTermGPA))
    assert gpa is not None
    gpa.term_gpa = Decimal("1.76")
    await db.commit()

    response = await client.post(
        STUDENT_URL,
        headers=await _student_headers(client, "2026-00170-MN-0"),
        json=_file_body(),
    )

    assert response.status_code == 409
    body = response.json()
    assert body["code"] == "ENROLL_OVERLOAD_NOT_ELIGIBLE"
    assert body["details"] == {
        "isGraduating": False,
        "gwa": 1.76,
        "requiredGwa": 1.75,
    }
    assert await db.scalar(select(func.count()).select_from(OverloadRequest)) == 0


async def _file(client: AsyncClient) -> dict:
    response = await client.post(
        STUDENT_URL,
        headers=await _student_headers(client, "2026-00170-MN-0"),
        json=_file_body(),
    )
    assert response.status_code == 202, response.text
    return response.json()


@pytest.mark.parametrize(
    ("decision", "expected_state", "remarks"),
    [
        ("APPROVE", OVERLOAD_APPROVED, "Approved for graduating load."),
        ("REJECT", OVERLOAD_REJECTED, "Submit an updated study plan."),
    ],
)
async def test_ac_enrl_001_3_5_dean_decision_audit_and_notification(
    client: AsyncClient,
    db: AsyncSession,
    overload_setup,
    decision: str,
    expected_state: str,
    remarks: str,
) -> None:
    filed = await _file(client)
    dean, headers = await _admin(db, position="dean", college="CCIS")
    await _grant_action(db, action=APPROVE_OVERLOAD_REQUEST, users=[dean])
    response = await client.patch(
        f"{ADMIN_URL}/{filed['overloadRequestId']}",
        headers=headers,
        json={"decision": decision, "remarks": remarks},
    )

    assert response.status_code == 200, response.text
    assert response.json()["state"] == expected_state
    action = f"enrollment.overload_{expected_state.lower()}"
    audits = list(
        await db.scalars(select(AuditEntry).where(AuditEntry.action == action))
    )
    assert len(audits) == 1
    assert audits[0].actor_id == dean.id
    messages = list(
        await db.scalars(
            select(OutboxMessage).where(
                OutboxMessage.event_name == OVERLOAD_REQUEST_DECIDED_EVENT
            )
        )
    )
    assert len(messages) == 1
    message = messages[0]
    assert message.payload["remarks"] == remarks
    rendered = render(Event(name=message.event_name, payload=message.payload))
    assert remarks in rendered.body


async def test_ac_enrl_001_3_approval_raises_eligibility_ceiling_to_35(
    client: AsyncClient, db: AsyncSession, overload_setup
) -> None:
    filed = await _file(client)
    dean, headers = await _admin(db, position="dean", college="CCIS")
    await _grant_action(db, action=APPROVE_OVERLOAD_REQUEST, users=[dean])
    decided = await client.patch(
        f"{ADMIN_URL}/{filed['overloadRequestId']}",
        headers=headers,
        json={"decision": "APPROVE", "remarks": "Approved."},
    )
    assert decided.status_code == 200, decided.text

    eligibility = await client.get(
        "/api/v1/student/enrollment/eligibility",
        headers=await _student_headers(client, "2026-00170-MN-0"),
    )
    assert eligibility.status_code == 200, eligibility.text
    assert Decimal(eligibility.json()["prescribedLoad"]) == Decimal("26")
    assert Decimal(eligibility.json()["approvedOverloadUnits"]) == Decimal("9")
    assert Decimal(eligibility.json()["effectiveCeiling"]) == Decimal("35")


async def test_ac_enrl_001_3_pending_request_shows_on_eligibility(
    client: AsyncClient, db: AsyncSession, overload_setup
) -> None:
    """The enrollment page shows "Waiting for the Dean" from pendingOverloadUnits."""
    url = "/api/v1/student/enrollment/eligibility"
    await _file(client)
    headers = await _student_headers(client, "2026-00170-MN-0")

    pending = await client.get(url, headers=headers)
    assert pending.status_code == 200, pending.text
    assert Decimal(pending.json()["pendingOverloadUnits"]) == Decimal("9")
    assert Decimal(pending.json()["approvedOverloadUnits"]) == Decimal("0")
    assert Decimal(pending.json()["effectiveCeiling"]) == Decimal("26")


async def test_ac_enrl_001_5_rejection_requires_remarks_and_replay_conflicts(
    client: AsyncClient, db: AsyncSession, overload_setup
) -> None:
    filed = await _file(client)
    dean, headers = await _admin(db, position="dean", college="CCIS")
    await _grant_action(db, action=APPROVE_OVERLOAD_REQUEST, users=[dean])
    path = f"{ADMIN_URL}/{filed['overloadRequestId']}"

    blank = await client.patch(
        path,
        headers=headers,
        json={"decision": "REJECT", "remarks": "   "},
    )
    assert blank.status_code == 422
    assert blank.json()["code"] == "DECISION_REMARKS_REQUIRED"

    first = await client.patch(
        path,
        headers=headers,
        json={"decision": "APPROVE", "remarks": "Approved."},
    )
    second = await client.patch(
        path,
        headers=headers,
        json={"decision": "REJECT", "remarks": "Too late."},
    )
    assert first.status_code == 200
    assert second.status_code == 409
    assert second.json()["code"] == "REQUEST_ALREADY_DECIDED"


async def test_ac_enrl_001_5_admin_role_scopes_and_student_refusal(
    client: AsyncClient, db: AsyncSession, overload_setup
) -> None:
    first = OverloadRequest(
        student_id=overload_setup["student"].id,
        academic_term_id=overload_setup["current"].id,
        requested_units=3,
        justification="CCIS request",
        state=OVERLOAD_PENDING,
    )
    other_program, other_curriculum = await _program(
        db, code="BSA", department="Accountancy", college="CBA"
    )
    other_student = await _student(
        db,
        number="2026-00171-MN-0",
        program=other_program,
        curriculum=other_curriculum,
        graduating=True,
    )
    second = OverloadRequest(
        student_id=other_student.id,
        academic_term_id=overload_setup["current"].id,
        requested_units=3,
        justification="CBA request",
        state=OVERLOAD_PENDING,
    )
    db.add_all([first, second])
    await db.commit()

    chair, chair_headers = await _admin(
        db, position="chairperson", department="Computer Science"
    )
    dean, dean_headers = await _admin(db, position="dean", college="CCIS")
    registrar, registrar_headers = await _admin(db, position="registrar")
    await _grant_action(
        db, action=READ_OVERLOAD_REQUEST, users=[chair, dean, registrar]
    )
    await _grant_action(db, action=APPROVE_OVERLOAD_REQUEST, users=[dean])

    chair_list = await client.get(ADMIN_URL, headers=chair_headers)
    dean_list = await client.get(ADMIN_URL, headers=dean_headers)
    registrar_list = await client.get(ADMIN_URL, headers=registrar_headers)
    assert [row["overloadRequestId"] for row in chair_list.json()] == [str(first.id)]
    assert [row["overloadRequestId"] for row in dean_list.json()] == [str(first.id)]
    assert {row["overloadRequestId"] for row in registrar_list.json()} == {
        str(first.id),
        str(second.id),
    }

    assert (
        await client.patch(
            f"{ADMIN_URL}/{first.id}",
            headers=chair_headers,
            json={"decision": "APPROVE", "remarks": "No."},
        )
    ).status_code == 403
    outside = await client.patch(
        f"{ADMIN_URL}/{second.id}",
        headers=dean_headers,
        json={"decision": "APPROVE", "remarks": "No."},
    )
    assert outside.status_code == 404
    assert outside.json()["code"] == "RESOURCE_NOT_FOUND"
    assert (
        await client.patch(
            f"{ADMIN_URL}/{first.id}",
            headers=registrar_headers,
            json={"decision": "APPROVE", "remarks": "No."},
        )
    ).status_code == 403
    student_headers = await _student_headers(client, "2026-00170-MN-0")
    assert (await client.get(ADMIN_URL, headers=student_headers)).status_code == 403


async def test_ac_enrl_001_5_permissions_are_seeded_by_admin_position(
    db: AsyncSession, monkeypatch
) -> None:
    from scripts import seed_iam

    class SeedSession:
        async def __aenter__(self) -> AsyncSession:
            return db

        async def __aexit__(self, *exc_info: object) -> None:
            return None

    users_by_position = {}
    for position in ("chairperson", "dean", "registrar", "finance"):
        user = User(
            email=f"seed-{position}@example.com",
            password_hash="not-used",
            full_name=f"Seed {position.title()}",
            is_active=True,
        )
        db.add(user)
        await db.flush()
        db.add(AdminProfile(user_id=user.id, position=position, is_active=True))
        users_by_position[position] = user
    await db.commit()

    monkeypatch.setattr(seed_iam, "AsyncSessionLocal", lambda: SeedSession())
    await seed_iam.seed()

    chair = await iam_service.get_effective_permissions(
        db, users_by_position["chairperson"].id
    )
    dean = await iam_service.get_effective_permissions(db, users_by_position["dean"].id)
    registrar = await iam_service.get_effective_permissions(
        db, users_by_position["registrar"].id
    )
    finance = await iam_service.get_effective_permissions(
        db, users_by_position["finance"].id
    )
    assert READ_OVERLOAD_REQUEST in chair
    assert APPROVE_OVERLOAD_REQUEST not in chair
    assert {READ_OVERLOAD_REQUEST, APPROVE_OVERLOAD_REQUEST} <= dean
    assert READ_OVERLOAD_REQUEST in registrar
    assert APPROVE_OVERLOAD_REQUEST not in registrar
    assert READ_OVERLOAD_REQUEST not in finance
    assert APPROVE_OVERLOAD_REQUEST not in finance
