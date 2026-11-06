from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from httpx import AsyncClient
from sqlalchemy import select
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
from cbpupsis_database.models.enrollment import (
    AddDropRequest,
    CorIssuance,
    StudentCourseEnrollment,
    TermEnrollment,
)
from cbpupsis_database.models.grading import StudentTermGPA
from cbpupsis_database.models.outbox import OutboxMessage
from cbpupsis_database.models.sections import CourseSection, SectionSchedule
from cbpupsis_database.models.users import (
    AdminProfile,
    StudentProfile,
    User,
    UserProfile,
)
from cbpupsis_shared.domains.auth import service as auth_service
from cbpupsis_shared.domains.auth.security import hash_password
from cbpupsis_shared.domains.enrollment import repository
from cbpupsis_shared.domains.enrollment.constants import (
    ADD_DROP_APPROVED,
    ADD_DROP_PENDING,
    ADD_DROP_REJECTED,
    ADD_DROP_REQUEST_DECIDED_ACTION,
    ADD_DROP_REQUEST_DECIDED_EVENT,
    ADD_DROP_REQUEST_FILED_ACTION,
    APPROVE_ADD_DROP,
    CREATE_ADD_DROP_REQUEST,
    READ_ADD_DROP_REQUEST,
    TAG_PENDING_ADD,
    TAG_PENDING_DROP,
)
from cbpupsis_shared.domains.iam import service as iam_service
from cbpupsis_shared.domains.iam.constants import ADMIN_GROUP
from cbpupsis_shared.domains.notifications.templates import render

STUDENT_URL = "/api/v1/student/enrollment/add-drop"
ADMIN_URL = "/api/v1/admin/add-drop"
LOGIN_URL = "/api/v1/auth/student/login"
PASSWORD = "StudentPassword123!"


async def _term(
    db: AsyncSession,
    *,
    year: str = "2026-2027",
    semester: str = "First Semester",
    current: bool = True,
    add_drop_open: bool = True,
) -> AcademicTerm:
    now = datetime.now(UTC)
    deadline = now + timedelta(days=7) if add_drop_open else now - timedelta(days=1)
    term = AcademicTerm(
        academic_year=year,
        semester=semester,
        start_date=date(2026, 8, 1),
        end_date=date(2027, 1, 1),
        is_current=current,
        regular_enrollment_start=now - timedelta(days=3),
        regular_enrollment_end=now + timedelta(days=3),
        add_drop_deadline=deadline,
        grade_submission_deadline=now + timedelta(days=30),
    )
    db.add(term)
    await db.flush()
    return term


async def _program(
    db: AsyncSession,
    *,
    code: str = "BSCS",
    department: str = "Computer Science",
    college: str = "CCIS",
    prescribed_load: Decimal = Decimal("18"),
) -> tuple[Program, Curriculum]:
    program = Program(
        code=code,
        name=f"{code} Program",
        department=department,
        college=college,
        total_units_required=Decimal("140"),
        is_active=True,
    )
    db.add(program)
    await db.flush()
    curriculum = Curriculum(
        program_id=program.id, curriculum_year="2026", is_active=True
    )
    db.add(curriculum)
    await db.flush()
    return program, curriculum


async def _course(
    db: AsyncSession,
    *,
    curriculum: Curriculum,
    code: str,
    title: str,
    units: Decimal = Decimal("3"),
    year_level: int = 3,
    semester: str = "First Semester",
) -> Course:
    course = Course(
        course_code=code,
        title=title,
        lecture_units=units,
        lab_units=Decimal("0"),
        is_active=True,
    )
    db.add(course)
    await db.flush()
    db.add(
        CurriculumCourse(
            curriculum_id=curriculum.id,
            course_id=course.id,
            year_level=year_level,
            semester=semester,
        )
    )
    await db.flush()
    return course


async def _section(
    db: AsyncSession,
    *,
    term: AcademicTerm,
    course: Course,
    name: str = "S1",
    day: int = 0,
    start: time = time(8, 0),
    end: time = time(11, 0),
    capacity: int = 40,
) -> CourseSection:
    section = CourseSection(
        academic_term_id=term.id,
        course_id=course.id,
        section_name=name,
        capacity=capacity,
        status="open",
    )
    db.add(section)
    await db.flush()
    db.add(
        SectionSchedule(
            section_id=section.id,
            day_of_week=day,
            start_time=start,
            end_time=end,
            schedule_type="lecture",
        )
    )
    await db.flush()
    return section


async def _student(
    db: AsyncSession,
    *,
    number: str,
    program: Program,
    curriculum: Curriculum,
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
            first_name="AddDrop",
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
            enrollment_status="enrolled",
        )
    )
    await db.flush()
    return user


async def _enroll(
    db: AsyncSession,
    *,
    student: User,
    term: AcademicTerm,
    sections: list[CourseSection],
) -> tuple[TermEnrollment, list[StudentCourseEnrollment], CorIssuance]:
    now = datetime.now(UTC)
    te = TermEnrollment(
        student_id=student.id,
        academic_term_id=term.id,
        enrollment_status="enrolled",
    )
    db.add(te)
    await db.flush()
    sces = []
    for s in sections:
        sce = StudentCourseEnrollment(
            term_enrollment_id=te.id,
            academic_term_id=term.id,
            section_id=s.id,
            status="enrolled",
        )
        db.add(sce)
        sces.append(sce)
    await db.flush()
    cor = CorIssuance(
        student_id=student.id,
        term_enrollment_id=te.id,
        verification_token_hash=uuid.uuid4().hex,
        status="CURRENT",
        issued_at=now,
    )
    db.add(cor)
    await db.flush()
    return te, sces, cor


async def _grant_action(db: AsyncSession, *, action: str, users: list[User]) -> None:
    suffix = uuid.uuid4().hex
    from cbpupsis_database.models.iam import Permission

    perm = await db.scalar(select(Permission).where(Permission.action == action))
    if perm is None:
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
    from cbpupsis_database.models.iam import Group

    admin_group = await db.scalar(select(Group).where(Group.name == ADMIN_GROUP))
    if admin_group is None:
        admin_group = await iam_service.create_group(db, name=ADMIN_GROUP)
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
async def add_drop_setup(db: AsyncSession):
    term = await _term(db, add_drop_open=True)
    program, curriculum = await _program(
        db, code="BSCS", department="Computer Science", college="CCIS"
    )

    # 5 courses enrolled initially (15 units)
    courses = []
    sections = []
    for i in range(1, 6):
        c = await _course(
            db, curriculum=curriculum, code=f"CS31{i}", title=f"Subject {i}"
        )
        courses.append(c)
        s = await _section(
            db,
            term=term,
            course=c,
            name=f"CS31{i}-1",
            day=i - 1,
            start=time(8, 0),
            end=time(11, 0),
        )
        sections.append(s)

    # Valid add section (Day 5, no conflict)
    c6 = await _course(db, curriculum=curriculum, code="CS316", title="Subject 6")
    s6 = await _section(
        db,
        term=term,
        course=c6,
        name="CS316-1",
        day=5,
        start=time(8, 0),
        end=time(11, 0),
    )

    # Conflicting section (same day/time as S1)
    c7 = await _course(
        db, curriculum=curriculum, code="CS317", title="Subject 7", year_level=4
    )
    s7 = await _section(
        db,
        term=term,
        course=c7,
        name="CS317-1",
        day=0,
        start=time(9, 0),
        end=time(12, 0),
    )

    # Full section (capacity 1)
    c8 = await _course(
        db, curriculum=curriculum, code="CS318", title="Subject 8", year_level=4
    )
    s8 = await _section(
        db,
        term=term,
        course=c8,
        name="CS318-1",
        day=5,
        start=time(13, 0),
        end=time(16, 0),
        capacity=1,
    )

    student = await _student(
        db,
        number="2026-00101-MN-0",
        program=program,
        curriculum=curriculum,
    )
    te, sces, cor = await _enroll(db, student=student, term=term, sections=sections)
    await db.commit()

    await _grant_action(
        db,
        action=CREATE_ADD_DROP_REQUEST,
        users=[student],
    )
    await _grant_action(
        db,
        action=READ_ADD_DROP_REQUEST,
        users=[student],
    )

    return {
        "term": term,
        "program": program,
        "curriculum": curriculum,
        "student": student,
        "courses": courses,
        "sections": sections,
        "s6": s6,
        "s7": s7,
        "s8": s8,
        "te": te,
        "sces": sces,
        "cor": cor,
    }


async def test_ac_enrl_004_1_file_request_success_pending_unchanged_enrollment(
    client: AsyncClient, db: AsyncSession, add_drop_setup
) -> None:
    """AC-ENRL-004.1: Filing creates a PENDING request, leaves enrollment unchanged."""
    s = add_drop_setup
    headers = await _student_headers(client, "2026-00101-MN-0")
    payload = {
        "add": [{"sectionId": str(s["s6"].id)}],
        "drop": [
            {
                "sectionId": str(s["sections"][4].id),
                "reason": "Schedule Conflict with Work",
            }
        ],
        "comments": "Requesting section change due to shift adjustment",
    }
    # Snapshot all enrollment rows before filing
    before_rows = [
        await repository.get_student_course_enrollment(db, s["te"].id, sec.id)
        for sec in s["sections"]
    ]
    before_snapshots = [
        {
            "id": r.id,
            "term_enrollment_id": r.term_enrollment_id,
            "academic_term_id": r.academic_term_id,
            "section_id": r.section_id,
            "status": r.status,
            "created_at": r.created_at,
        }
        for r in before_rows
        if r is not None
    ]

    response = await client.post(STUDENT_URL, headers=headers, json=payload)
    assert response.status_code == 201, response.text
    data = response.json()

    assert data["state"] == "PENDING"
    assert Decimal(data["unitsBefore"]) == Decimal("15.0")
    assert Decimal(data["unitsAfter"]) == Decimal("15.0")
    assert data["comments"] == "Requesting section change due to shift adjustment"
    assert data["remarks"] is None
    assert data["version"] == 1
    assert len(data["courses"]) == 2

    # Check tags
    courses = {c["sectionId"]: c for c in data["courses"]}
    assert courses[str(s["s6"].id)]["tag"] == TAG_PENDING_ADD
    assert courses[str(s["sections"][4].id)]["tag"] == TAG_PENDING_DROP
    assert courses[str(s["sections"][4].id)]["reason"] == "Schedule Conflict with Work"

    # All enrollment rows in database remain completely identical field-by-field
    after_rows = [
        await repository.get_student_course_enrollment(db, s["te"].id, sec.id)
        for sec in s["sections"]
    ]
    after_snapshots = [
        {
            "id": r.id,
            "term_enrollment_id": r.term_enrollment_id,
            "academic_term_id": r.academic_term_id,
            "section_id": r.section_id,
            "status": r.status,
            "created_at": r.created_at,
        }
        for r in after_rows
        if r is not None
    ]
    assert before_snapshots == after_snapshots

    added_sec = await repository.get_student_course_enrollment(
        db, s["te"].id, s["s6"].id
    )
    assert added_sec is None


async def test_ac_enrl_004_2_chair_or_dean_approves_updates_enrollment_and_assessment(
    client: AsyncClient, db: AsyncSession, add_drop_setup
) -> None:
    """AC-ENRL-004.2: Approval mutates enrollment, updates COR, and marks APPROVED."""
    s = add_drop_setup
    headers = await _student_headers(client, "2026-00101-MN-0")
    payload = {
        "add": [{"sectionId": str(s["s6"].id)}],
        "drop": [
            {
                "sectionId": str(s["sections"][4].id),
                "reason": "Shift adjustment",
            }
        ],
    }
    file_resp = await client.post(STUDENT_URL, headers=headers, json=payload)
    request_id = file_resp.json()["requestId"]

    chair, chair_headers = await _admin(
        db, position="chairperson", department="Computer Science", college="CCIS"
    )
    await _grant_action(db, action=APPROVE_ADD_DROP, users=[chair])
    await _grant_action(db, action=READ_ADD_DROP_REQUEST, users=[chair])

    decide_resp = await client.patch(
        f"{ADMIN_URL}/{request_id}",
        headers=chair_headers,
        json={"decision": "APPROVE", "remarks": "Approved as requested.", "version": 1},
    )
    assert decide_resp.status_code == 200, decide_resp.text
    decide_data = decide_resp.json()
    assert decide_data["state"] == ADD_DROP_APPROVED
    assert decide_data["remarks"] == "Approved as requested."

    # Verify database updates
    dropped_sec = await repository.get_student_course_enrollment(
        db, s["te"].id, s["sections"][4].id
    )
    assert dropped_sec is not None
    assert dropped_sec.status == "dropped"

    added_sec = await repository.get_student_course_enrollment(
        db, s["te"].id, s["s6"].id
    )
    assert added_sec is not None
    assert added_sec.status == "enrolled"

    # Note on billing assessment & COR reissue:
    # Staging assessment (billing.stage_assessment) and COR reissue
    # (certificate.stage_reissue) integrate via PR #214 (Issue #100).
    # Assessment staging assertions will be added upon merging PR #214.


async def test_ac_enrl_004_2_failure_rollback_preserves_pending(
    client: AsyncClient, db: AsyncSession, add_drop_setup
) -> None:
    """AC-ENRL-004.2: Approval failure rolls back; request remains PENDING."""
    s = add_drop_setup
    headers = await _student_headers(client, "2026-00101-MN-0")
    payload = {
        "add": [{"sectionId": str(s["s6"].id)}],
        "drop": [{"sectionId": str(s["sections"][4].id), "reason": "Conflict"}],
    }
    file_resp = await client.post(STUDENT_URL, headers=headers, json=payload)
    request_id = file_resp.json()["requestId"]

    chair, chair_headers = await _admin(
        db, position="chairperson", department="Computer Science", college="CCIS"
    )
    await _grant_action(db, action=APPROVE_ADD_DROP, users=[chair])
    await _grant_action(db, action=READ_ADD_DROP_REQUEST, users=[chair])

    te_id = s["te"].id
    drop_section_id = s["sections"][4].id
    s6_id = s["s6"].id

    # Force failure during enrollment mutation
    with (
        patch(
            "cbpupsis_shared.domains.enrollment.repository.add_course_enrollments",
            side_effect=RuntimeError("Forced failure during enrollment update"),
        ),
        pytest.raises(RuntimeError),
    ):
        await client.patch(
            f"{ADMIN_URL}/{request_id}",
            headers=chair_headers,
            json={"decision": "APPROVE", "remarks": "Approved.", "version": 1},
        )

    # Ensure DB rollback preserved original state
    req = await repository.get_add_drop_request(db, uuid.UUID(request_id))
    assert req is not None
    assert req.state == ADD_DROP_PENDING

    dropped_sec = await repository.get_student_course_enrollment(
        db, te_id, drop_section_id
    )
    assert dropped_sec.status == "enrolled"

    added_sec = await repository.get_student_course_enrollment(db, te_id, s6_id)
    assert added_sec is None


async def test_ac_enrl_004_3_reject_leaves_enrollment_identical_with_remarks(
    client: AsyncClient, db: AsyncSession, add_drop_setup
) -> None:
    """AC-ENRL-004.3: Rejection marks REJECTED and leaves enrollments identical."""
    s = add_drop_setup
    headers = await _student_headers(client, "2026-00101-MN-0")
    payload = {
        "add": [{"sectionId": str(s["s6"].id)}],
        "drop": [{"sectionId": str(s["sections"][4].id), "reason": "Conflict"}],
    }
    file_resp = await client.post(STUDENT_URL, headers=headers, json=payload)
    request_id = file_resp.json()["requestId"]

    dean, dean_headers = await _admin(
        db, position="dean", department="Computer Science", college="CCIS"
    )
    await _grant_action(db, action=APPROVE_ADD_DROP, users=[dean])
    await _grant_action(db, action=READ_ADD_DROP_REQUEST, users=[dean])

    decide_resp = await client.patch(
        f"{ADMIN_URL}/{request_id}",
        headers=dean_headers,
        json={
            "decision": "REJECT",
            "remarks": "Not justified at this time.",
            "version": 1,
        },
    )
    assert decide_resp.status_code == 200, decide_resp.text
    assert decide_resp.json()["state"] == ADD_DROP_REJECTED
    assert decide_resp.json()["remarks"] == "Not justified at this time."

    # Enrollment untouched
    dropped_sec = await repository.get_student_course_enrollment(
        db, s["te"].id, s["sections"][4].id
    )
    assert dropped_sec.status == "enrolled"


async def test_ac_enrl_004_4_decision_queues_notification(
    client: AsyncClient, db: AsyncSession, add_drop_setup
) -> None:
    """AC-ENRL-004.4: Deciding a request queues outbox notification."""
    s = add_drop_setup
    headers = await _student_headers(client, "2026-00101-MN-0")
    payload = {
        "add": [{"sectionId": str(s["s6"].id)}],
        "drop": [{"sectionId": str(s["sections"][4].id), "reason": "Conflict"}],
    }
    file_resp = await client.post(STUDENT_URL, headers=headers, json=payload)
    request_id = file_resp.json()["requestId"]

    dean, dean_headers = await _admin(
        db, position="dean", department="Computer Science", college="CCIS"
    )
    await _grant_action(db, action=APPROVE_ADD_DROP, users=[dean])
    await _grant_action(db, action=READ_ADD_DROP_REQUEST, users=[dean])

    remarks = "Approved by College Dean."
    await client.patch(
        f"{ADMIN_URL}/{request_id}",
        headers=dean_headers,
        json={"decision": "APPROVE", "remarks": remarks, "version": 1},
    )

    messages = list(
        await db.scalars(
            select(OutboxMessage).where(
                OutboxMessage.event_name == ADD_DROP_REQUEST_DECIDED_EVENT
            )
        )
    )
    assert len(messages) == 1
    msg = messages[0]
    assert msg.payload["user_id"] == str(s["student"].id)
    assert msg.payload["state"] == ADD_DROP_APPROVED
    assert msg.payload["remarks"] == remarks

    rendered = render(Event(name=msg.event_name, payload=msg.payload))
    assert "Add and drop request" in rendered.title
    assert remarks in rendered.body


async def test_ac_enrl_004_5_reject_without_remarks_422(
    client: AsyncClient, db: AsyncSession, add_drop_setup
) -> None:
    """AC-ENRL-004.5: Rejection without remarks returns 422."""
    s = add_drop_setup
    headers = await _student_headers(client, "2026-00101-MN-0")
    file_resp = await client.post(
        STUDENT_URL,
        headers=headers,
        json={
            "add": [{"sectionId": str(s["s6"].id)}],
            "drop": [{"sectionId": str(s["sections"][4].id), "reason": "Conflict"}],
        },
    )
    request_id = file_resp.json()["requestId"]

    chair, chair_headers = await _admin(
        db, position="chairperson", department="Computer Science", college="CCIS"
    )
    await _grant_action(db, action=APPROVE_ADD_DROP, users=[chair])
    await _grant_action(db, action=READ_ADD_DROP_REQUEST, users=[chair])

    # Blank remarks
    resp = await client.patch(
        f"{ADMIN_URL}/{request_id}",
        headers=chair_headers,
        json={"decision": "REJECT", "remarks": "   ", "version": 1},
    )
    assert resp.status_code == 422
    assert resp.json()["code"] == "DECISION_REMARKS_REQUIRED"

    # None remarks
    resp2 = await client.patch(
        f"{ADMIN_URL}/{request_id}",
        headers=chair_headers,
        json={"decision": "REJECT", "version": 1},
    )
    assert resp2.status_code == 422
    assert resp2.json()["code"] == "DECISION_REMARKS_REQUIRED"


async def test_ac_enrl_004_6_already_decided_refused_409(
    client: AsyncClient, db: AsyncSession, add_drop_setup
) -> None:
    """AC-ENRL-004.6: Replay on already decided request returns 409."""
    s = add_drop_setup
    headers = await _student_headers(client, "2026-00101-MN-0")
    file_resp = await client.post(
        STUDENT_URL,
        headers=headers,
        json={
            "add": [{"sectionId": str(s["s6"].id)}],
            "drop": [{"sectionId": str(s["sections"][4].id), "reason": "Conflict"}],
        },
    )
    request_id = file_resp.json()["requestId"]

    chair, chair_headers = await _admin(
        db, position="chairperson", department="Computer Science", college="CCIS"
    )
    await _grant_action(db, action=APPROVE_ADD_DROP, users=[chair])
    await _grant_action(db, action=READ_ADD_DROP_REQUEST, users=[chair])

    first = await client.patch(
        f"{ADMIN_URL}/{request_id}",
        headers=chair_headers,
        json={"decision": "APPROVE", "remarks": "Approved.", "version": 1},
    )
    assert first.status_code == 200

    second = await client.patch(
        f"{ADMIN_URL}/{request_id}",
        headers=chair_headers,
        json={"decision": "APPROVE", "remarks": "Approved again.", "version": 2},
    )
    assert second.status_code == 409
    assert second.json()["code"] == "REQUEST_ALREADY_DECIDED"


async def test_ac_enrl_004_7_closed_period_refused_409(
    client: AsyncClient, db: AsyncSession, add_drop_setup
) -> None:
    """AC-ENRL-004.7: Filing outside add/drop deadline returns 409."""
    s = add_drop_setup
    # Close the add/drop period
    s["term"].add_drop_deadline = datetime.now(UTC) - timedelta(days=2)
    await db.commit()

    headers = await _student_headers(client, "2026-00101-MN-0")
    response = await client.post(
        STUDENT_URL,
        headers=headers,
        json={
            "add": [{"sectionId": str(s["s6"].id)}],
            "drop": [{"sectionId": str(s["sections"][4].id), "reason": "Conflict"}],
        },
    )
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "ENROLL_ADD_DROP_PERIOD_CLOSED"
    assert "periodStart" in response.json()["details"]
    assert "periodEnd" in response.json()["details"]


async def test_ac_enrl_004_8_below_minimum_load_refused_409(
    client: AsyncClient, db: AsyncSession, add_drop_setup
) -> None:
    """AC-ENRL-004.8: Drop resulting in load < 12 units returns 409."""
    s = add_drop_setup
    # Set student to 12 units enrolled by removing 5th course from enrollment
    sce5 = await repository.get_student_course_enrollment(
        db, s["te"].id, s["sections"][4].id
    )
    await db.delete(sce5)
    await db.commit()

    headers = await _student_headers(client, "2026-00101-MN-0")
    # Dropping one 3-unit course leaves 9.0 units, which is < 12.0
    response = await client.post(
        STUDENT_URL,
        headers=headers,
        json={
            "add": [],
            "drop": [{"sectionId": str(s["sections"][3].id), "reason": "Conflict"}],
        },
    )
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "ENROLL_BELOW_MINIMUM_LOAD"
    assert Decimal(response.json()["details"]["minimumLoad"]) == Decimal("12.0")
    assert Decimal(response.json()["details"]["unitsAfter"]) == Decimal("9.0")


async def test_ac_enrl_004_9_student_cannot_read_other_request_404(
    client: AsyncClient, db: AsyncSession, add_drop_setup
) -> None:
    """AC-ENRL-004.9: Reading another student's request returns 404 (anti-probing)."""
    s = add_drop_setup
    headers1 = await _student_headers(client, "2026-00101-MN-0")
    file_resp = await client.post(
        STUDENT_URL,
        headers=headers1,
        json={
            "add": [{"sectionId": str(s["s6"].id)}],
            "drop": [{"sectionId": str(s["sections"][4].id), "reason": "Conflict"}],
        },
    )
    request_id = file_resp.json()["requestId"]

    # Student 2
    student2 = await _student(
        db,
        number="2026-00102-MN-0",
        program=s["program"],
        curriculum=s["curriculum"],
    )
    await _enroll(db, student=student2, term=s["term"], sections=s["sections"])
    await db.commit()
    await _grant_action(db, action=READ_ADD_DROP_REQUEST, users=[student2])

    headers2 = await _student_headers(client, "2026-00102-MN-0")
    resp = await client.get(f"{STUDENT_URL}/{request_id}", headers=headers2)
    assert resp.status_code == 404


async def test_ac_enrl_004_10_addition_validation_refusals(
    client: AsyncClient, db: AsyncSession, add_drop_setup
) -> None:
    """AC-ENRL-004.10: Reused submit checks refuse additions without request rows."""
    s = add_drop_setup
    headers = await _student_headers(client, "2026-00101-MN-0")

    # 1. Section full: occupy S8
    occupier = await _student(
        db,
        number="2026-00199-MN-0",
        program=s["program"],
        curriculum=s["curriculum"],
    )
    await _enroll(db, student=occupier, term=s["term"], sections=[s["s8"]])
    await db.commit()

    full_resp = await client.post(
        STUDENT_URL,
        headers=headers,
        json={
            "add": [{"sectionId": str(s["s8"].id)}],
            "drop": [{"sectionId": str(s["sections"][4].id), "reason": "Conflict"}],
        },
    )
    assert full_resp.status_code == 409
    assert full_resp.json()["code"] == "ENROLL_SECTION_FULL"

    # 2. Schedule conflict: S7 conflicts with S1 (Monday 8-11 vs Monday 9-12)
    conflict_resp = await client.post(
        STUDENT_URL,
        headers=headers,
        json={
            "add": [{"sectionId": str(s["s7"].id)}],
            "drop": [{"sectionId": str(s["sections"][4].id), "reason": "Conflict"}],
        },
    )
    assert conflict_resp.status_code == 409
    assert conflict_resp.json()["code"] == "ENROLL_SCHEDULE_CONFLICT"

    # 3. Unit ceiling exceeded: Prescribed load is 18. Currently 15 units.
    # Adding two 3-unit courses (CS316 and a new CS319) = 15 + 6 = 21 units > 18.
    c9 = await _course(
        db, curriculum=s["curriculum"], code="CS319", title="Subject 9", year_level=4
    )
    s9 = await _section(
        db,
        term=s["term"],
        course=c9,
        name="CS319-1",
        day=5,
        start=time(13, 0),
        end=time(16, 0),
    )
    await db.commit()

    ceiling_resp = await client.post(
        STUDENT_URL,
        headers=headers,
        json={
            "add": [
                {"sectionId": str(s["s6"].id)},
                {"sectionId": str(s9.id)},
            ],
            "drop": [],
        },
    )
    assert ceiling_resp.status_code == 409
    assert ceiling_resp.json()["code"] == "ENROLL_UNIT_CEILING_EXCEEDED"

    # Invariant: No request rows were persisted
    requests = list(await db.scalars(select(AddDropRequest)))
    assert len(requests) == 0


async def test_ac_enrl_004_11_chair_scope_enforcement(
    client: AsyncClient, db: AsyncSession, add_drop_setup
) -> None:
    """AC-ENRL-004.11: Chairperson scope enforcement (own-dept only, 404 outside)."""
    s = add_drop_setup
    headers = await _student_headers(client, "2026-00101-MN-0")
    file_resp = await client.post(
        STUDENT_URL,
        headers=headers,
        json={
            "add": [{"sectionId": str(s["s6"].id)}],
            "drop": [{"sectionId": str(s["sections"][4].id), "reason": "Conflict"}],
        },
    )
    request_id = file_resp.json()["requestId"]

    # Math Chair (different dept)
    math_chair, math_headers = await _admin(
        db, position="chairperson", department="Mathematics", college="CCIS"
    )
    await _grant_action(db, action=READ_ADD_DROP_REQUEST, users=[math_chair])
    await _grant_action(db, action=APPROVE_ADD_DROP, users=[math_chair])

    # CS Chair (own dept)
    cs_chair, cs_headers = await _admin(
        db, position="chairperson", department="Computer Science", college="CCIS"
    )
    await _grant_action(db, action=READ_ADD_DROP_REQUEST, users=[cs_chair])
    await _grant_action(db, action=APPROVE_ADD_DROP, users=[cs_chair])

    # Math Chair listing sees nothing
    math_list = await client.get(ADMIN_URL, headers=math_headers)
    assert math_list.status_code == 200
    assert len(math_list.json()) == 0

    # CS Chair listing sees request
    cs_list = await client.get(ADMIN_URL, headers=cs_headers)
    assert cs_list.status_code == 200
    assert len(cs_list.json()) == 1

    # Math Chair patch gets 404 (anti-probing)
    math_patch = await client.patch(
        f"{ADMIN_URL}/{request_id}",
        headers=math_headers,
        json={"decision": "APPROVE", "remarks": "Approved.", "version": 1},
    )
    assert math_patch.status_code == 404


async def test_ac_enrl_004_11_dean_scope_enforcement(
    client: AsyncClient, db: AsyncSession, add_drop_setup
) -> None:
    """AC-ENRL-004.11: Dean scope enforcement (own-college only, 404 outside)."""
    s = add_drop_setup
    headers = await _student_headers(client, "2026-00101-MN-0")
    file_resp = await client.post(
        STUDENT_URL,
        headers=headers,
        json={
            "add": [{"sectionId": str(s["s6"].id)}],
            "drop": [{"sectionId": str(s["sections"][4].id), "reason": "Conflict"}],
        },
    )
    request_id = file_resp.json()["requestId"]

    # Other college Dean
    coed_dean, coed_headers = await _admin(
        db, position="dean", department="Elementary Education", college="COED"
    )
    await _grant_action(db, action=READ_ADD_DROP_REQUEST, users=[coed_dean])
    await _grant_action(db, action=APPROVE_ADD_DROP, users=[coed_dean])

    # CCIS Dean
    ccis_dean, ccis_headers = await _admin(
        db, position="dean", department="Computer Science", college="CCIS"
    )
    await _grant_action(db, action=READ_ADD_DROP_REQUEST, users=[ccis_dean])
    await _grant_action(db, action=APPROVE_ADD_DROP, users=[ccis_dean])

    # COED Dean list empty
    coed_list = await client.get(ADMIN_URL, headers=coed_headers)
    assert len(coed_list.json()) == 0

    # CCIS Dean list sees request
    ccis_list = await client.get(ADMIN_URL, headers=ccis_headers)
    assert len(ccis_list.json()) == 1

    # COED Dean patch gets 404
    coed_patch = await client.patch(
        f"{ADMIN_URL}/{request_id}",
        headers=coed_headers,
        json={"decision": "APPROVE", "remarks": "Approved.", "version": 1},
    )
    assert coed_patch.status_code == 404


async def test_ac_enrl_004_11_registrar_read_only_403(
    client: AsyncClient, db: AsyncSession, add_drop_setup
) -> None:
    """AC-ENRL-004.11: Registrar can list all requests but is denied PATCH (403)."""
    s = add_drop_setup
    headers = await _student_headers(client, "2026-00101-MN-0")
    file_resp = await client.post(
        STUDENT_URL,
        headers=headers,
        json={
            "add": [{"sectionId": str(s["s6"].id)}],
            "drop": [{"sectionId": str(s["sections"][4].id), "reason": "Conflict"}],
        },
    )
    request_id = file_resp.json()["requestId"]

    registrar, reg_headers = await _admin(db, position="registrar")
    await _grant_action(db, action=READ_ADD_DROP_REQUEST, users=[registrar])

    # Registrar can read
    reg_list = await client.get(ADMIN_URL, headers=reg_headers)
    assert reg_list.status_code == 200
    assert len(reg_list.json()) == 1

    # Registrar cannot patch (403 because lacks ApproveAddDrop)
    reg_patch = await client.patch(
        f"{ADMIN_URL}/{request_id}",
        headers=reg_headers,
        json={"decision": "APPROVE", "remarks": "Approved.", "version": 1},
    )
    assert reg_patch.status_code == 403
    assert "Missing required permission" in reg_patch.json()["detail"]


async def test_ac_enrl_004_student_admin_patch_forbidden_403(
    client: AsyncClient, db: AsyncSession, add_drop_setup
) -> None:
    """A student attempting to call admin PATCH endpoint is rejected with 403."""
    headers = await _student_headers(client, "2026-00101-MN-0")
    resp = await client.patch(
        f"{ADMIN_URL}/{uuid.uuid4()}",
        headers=headers,
        json={"decision": "APPROVE", "version": 1},
    )
    assert resp.status_code == 403
    assert resp.json()["code"] == "AUTH_INSUFFICIENT_ROLE"


async def test_ac_enrl_004_audit_rows_logged(
    client: AsyncClient, db: AsyncSession, add_drop_setup
) -> None:
    """Filing and decision stage transactional audit entries."""
    s = add_drop_setup
    headers = await _student_headers(client, "2026-00101-MN-0")
    file_resp = await client.post(
        STUDENT_URL,
        headers=headers,
        json={
            "add": [{"sectionId": str(s["s6"].id)}],
            "drop": [{"sectionId": str(s["sections"][4].id), "reason": "Conflict"}],
        },
    )
    request_id = file_resp.json()["requestId"]

    # Verify filing audit
    file_audits = list(
        await db.scalars(
            select(AuditEntry).where(AuditEntry.action == ADD_DROP_REQUEST_FILED_ACTION)
        )
    )
    assert len(file_audits) == 1
    assert file_audits[0].actor_id == s["student"].id
    assert file_audits[0].target_id == str(request_id)
    assert file_audits[0].new_state["state"] == ADD_DROP_PENDING

    # Decide
    chair, chair_headers = await _admin(
        db, position="chairperson", department="Computer Science", college="CCIS"
    )
    await _grant_action(db, action=APPROVE_ADD_DROP, users=[chair])
    await _grant_action(db, action=READ_ADD_DROP_REQUEST, users=[chair])

    await client.patch(
        f"{ADMIN_URL}/{request_id}",
        headers=chair_headers,
        json={"decision": "APPROVE", "remarks": "Approved by Chair", "version": 1},
    )

    decide_audits = list(
        await db.scalars(
            select(AuditEntry).where(
                AuditEntry.action == ADD_DROP_REQUEST_DECIDED_ACTION
            )
        )
    )
    assert len(decide_audits) == 1
    assert decide_audits[0].actor_id == chair.id
    assert decide_audits[0].new_state["state"] == ADD_DROP_APPROVED


async def test_student_list_and_get_own_requests(
    client: AsyncClient, db: AsyncSession, add_drop_setup
) -> None:
    """Student can list and fetch their own requests."""
    s = add_drop_setup
    headers = await _student_headers(client, "2026-00101-MN-0")
    file_resp = await client.post(
        STUDENT_URL,
        headers=headers,
        json={
            "add": [{"sectionId": str(s["s6"].id)}],
            "drop": [{"sectionId": str(s["sections"][4].id), "reason": "Conflict"}],
        },
    )
    request_id = file_resp.json()["requestId"]

    list_resp = await client.get(STUDENT_URL, headers=headers)
    assert list_resp.status_code == 200
    assert len(list_resp.json()) == 1
    assert list_resp.json()[0]["requestId"] == request_id

    get_resp = await client.get(f"{STUDENT_URL}/{request_id}", headers=headers)
    assert get_resp.status_code == 200
    assert get_resp.json()["requestId"] == request_id


async def test_ac_enrl_004_multiple_pending_requests_refused(
    client: AsyncClient, db: AsyncSession, add_drop_setup
) -> None:
    """A student cannot file a second add/drop request while one is already PENDING."""
    s = add_drop_setup
    headers = await _student_headers(client, "2026-00101-MN-0")
    first = await client.post(
        STUDENT_URL,
        headers=headers,
        json={
            "add": [{"sectionId": str(s["s6"].id)}],
            "drop": [{"sectionId": str(s["sections"][4].id), "reason": "Conflict"}],
        },
    )
    assert first.status_code == 201

    second = await client.post(
        STUDENT_URL,
        headers=headers,
        json={
            "add": [{"sectionId": str(s["s6"].id)}],
            "drop": [{"sectionId": str(s["sections"][4].id), "reason": "Conflict"}],
        },
    )
    assert second.status_code == 409
    assert second.json()["code"] == "REQUEST_ALREADY_PENDING"


async def test_ac_enrl_004_holds_block_filing(
    client: AsyncClient, db: AsyncSession, add_drop_setup
) -> None:
    """Financial and administrative holds block add/drop request filing."""
    s = add_drop_setup
    headers = await _student_headers(client, "2026-00101-MN-0")
    profile = await db.scalar(
        select(StudentProfile).where(StudentProfile.user_id == s["student"].id)
    )
    assert profile is not None

    # 1. Financial hold
    profile.has_financial_hold = True
    await db.commit()

    resp = await client.post(
        STUDENT_URL,
        headers=headers,
        json={
            "add": [{"sectionId": str(s["s6"].id)}],
            "drop": [{"sectionId": str(s["sections"][4].id), "reason": "Conflict"}],
        },
    )
    assert resp.status_code == 409
    assert resp.json()["code"] == "ENROLL_FINANCIAL_HOLD"

    # 2. Administrative hold
    profile.has_financial_hold = False
    profile.has_academic_hold = True
    await db.commit()

    resp = await client.post(
        STUDENT_URL,
        headers=headers,
        json={
            "add": [{"sectionId": str(s["s6"].id)}],
            "drop": [{"sectionId": str(s["sections"][4].id), "reason": "Conflict"}],
        },
    )
    assert resp.status_code == 409
    assert resp.json()["code"] == "ENROLL_HOLD_ACTIVE"


async def test_ac_enrl_004_blocked_standing_refused(
    client: AsyncClient, db: AsyncSession, add_drop_setup
) -> None:
    """Students on blocked academic standing cannot file add/drop requests."""
    s = add_drop_setup
    headers = await _student_headers(client, "2026-00101-MN-0")
    db.add(
        StudentTermGPA(
            student_id=s["student"].id,
            academic_term_id=s["term"].id,
            total_gpa_units=Decimal("18"),
            term_gpa=Decimal("5"),
            cumulative_gpa=Decimal("5"),
            academic_standing="University Dismissal",
        )
    )
    await db.commit()

    resp = await client.post(
        STUDENT_URL,
        headers=headers,
        json={
            "add": [{"sectionId": str(s["s6"].id)}],
            "drop": [{"sectionId": str(s["sections"][4].id), "reason": "Conflict"}],
        },
    )
    assert resp.status_code == 409
    assert resp.json()["code"] == "ENROLL_STANDING_BLOCKED"


async def test_ac_enrl_004_non_curriculum_addition_refused(
    client: AsyncClient, db: AsyncSession, add_drop_setup
) -> None:
    """Adding a course that does not belong to the student's curriculum returns 404."""
    s = add_drop_setup
    headers = await _student_headers(client, "2026-00101-MN-0")

    # Create another curriculum with course not in student's curriculum
    _other_prog, other_curr = await _program(
        db, code="BSIT", department="Information Technology", college="CCIS"
    )
    other_course = await _course(
        db, curriculum=other_curr, code="IT311", title="IT Subject"
    )
    other_section = await _section(
        db,
        term=s["term"],
        course=other_course,
        name="IT311-1",
        day=4,
        start=time(14),
        end=time(17),
    )
    await db.commit()

    resp = await client.post(
        STUDENT_URL,
        headers=headers,
        json={
            "add": [{"sectionId": str(other_section.id)}],
            "drop": [{"sectionId": str(s["sections"][4].id), "reason": "Conflict"}],
        },
    )
    assert resp.status_code == 404


async def test_ac_enrl_004_duplicate_course_refused(
    client: AsyncClient, db: AsyncSession, add_drop_setup
) -> None:
    """Adding a second section of an already enrolled course is refused."""
    s = add_drop_setup
    headers = await _student_headers(client, "2026-00101-MN-0")

    # s["courses"][0] is CS311, already enrolled in s["sections"][0]
    sec2 = await _section(
        db,
        term=s["term"],
        course=s["courses"][0],
        name="CS311-2",
        day=4,
        start=time(14),
        end=time(17),
    )
    await db.commit()

    # Try to add sec2 without dropping s["sections"][0]
    resp = await client.post(
        STUDENT_URL,
        headers=headers,
        json={
            "add": [{"sectionId": str(sec2.id)}],
            "drop": [{"sectionId": str(s["sections"][4].id), "reason": "Conflict"}],
        },
    )
    assert resp.status_code == 409
    assert resp.json()["code"] == "ENROLL_DUPLICATE_COURSE"


async def test_ac_enrl_004_approval_seat_race_section_full_409(
    client: AsyncClient, db: AsyncSession, add_drop_setup
) -> None:
    """If a section fills up before approval, approving fails with 409."""

    s = add_drop_setup
    headers = await _student_headers(client, "2026-00101-MN-0")

    # File request adding S8 (capacity 1)
    file_resp = await client.post(
        STUDENT_URL,
        headers=headers,
        json={
            "add": [{"sectionId": str(s["s8"].id)}],
            "drop": [{"sectionId": str(s["sections"][4].id), "reason": "Conflict"}],
        },
    )
    assert file_resp.status_code == 201
    request_id = file_resp.json()["requestId"]

    # In the meantime, another student takes the last seat in S8
    other = await _student(
        db,
        number="2026-00198-MN-0",
        program=s["program"],
        curriculum=s["curriculum"],
    )
    await _enroll(db, student=other, term=s["term"], sections=[s["s8"]])
    await db.commit()

    # Chairperson attempts to APPROVE
    chair, chair_headers = await _admin(
        db, position="chairperson", department="Computer Science", college="CCIS"
    )
    await _grant_action(db, action=APPROVE_ADD_DROP, users=[chair])
    await _grant_action(db, action=READ_ADD_DROP_REQUEST, users=[chair])

    decide_resp = await client.patch(
        f"{ADMIN_URL}/{request_id}",
        headers=chair_headers,
        json={"decision": "APPROVE", "remarks": "Approved.", "version": 1},
    )
    assert decide_resp.status_code == 409
    assert decide_resp.json()["code"] == "ENROLL_SECTION_FULL"
