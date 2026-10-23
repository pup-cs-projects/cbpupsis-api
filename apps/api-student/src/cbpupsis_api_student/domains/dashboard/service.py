"""Business logic for assembling the student landing dashboard aggregate view."""

from __future__ import annotations

import logging
import uuid
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_api_student.domains.dashboard.constants import (
    DEFAULT_SCHEDULE_URL,
    EMPTY_ANNOUNCEMENTS,
    EMPTY_EVENTS,
    EMPTY_RECENT_ACTIVITY,
)
from cbpupsis_api_student.domains.dashboard.schemas import (
    EmptyStatesRead,
    StudentDashboardData,
    StudentDashboardResponse,
    StudentIdentityRead,
)
from cbpupsis_shared.domains.academics import client as academics_client
from cbpupsis_shared.domains.academics.schemas import (
    AcademicEventRead,
    AcademicPeriodRead,
    AcademicTermRead,
)
from cbpupsis_shared.domains.announcements import client as announcements_client
from cbpupsis_shared.domains.announcements.schemas import AnnouncementRead
from cbpupsis_shared.domains.audit import client as audit_client
from cbpupsis_shared.domains.audit.schemas import StudentRecentActivityRead
from cbpupsis_shared.domains.auth.dependencies import CurrentUser
from cbpupsis_shared.domains.billing import client as billing_client
from cbpupsis_shared.domains.billing.schemas import HoldRead
from cbpupsis_shared.domains.curriculum import client as curriculum_client
from cbpupsis_shared.domains.enrollment import client as enrollment_client
from cbpupsis_shared.domains.enrollment.schemas import EnrollmentSummaryRead
from cbpupsis_shared.domains.grading import client as grading_client
from cbpupsis_shared.domains.grading.schemas import ScholasticStandingRead
from cbpupsis_shared.domains.users import client as users_client

logger = logging.getLogger(__name__)


async def get_dashboard(
    db: AsyncSession,
    *,
    user: CurrentUser,
    semester: str | None = None,
    year: str | None = None,
    announcement_limit: int = 10,
    event_limit: int = 10,
    activity_limit: int = 10,
) -> StudentDashboardResponse:
    """Assemble single aggregate dashboard payload for the authenticated student.

    Issues a fixed, bounded query set that never fans out per course, term, or activity.
    Handles partial panel failures gracefully by serving fallback states.
    """
    student_profile = await users_client.get_student_profile(db, user.id)

    user_dto = None
    try:
        user_dto = await users_client.get_user(db, user.id)
    except Exception:
        logger.warning("Failed to fetch user DTO for dashboard", exc_info=True)

    # 1. Identity & Program
    student_id_str = (
        student_profile.student_number if student_profile else "2026-00001-MN-4"
    )
    display_name = (
        (user_dto.display_name or user_dto.full_name)
        if user_dto and (user_dto.display_name or user_dto.full_name)
        else "Student"
    )
    avatar_url = user_dto.avatar_url if user_dto else None
    year_level = student_profile.year_level if student_profile else 1
    enrollment_status = (
        student_profile.enrollment_status.upper()
        if student_profile and student_profile.enrollment_status
        else "NOT_ENROLLED"
    )

    program_name = "BS in Computer Science"
    if student_profile and student_profile.program_id:
        try:
            prog = await curriculum_client.get_program(db, student_profile.program_id)
            if prog:
                program_name = prog.name
        except Exception:
            logger.warning(
                "Failed to fetch program for dashboard, using fallback", exc_info=True
            )

    identity = StudentIdentityRead(
        student_number=student_id_str,
        name=display_name,
        programme=program_name,
        year_level=year_level,
        enrollment_status=enrollment_status,
        avatar_url=avatar_url,
    )

    # 2. Term context
    active_term: AcademicTermRead | None = None
    try:
        if year and semester:
            active_term = await academics_client.get_term(db, year, semester)
        if active_term is None:
            active_term = await academics_client.get_current_term(db)
    except Exception:
        logger.warning("Failed to load academic term for dashboard", exc_info=True)

    if active_term is None:
        active_term = AcademicTermRead(
            id=uuid.uuid4(),
            academic_year=year or "2025-2026",
            semester=semester or "Second Semester",
            term_type="REGULAR",
            is_current=True,
        )
    term_id = active_term.id

    # 3. Holds
    holds: list[HoldRead] = []
    try:
        holds = await billing_client.get_student_holds(
            db,
            student_id=user.id,
            has_financial_hold=student_profile.has_financial_hold
            if student_profile
            else False,
            has_academic_hold=student_profile.has_academic_hold
            if student_profile
            else False,
        )
    except Exception:
        logger.warning("Failed to load holds for student dashboard", exc_info=True)

    # 4. Scholastic Standing
    standing = ScholasticStandingRead(
        value="Good Standing",
        unit_reduction=0,
        explanation=None,
        appeal_available=False,
    )
    try:
        standing = await grading_client.get_student_scholastic_standing(db, user.id)
    except Exception:
        logger.warning(
            "Failed to load scholastic standing for dashboard", exc_info=True
        )

    # 5. Prescribed load & Enrollment Summary
    prescribed_load = Decimal("26")
    try:
        curriculum_id = student_profile.curriculum_id if student_profile else None
        prescribed_load = await curriculum_client.get_prescribed_load(
            db,
            curriculum_id=curriculum_id,
            year_level=year_level,
            semester=active_term.semester,
        )
    except Exception:
        logger.warning(
            "Failed to calculate prescribed load for dashboard", exc_info=True
        )

    enrollment = EnrollmentSummaryRead(
        enrolled_units=Decimal("0"),
        effective_ceiling=prescribed_load - Decimal(str(standing.unit_reduction)),
        prescribed_load=prescribed_load,
        approved_overload_units=Decimal("0"),
        standing_reduction=Decimal(str(standing.unit_reduction)),
        enrolled_course_count=0,
        schedule_url=DEFAULT_SCHEDULE_URL,
    )
    try:
        if active_term is not None:
            enrollment = await enrollment_client.get_enrollment_summary(
                db,
                student_id=user.id,
                academic_term_id=term_id,
                prescribed_load=prescribed_load,
                standing_reduction=Decimal(str(standing.unit_reduction)),
            )
    except Exception:
        logger.warning("Failed to load enrollment summary for dashboard", exc_info=True)

    # 6. Periods
    periods: list[AcademicPeriodRead] = []
    if active_term is not None:
        try:
            periods = academics_client.get_term_periods(active_term)
        except Exception:
            logger.warning("Failed to evaluate periods for dashboard", exc_info=True)

    # 7. Announcements
    announcements: list[AnnouncementRead] = []
    try:
        announcements = await announcements_client.list_active_announcements(
            db, limit=announcement_limit
        )
    except Exception:
        logger.warning("Failed to fetch announcements for dashboard", exc_info=True)

    # 8. Events
    events: list[AcademicEventRead] = []
    if active_term is not None:
        try:
            events = await academics_client.list_upcoming_events(
                db, term_id, limit=event_limit
            )
        except Exception:
            logger.warning(
                "Failed to fetch calendar events for dashboard", exc_info=True
            )

    # 9. Recent Activity
    recent_activity: list[StudentRecentActivityRead] = []
    try:
        recent_activity = await audit_client.list_student_recent_activity(
            db,
            user_id=user.id,
            student_number=student_id_str,
            limit=activity_limit,
        )
    except Exception:
        logger.warning("Failed to fetch recent activity for dashboard", exc_info=True)

    # 10. Empty States
    empty_states = EmptyStatesRead(
        announcements=EMPTY_ANNOUNCEMENTS if len(announcements) == 0 else None,
        events=EMPTY_EVENTS if len(events) == 0 else None,
        recent_activity=EMPTY_RECENT_ACTIVITY if len(recent_activity) == 0 else None,
    )

    data = StudentDashboardData(
        student=identity,
        current_term=active_term,
        holds=holds,
        standing=standing,
        enrollment=enrollment,
        periods=periods,
        announcements=announcements,
        events=events,
        recent_activity=recent_activity,
        empty_states=empty_states,
    )

    return StudentDashboardResponse(status="success", data=data)
