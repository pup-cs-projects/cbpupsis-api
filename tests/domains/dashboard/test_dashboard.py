"""Acceptance and integration tests for FR-002 / US-101 Landing View Dashboard.

Validates AC-101.1 through AC-101.7, business rules BR-AUTH-005, BR-AUTH-007,
BR-STAT-003, BR-ENRL-001, Audit 2.3, bounded query count, partial failure resilience,
error scenarios, and performance.
"""

from __future__ import annotations

import ast
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from httpx import AsyncClient
from sqlalchemy import delete, event, select
from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_api_student.domains.dashboard import service as dashboard_service
from cbpupsis_api_student.domains.dashboard.constants import READ_DASHBOARD
from cbpupsis_database.models.academics import AcademicEvent, AcademicTerm
from cbpupsis_database.models.announcements import Announcement
from cbpupsis_database.models.audit import AuthAuditLog
from cbpupsis_database.models.billing import StudentBillingAccount
from cbpupsis_database.models.curriculum import (
    Course,
    Curriculum,
    CurriculumCourse,
    Program,
)
from cbpupsis_database.models.enrollment import (
    OverloadRequest,
    StudentCourseEnrollment,
    TermEnrollment,
)
from cbpupsis_database.models.grading import StudentTermGPA
from cbpupsis_database.models.iam import Group, Role
from cbpupsis_database.models.sections import CourseSection
from cbpupsis_database.models.users import StudentProfile, User, UserProfile
from cbpupsis_shared.domains.auth.security import hash_password
from cbpupsis_shared.domains.iam import service as iam_service
from cbpupsis_shared.domains.iam.constants import STUDENT_GROUP
from scripts.seed_iam import seed as seed_iam

DASHBOARD_URL = "/api/v1/student/dashboard"
LOGIN_URL = "/api/v1/auth/student/login"
TEST_PASSWORD = "StudentPassword123!"


async def _create_student(
    db: AsyncSession,
    *,
    student_number: str,
    first_name: str = "Juan",
    last_name: str = "Dela Cruz",
    email: str | None = None,
    program_id: uuid.UUID | None = None,
    curriculum_id: uuid.UUID | None = None,
    year_level: int = 3,
    enrollment_status: str = "ENROLLED",
    has_financial_hold: bool = False,
    has_academic_hold: bool = False,
) -> tuple[User, str]:
    """Helper to create a full student user and active session token."""
    email = email or f"student_{student_number.replace('-', '_')}@example.com"
    user = User(
        email=email,
        password_hash=hash_password(TEST_PASSWORD),
        is_active=True,
        display_name=f"{first_name} {last_name}",
    )
    db.add(user)
    await db.flush()

    birthdate = date(2003, 5, 14)
    db.add(
        UserProfile(
            user_id=user.id,
            first_name=first_name,
            last_name=last_name,
            birthdate=birthdate,
        )
    )

    if program_id is None:
        prog = Program(
            code=f"BSCS_{str(uuid.uuid4())[:4]}",
            name="BS in Computer Science",
            department="Computer Science",
            total_units_required=Decimal("140.0"),
            is_active=True,
        )
        db.add(prog)
        await db.flush()
        program_id = prog.id

    db.add(
        StudentProfile(
            user_id=user.id,
            student_number=student_number,
            program_id=program_id,
            curriculum_id=curriculum_id,
            year_level=year_level,
            enrollment_status=enrollment_status,
            has_financial_hold=has_financial_hold,
            has_academic_hold=has_academic_hold,
        )
    )
    await db.commit()
    return user, TEST_PASSWORD


async def _grant_dashboard(db: AsyncSession, grant, user_id: uuid.UUID) -> None:
    """Grant ``ReadDashboard`` idempotently across multiple students in one test."""
    group_id = await db.scalar(
        select(Group.id).where(Group.name == f"group-{READ_DASHBOARD}")
    )
    if group_id is None:
        await grant(user_id, READ_DASHBOARD)
    else:
        await iam_service.add_user_to_group(db, user_id, group_id)


async def _get_student_headers(
    client: AsyncClient, student_number: str
) -> dict[str, str]:
    login_resp = await client.post(
        LOGIN_URL,
        json={
            "student_number": student_number,
            "birthdate": "2003-05-14",
            "password": TEST_PASSWORD,
        },
    )
    assert login_resp.status_code == 200, f"Login failed: {login_resp.text}"
    token = login_resp.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
class TestStudentDashboardAcceptanceCriteria:
    """Covers Acceptance Criteria AC-101.1 through AC-101.7."""

    async def test_ac_101_1_identity_block(
        self, client: AsyncClient, db: AsyncSession, grant
    ) -> None:
        """AC-101.1: A student holds a session | The dashboard is opened |

        Shows name, student number, programme, year level, enrollment status, term.
        """
        student_num = "2021-00456-MN-0"
        user, _ = await _create_student(
            db,
            student_number=student_num,
            first_name="Juan A.",
            last_name="Dela Cruz",
            year_level=3,
            enrollment_status="ENROLLED",
        )
        await _grant_dashboard(db, grant, user.id)
        headers = await _get_student_headers(client, student_num)

        resp = await client.get(DASHBOARD_URL, headers=headers)
        assert resp.status_code == 200, resp.text
        assert resp.headers["cache-control"] == "private, no-store"
        data = resp.json()["data"]

        student = data["student"]
        assert student["student_number"] == student_num
        assert "Juan A. Dela Cruz" in student["name"]
        assert student["programme"] == "BS in Computer Science"
        assert student["year_level"] == 3
        assert student["enrollment_status"] == "ENROLLED"
        assert data["current_term"]["semester"] is not None
        assert data["current_term"]["academic_year"] is not None

    async def test_ac_101_2_financial_hold(
        self, client: AsyncClient, db: AsyncSession, grant
    ) -> None:
        """AC-101.2: A student has an unresolved financial hold |

        Alert states hold, amount outstanding, and that it blocks enrollment.
        """
        student_num = "2021-00789-MN-0"
        user, _ = await _create_student(
            db,
            student_number=student_num,
            has_financial_hold=True,
        )
        await _grant_dashboard(db, grant, user.id)

        term = AcademicTerm(
            academic_year="2025-2026",
            semester="Second Semester",
            start_date=date(2026, 1, 15),
            end_date=date(2026, 5, 30),
            is_current=True,
            regular_enrollment_start=datetime.now(UTC),
            regular_enrollment_end=datetime.now(UTC) + timedelta(days=10),
            add_drop_deadline=datetime.now(UTC) + timedelta(days=15),
            grade_submission_deadline=datetime.now(UTC) + timedelta(days=30),
        )
        db.add(term)
        await db.flush()

        db.add(
            StudentBillingAccount(
                student_id=user.id,
                academic_term_id=term.id,
                gross_amount=Decimal("1250.00"),
                fhe_subsidy_amount=Decimal("0.00"),
                amount_paid=Decimal("0.00"),
                due_date=date(2026, 6, 30),
                payment_status="unpaid",
            )
        )
        await db.commit()

        headers = await _get_student_headers(client, student_num)
        resp = await client.get(DASHBOARD_URL, headers=headers)
        assert resp.status_code == 200, resp.text
        holds = resp.json()["data"]["holds"]

        assert len(holds) == 1
        hold = holds[0]
        assert hold["type"] == "FINANCIAL"
        assert Decimal(hold["amount_outstanding"]) > Decimal("0")
        assert hold["blocks_enrollment"] is True
        assert hold["is_blocking"] is True
        assert "ENROLLMENT" in hold["blocks"]

    async def test_ac_101_3_probation_unit_reduction_and_no_suspension(
        self, client: AsyncClient, db: AsyncSession, grant
    ) -> None:
        """AC-101.3: Set standing to Probation, then GET dashboard |

        Probation carries unit_reduction 6, and never the value Suspension.
        """
        student_num = "2021-00999-MN-0"
        user, _ = await _create_student(db, student_number=student_num)
        await _grant_dashboard(db, grant, user.id)

        term = AcademicTerm(
            academic_year="2025-2026",
            semester="First Semester",
            start_date=date(2025, 8, 15),
            end_date=date(2025, 12, 20),
            is_current=False,
            regular_enrollment_start=datetime.now(UTC) - timedelta(days=60),
            regular_enrollment_end=datetime.now(UTC) - timedelta(days=50),
            add_drop_deadline=datetime.now(UTC) - timedelta(days=40),
            grade_submission_deadline=datetime.now(UTC) - timedelta(days=10),
        )
        db.add(term)
        await db.flush()

        db.add(
            StudentTermGPA(
                student_id=user.id,
                academic_term_id=term.id,
                total_gpa_units=Decimal("18.0"),
                term_gpa=Decimal("3.25"),
                cumulative_gpa=Decimal("3.10"),
                academic_standing="Probation",
            )
        )
        await db.commit()

        headers = await _get_student_headers(client, student_num)
        resp = await client.get(DASHBOARD_URL, headers=headers)
        assert resp.status_code == 200, resp.text
        standing = resp.json()["data"]["standing"]

        assert standing["value"] == "Probation"
        assert standing["unit_reduction"] == 6
        assert standing["appeal_available"] is True
        assert standing["value"] != "Suspension"

    async def test_ac_101_4_enrolled_units_and_computed_ceiling(
        self, client: AsyncClient, db: AsyncSession, grant
    ) -> None:
        """AC-101.4: Student enrolled in 23 units |

        Ceiling shown must be computed from curriculum plus overload, not a constant.
        """
        prog = Program(
            code="BSCS_2026",
            name="BS in Computer Science",
            department="Computer Science",
            total_units_required=Decimal("140.0"),
            is_active=True,
        )
        db.add(prog)
        await db.flush()

        curr = Curriculum(
            program_id=prog.id,
            curriculum_year="2025-2026",
            is_active=True,
        )
        db.add(curr)
        await db.flush()

        term = AcademicTerm(
            academic_year="2025-2026",
            semester="Second Semester",
            start_date=date(2026, 1, 15),
            end_date=date(2026, 5, 30),
            is_current=True,
            regular_enrollment_start=datetime.now(UTC) - timedelta(days=10),
            regular_enrollment_end=datetime.now(UTC) + timedelta(days=10),
            add_drop_deadline=datetime.now(UTC) + timedelta(days=15),
            grade_submission_deadline=datetime.now(UTC) + timedelta(days=30),
        )
        db.add(term)
        await db.flush()

        # Create curriculum courses totaling 26 prescribed units
        courses = []
        for i in range(8):
            course = Course(
                course_code=f"CS10{i}",
                title=f"CS Course {i}",
                lecture_units=Decimal("4.0") if i in (5, 6) else Decimal("3.0"),
                lab_units=Decimal("0.0"),
                is_active=True,
            )
            db.add(course)
            courses.append(course)
        await db.flush()

        for c in courses:
            db.add(
                CurriculumCourse(
                    curriculum_id=curr.id,
                    course_id=c.id,
                    year_level=3,
                    semester="Second Semester",
                )
            )

        student_num = "2021-00230-MN-0"
        user, _ = await _create_student(
            db,
            student_number=student_num,
            program_id=prog.id,
            curriculum_id=curr.id,
            year_level=3,
        )
        await _grant_dashboard(db, grant, user.id)

        # Term enrollment
        term_enrl = TermEnrollment(
            student_id=user.id,
            academic_term_id=term.id,
            enrollment_status="enrolled",
        )
        db.add(term_enrl)
        await db.flush()

        # Enroll student in exactly 23 units (7 courses of 3, 3, 3, 3, 3, 4, 4)
        c_23_units = courses[:7]  # 5*3 + 2*4 = 23 units
        for idx, c in enumerate(c_23_units):
            sec = CourseSection(
                academic_term_id=term.id,
                course_id=c.id,
                section_name=f"CS3A-{idx}",
                capacity=40,
                status="open",
            )
            db.add(sec)
            await db.flush()
            db.add(
                StudentCourseEnrollment(
                    term_enrollment_id=term_enrl.id,
                    academic_term_id=term.id,
                    section_id=sec.id,
                    status="enrolled",
                )
            )

        # Add approved overload of 3 units
        db.add(
            OverloadRequest(
                student_id=user.id,
                academic_term_id=term.id,
                requested_units=3,
                justification="Graduating senior",
                state="APPROVED",
            )
        )
        await db.commit()

        headers = await _get_student_headers(client, student_num)
        resp = await client.get(DASHBOARD_URL, headers=headers)
        assert resp.status_code == 200, resp.text
        enrollment = resp.json()["data"]["enrollment"]

        assert Decimal(str(enrollment["enrolled_units"])) == Decimal("23")
        # prescribedLoad (26) + approvedOverload (3) - standingReduction (0) = 29
        assert Decimal(str(enrollment["effective_ceiling"])) == Decimal("29")
        assert Decimal(str(enrollment["unit_ceiling"])) == Decimal("29")
        assert enrollment["schedule_url"] == "/api/v1/student/schedule"

    async def test_ac_101_5_newly_admitted_student_empty_state(
        self, client: AsyncClient, db: AsyncSession, grant
    ) -> None:
        """AC-101.5: GET dashboard for newly admitted student with no history |

        Every panel shows explanatory empty state rather than a zero or error.
        """
        student_num = "2026-00001-MN-4"
        user, _ = await _create_student(
            db,
            student_number=student_num,
            first_name="Maria L.",
            last_name="Santos",
            year_level=1,
            enrollment_status="NOT_ENROLLED",
        )
        await _grant_dashboard(db, grant, user.id)
        headers = await _get_student_headers(client, student_num)
        # Clear login audit logs to ensure clean zero-history state for new student
        await db.execute(delete(AuthAuditLog).where(AuthAuditLog.user_id == user.id))
        await db.commit()

        resp = await client.get(DASHBOARD_URL, headers=headers)
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]

        assert data["student"]["enrollment_status"] == "NOT_ENROLLED"
        assert data["holds"] == []
        assert data["standing"]["value"] == "Good Standing"
        assert data["standing"]["unit_reduction"] == 0
        assert data["periods"] == []
        assert data["announcements"] == []
        assert data["events"] == []
        assert data["recent_activity"] == []

        empty_states = data["empty_states"]
        assert empty_states["announcements"] == "No announcements at this time."
        assert empty_states["events"] == "No upcoming events on the academic calendar."
        assert (
            empty_states["recent_activity"]
            == "Your account activity will appear here once you begin using the portal."
        )

    async def test_ac_101_6_ownership_enforcement_and_no_data_leakage(
        self, client: AsyncClient, db: AsyncSession, grant
    ) -> None:
        """AC-101.6: Authenticate as student A and inspect the payload |

        Only that student's own data is served, and no other student id appears.
        Old parameterized dashboard routes are removed and return 404.
        """
        student_a = "2021-00001-MN-0"
        user_a, _ = await _create_student(
            db, student_number=student_a, first_name="Student", last_name="Alpha"
        )
        await _grant_dashboard(db, grant, user_a.id)

        student_b = "2021-00002-MN-0"
        user_b, _ = await _create_student(
            db, student_number=student_b, first_name="Student", last_name="Beta"
        )

        headers_a = await _get_student_headers(client, student_a)
        resp = await client.get(DASHBOARD_URL, headers=headers_a)
        assert resp.status_code == 200

        text_payload = resp.text
        assert student_a in text_payload
        assert student_b not in text_payload
        assert str(user_b.id) not in text_payload
        assert "Student Beta" not in text_payload

        # Removed path returns 404
        resp_probe = await client.get(
            f"/api/v1/student/{student_b}/dashboard", headers=headers_a
        )
        assert resp_probe.status_code == 404

    async def test_ac_101_7_periods_days_remaining(
        self, client: AsyncClient, db: AsyncSession, grant
    ) -> None:
        """AC-101.7: Open an enrollment period closing in 3 days, then GET dashboard |

        The period and its closing date are shown with days_remaining == 3.
        """
        now = datetime.now(UTC)
        closes_in_3_days = now + timedelta(days=3, hours=1)

        term = AcademicTerm(
            academic_year="2025-2026",
            semester="Summer Term",
            start_date=date(2026, 6, 20),
            end_date=date(2026, 7, 30),
            is_current=True,
            regular_enrollment_start=now - timedelta(days=2),
            regular_enrollment_end=closes_in_3_days,
            add_drop_deadline=closes_in_3_days + timedelta(days=7),
            grade_submission_deadline=closes_in_3_days + timedelta(days=20),
        )
        db.add(term)
        await db.commit()

        student_num = "2021-00333-MN-0"
        user, _ = await _create_student(db, student_number=student_num)
        await _grant_dashboard(db, grant, user.id)
        headers = await _get_student_headers(client, student_num)

        resp = await client.get(DASHBOARD_URL, headers=headers)
        assert resp.status_code == 200, resp.text
        periods = resp.json()["data"]["periods"]

        enrl_period = next((p for p in periods if p["type"] == "ENROLLMENT"), None)
        assert enrl_period is not None
        assert enrl_period["is_open"] is True
        assert enrl_period["days_remaining"] == 3


@pytest.mark.asyncio
class TestNonFunctionalRequirementsAndArchitecture:
    """Covers bounded queries, no ceiling literal, no Suspension, and errors."""

    async def test_bounded_query_count_does_not_grow_with_history(
        self, client: AsyncClient, db: AsyncSession, grant
    ) -> None:
        """Performance: Count the database queries behind one dashboard request.

        The count is fixed and does not grow with enrolled courses or rows.
        """
        student_small = "2021-00101-MN-0"
        user_small, _ = await _create_student(db, student_number=student_small)
        await _grant_dashboard(db, grant, user_small.id)

        term = AcademicTerm(
            academic_year="2025-2026",
            semester="First Semester",
            start_date=date(2025, 8, 15),
            end_date=date(2025, 12, 20),
            is_current=True,
            regular_enrollment_start=datetime.now(UTC),
            regular_enrollment_end=datetime.now(UTC) + timedelta(days=10),
            add_drop_deadline=datetime.now(UTC) + timedelta(days=15),
            grade_submission_deadline=datetime.now(UTC) + timedelta(days=30),
        )
        db.add(term)
        await db.commit()

        headers_small = await _get_student_headers(client, student_small)

        query_count_small = 0

        def count_queries(
            _conn, _cursor, _statement, _parameters, _context, _executemany
        ):
            nonlocal query_count_small
            query_count_small += 1

        engine = db.bind
        event.listen(engine.sync_engine, "before_cursor_execute", count_queries)
        try:
            resp = await client.get(DASHBOARD_URL, headers=headers_small)
            assert resp.status_code == 200
        finally:
            event.remove(engine.sync_engine, "before_cursor_execute", count_queries)

        # Setup student with many enrolled courses and activity rows
        student_large = "2021-00102-MN-0"
        user_large, _ = await _create_student(db, student_number=student_large)
        await _grant_dashboard(db, grant, user_large.id)

        term_enrl = TermEnrollment(
            student_id=user_large.id,
            academic_term_id=term.id,
            enrollment_status="enrolled",
        )
        db.add(term_enrl)
        await db.flush()

        # Add 15 courses and enrollments
        for i in range(15):
            c = Course(
                course_code=f"BIG{i}",
                title=f"Big Course {i}",
                lecture_units=Decimal("3.0"),
                lab_units=Decimal("0.0"),
                is_active=True,
            )
            db.add(c)
            await db.flush()
            sec = CourseSection(
                academic_term_id=term.id,
                course_id=c.id,
                section_name=f"SEC-{i}",
                capacity=50,
                status="open",
            )
            db.add(sec)
            await db.flush()
            db.add(
                StudentCourseEnrollment(
                    term_enrollment_id=term_enrl.id,
                    academic_term_id=term.id,
                    section_id=sec.id,
                    status="enrolled",
                )
            )

        # Add 30 auth audit logs
        for i in range(30):
            db.add(
                AuthAuditLog(
                    id=uuid.uuid4(),
                    user_id=user_large.id,
                    attempted_id=student_large,
                    success=True,
                    created_at=datetime.now(UTC) - timedelta(minutes=i),
                )
            )
        await db.commit()

        headers_large = await _get_student_headers(client, student_large)

        query_count_large = 0

        def count_queries_large(
            _conn, _cursor, _statement, _parameters, _context, _executemany
        ):
            nonlocal query_count_large
            query_count_large += 1

        event.listen(engine.sync_engine, "before_cursor_execute", count_queries_large)
        try:
            resp = await client.get(DASHBOARD_URL, headers=headers_large)
            assert resp.status_code == 200
        finally:
            event.remove(
                engine.sync_engine, "before_cursor_execute", count_queries_large
            )

        # Assert query count is bounded and does not grow with history
        assert query_count_large == query_count_small, (
            f"Query count scaled: small={query_count_small}, large={query_count_large}"
        )

    async def test_no_ceiling_literal_appears_in_dashboard_module(self) -> None:
        """Assert effectiveCeiling is computed and no ceiling literal appears."""
        module_path = Path(dashboard_service.__file__)
        tree = ast.parse(module_path.read_text(encoding="utf-8"))

        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and "ceiling" in target.id.lower():
                        assert not isinstance(node.value, ast.Constant), (
                            f"Ceiling assigned constant literal: {ast.dump(node)}"
                        )

    async def test_standing_enumeration_contains_no_suspension(self) -> None:
        """Assert the standing enumeration contains no value named Suspension."""
        from cbpupsis_shared.domains.grading.service import STANDING_TIERS

        assert "Suspension" not in STANDING_TIERS
        assert "SUSPENSION" not in [s.upper() for s in STANDING_TIERS]

    async def test_partial_failure_resilience(
        self, client: AsyncClient, db: AsyncSession, grant
    ) -> None:
        """Reliability: Fail one upstream panel and assert the rest still renders."""
        student_num = "2021-00555-MN-0"
        user, _ = await _create_student(db, student_number=student_num)
        await _grant_dashboard(db, grant, user.id)
        headers = await _get_student_headers(client, student_num)

        with patch(
            "cbpupsis_shared.domains.billing.client.get_student_holds",
            side_effect=RuntimeError("Billing service temporary outage"),
        ):
            resp = await client.get(DASHBOARD_URL, headers=headers)
            assert resp.status_code == 200, resp.text
            data = resp.json()["data"]
            assert data["student"]["student_number"] == student_num
            assert data["holds"] == []
            assert data["standing"]["value"] == "Good Standing"

    async def test_error_scenarios(
        self, client: AsyncClient, db: AsyncSession, grant
    ) -> None:
        """Errors: 400 INVALID_INPUT, 401 UNAUTHORIZED, 404 on removed routes."""
        student_num = "2021-00666-MN-0"
        user, _ = await _create_student(db, student_number=student_num)
        await _grant_dashboard(db, grant, user.id)
        headers = await _get_student_headers(client, student_num)

        # 1. 401 Unauthorized with no token
        unauth_resp = await client.get(DASHBOARD_URL)
        assert unauth_resp.status_code == 401
        assert unauth_resp.headers["cache-control"] == "private, no-store"

        # 2. 400 Invalid Input on limit out of range (> 50)
        invalid_limit = await client.get(
            f"{DASHBOARD_URL}?announcementLimit=99", headers=headers
        )
        assert invalid_limit.status_code == 400
        assert invalid_limit.json()["code"] == "INVALID_INPUT"

        # 3. 400 Invalid Input on limit < 1
        invalid_zero = await client.get(
            f"{DASHBOARD_URL}?eventLimit=0", headers=headers
        )
        assert invalid_zero.status_code == 400
        assert invalid_zero.json()["code"] == "INVALID_INPUT"

        # 4. 400 Invalid Input on malformed year
        invalid_year = await client.get(
            f"{DASHBOARD_URL}?year=2025/2026", headers=headers
        )
        assert invalid_year.status_code == 400
        assert invalid_year.json()["code"] == "INVALID_INPUT"

        # 5. Removed dashboard routes return 404
        for removed in (
            "/api/v1/student/me/dashboard",
            "/api/v1/student/non-existent-id/dashboard",
        ):
            removed_resp = await client.get(removed, headers=headers)
            assert removed_resp.status_code == 404


@pytest.mark.asyncio
class TestDashboardRouteContractAndFiltering:
    """Covers single path, ReadDashboard permission, no-store, and content filtering."""

    @pytest.mark.parametrize(
        "path",
        [
            "/api/v1/student/me/dashboard",
            "/api/v1/student/2021-00111-MN-0/dashboard",
        ],
    )
    @pytest.mark.parametrize("authenticated", [False, True])
    async def test_removed_dashboard_paths_return_404(
        self,
        client: AsyncClient,
        db: AsyncSession,
        grant,
        path: str,
        authenticated: bool,
    ) -> None:
        headers: dict[str, str] = {}
        if authenticated:
            student_num = "2021-00111-MN-0"
            user, _ = await _create_student(db, student_number=student_num)
            await _grant_dashboard(db, grant, user.id)
            headers = await _get_student_headers(client, student_num)

        resp = await client.get(path, headers=headers)
        assert resp.status_code == 404

    async def test_student_without_read_dashboard_gets_403(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        student_num = "2021-00112-MN-0"
        await _create_student(db, student_number=student_num)
        headers = await _get_student_headers(client, student_num)

        resp = await client.get(DASHBOARD_URL, headers=headers)
        assert resp.status_code == 403
        assert READ_DASHBOARD in resp.json()["detail"]
        assert resp.headers["cache-control"] == "private, no-store"

    async def test_seed_iam_grants_read_dashboard_to_student_group(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        await seed_iam(db)
        student_num = "2021-00113-MN-0"
        user, _ = await _create_student(db, student_number=student_num)
        await iam_service.stage_new_members(db, [(user.id, STUDENT_GROUP)])
        await db.commit()

        headers = await _get_student_headers(client, student_num)
        resp = await client.get(DASHBOARD_URL, headers=headers)
        assert resp.status_code == 200, resp.text
        assert resp.headers["cache-control"] == "private, no-store"

    async def test_ac_101_8_excludes_faculty_only_and_expired_announcements(
        self, client: AsyncClient, db: AsyncSession, grant
    ) -> None:
        student_num = "2021-00114-MN-0"
        user, _ = await _create_student(db, student_number=student_num)
        await _grant_dashboard(db, grant, user.id)

        student_role = Role(code="student", name="Student")
        faculty_role = Role(code="faculty", name="Faculty")
        db.add_all([student_role, faculty_role])
        await db.flush()

        now = datetime.now(UTC)
        db.add_all(
            [
                Announcement(
                    author_id=user.id,
                    title="Global Pinned",
                    content="Visible to all",
                    category="GENERAL",
                    is_pinned=True,
                    is_published=True,
                    published_at=now - timedelta(hours=3),
                    created_at=now,
                ),
                Announcement(
                    author_id=user.id,
                    title="Student Only Notice",
                    content="Visible to students",
                    category="ACADEMIC",
                    target_role_id=student_role.id,
                    is_pinned=False,
                    is_published=True,
                    published_at=now - timedelta(hours=1),
                    created_at=now,
                ),
                Announcement(
                    author_id=user.id,
                    title="Global Unpinned",
                    content="Older global notice",
                    category="GENERAL",
                    is_pinned=False,
                    is_published=True,
                    published_at=now - timedelta(hours=2),
                    created_at=now,
                ),
                Announcement(
                    author_id=user.id,
                    title="Faculty Only Memo",
                    content="Must not be visible to students",
                    category="GENERAL",
                    target_role_id=faculty_role.id,
                    is_pinned=True,
                    is_published=True,
                    published_at=now,
                    created_at=now,
                ),
                Announcement(
                    author_id=user.id,
                    title="Expired Global Notice",
                    content="Already expired",
                    category="GENERAL",
                    is_pinned=True,
                    is_published=True,
                    published_at=now - timedelta(days=2),
                    expires_at=now - timedelta(minutes=1),
                    created_at=now,
                ),
            ]
        )
        await db.commit()

        headers = await _get_student_headers(client, student_num)
        resp = await client.get(DASHBOARD_URL, headers=headers)
        assert resp.status_code == 200, resp.text
        titles = [item["title"] for item in resp.json()["data"]["announcements"]]
        assert titles == [
            "Global Pinned",
            "Student Only Notice",
            "Global Unpinned",
        ]

    async def test_ac_101_8_excludes_faculty_announcement_even_without_student_role_row(
        self, client: AsyncClient, db: AsyncSession, grant
    ) -> None:
        student_num = "2021-00115-MN-0"
        user, _ = await _create_student(db, student_number=student_num)
        await _grant_dashboard(db, grant, user.id)

        faculty_role = Role(code="faculty", name="Faculty")
        db.add(faculty_role)
        await db.flush()

        now = datetime.now(UTC)
        db.add_all(
            [
                Announcement(
                    author_id=user.id,
                    title="Global Notice",
                    content="Visible to everyone",
                    category="GENERAL",
                    is_published=True,
                    published_at=now,
                    created_at=now,
                ),
                Announcement(
                    author_id=user.id,
                    title="Faculty Memo",
                    content="Hidden from students",
                    category="GENERAL",
                    target_role_id=faculty_role.id,
                    is_published=True,
                    published_at=now,
                    created_at=now,
                ),
            ]
        )
        await db.commit()

        headers = await _get_student_headers(client, student_num)
        resp = await client.get(DASHBOARD_URL, headers=headers)
        assert resp.status_code == 200, resp.text
        titles = [item["title"] for item in resp.json()["data"]["announcements"]]
        assert titles == ["Global Notice"]

    async def test_ac_101_9_excludes_finished_events_and_orders_soonest_first(
        self, client: AsyncClient, db: AsyncSession, grant
    ) -> None:
        student_num = "2021-00116-MN-0"
        user, _ = await _create_student(db, student_number=student_num)
        await _grant_dashboard(db, grant, user.id)

        today = datetime.now(ZoneInfo("Asia/Manila")).date()
        now = datetime.now(UTC)
        term = AcademicTerm(
            academic_year="2026-2027",
            semester="First Semester",
            start_date=today - timedelta(days=30),
            end_date=today + timedelta(days=90),
            is_current=True,
            regular_enrollment_start=now - timedelta(days=20),
            regular_enrollment_end=now + timedelta(days=10),
            add_drop_deadline=now + timedelta(days=15),
            grade_submission_deadline=now + timedelta(days=60),
        )
        db.add(term)
        await db.flush()

        db.add_all(
            [
                AcademicEvent(
                    academic_term_id=term.id,
                    title="Finished Single Day",
                    start_date=today - timedelta(days=2),
                    end_date=None,
                    event_type="HOLIDAY",
                ),
                AcademicEvent(
                    academic_term_id=term.id,
                    title="Finished Multi Day",
                    start_date=today - timedelta(days=5),
                    end_date=today - timedelta(days=1),
                    event_type="ACADEMIC",
                ),
                AcademicEvent(
                    academic_term_id=term.id,
                    title="Ongoing Multi Day",
                    start_date=today - timedelta(days=1),
                    end_date=today + timedelta(days=1),
                    event_type="ACADEMIC",
                ),
                AcademicEvent(
                    academic_term_id=term.id,
                    title="Today Event",
                    start_date=today,
                    end_date=None,
                    event_type="EXAM",
                ),
                AcademicEvent(
                    academic_term_id=term.id,
                    title="Upcoming Event",
                    start_date=today + timedelta(days=3),
                    end_date=None,
                    event_type="HOLIDAY",
                ),
            ]
        )
        await db.commit()

        headers = await _get_student_headers(client, student_num)
        resp = await client.get(DASHBOARD_URL, headers=headers)
        assert resp.status_code == 200, resp.text
        titles = [item["title"] for item in resp.json()["data"]["events"]]
        assert titles == [
            "Ongoing Multi Day",
            "Today Event",
            "Upcoming Event",
        ]
