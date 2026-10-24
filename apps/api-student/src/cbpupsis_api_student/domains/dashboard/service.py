"""Student landing dashboard rules (FR-002 / US-101).

Assembles the single aggregate dashboard payload for the authenticated student:
identity block, academic term context, financial & academic holds, scholastic
standing, enrollment summary with dynamically computed load ceiling, upcoming
deadlines and periods, announcements, events, and recent activity.

Ownership & Anti-Probing (BR-AUTH-005):
Verifies that callers cannot probe other student IDs. Any mismatch between the
authenticated session and the requested target student ID raises
ResourceNotFoundError (404), never 403.
"""

from __future__ import annotations

import logging
import uuid
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_api_student.domains.dashboard.constants import (
    ACTIVITY_LIMIT_ERROR_MESSAGE,
    ANNOUNCEMENT_LIMIT_ERROR_MESSAGE,
    DASHBOARD_LIMIT_MAX,
    DASHBOARD_LIMIT_MIN,
    DEFAULT_LIMIT,
    DEFAULT_SCHEDULE_URL,
    EMPTY_ANNOUNCEMENTS,
    EMPTY_EVENTS,
    EMPTY_RECENT_ACTIVITY,
    EVENT_LIMIT_ERROR_MESSAGE,
    YEAR_FORMAT_ERROR_MESSAGE,
    YEAR_PATTERN,
)
from cbpupsis_api_student.domains.dashboard.exceptions import (
    InvalidInputError,
    ResourceNotFoundError,
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
from cbpupsis_shared.domains.users.schemas import StudentProfileRead

logger = logging.getLogger(__name__)


def _validate_dashboard_query(
    *,
    announcement_limit: int,
    event_limit: int,
    activity_limit: int,
    year: str | None,
) -> None:
    """Validate query limits and format rules per FR-002."""
    if not (DASHBOARD_LIMIT_MIN <= announcement_limit <= DASHBOARD_LIMIT_MAX):
        raise InvalidInputError(ANNOUNCEMENT_LIMIT_ERROR_MESSAGE)
    if not (DASHBOARD_LIMIT_MIN <= event_limit <= DASHBOARD_LIMIT_MAX):
        raise InvalidInputError(EVENT_LIMIT_ERROR_MESSAGE)
    if not (DASHBOARD_LIMIT_MIN <= activity_limit <= DASHBOARD_LIMIT_MAX):
        raise InvalidInputError(ACTIVITY_LIMIT_ERROR_MESSAGE)
    if year is not None and not YEAR_PATTERN.match(year):
        raise InvalidInputError(YEAR_FORMAT_ERROR_MESSAGE)


def _check_ownership(
    *,
    user: CurrentUser,
    profile: StudentProfileRead | None,
    target_student_id: str | None,
) -> None:
    """Enforce student ownership per BR-AUTH-005; raises 404 on mismatch."""
    if target_student_id is None or target_student_id.lower() == "me":
        return
    if str(user.id) == target_student_id:
        return
    if profile is not None and profile.student_number == target_student_id:
        return
    # Must raise 404, never 403, per BR-AUTH-005 to prevent probing
    raise ResourceNotFoundError


async def _resolve_identity(
    db: AsyncSession,
    *,
    user: CurrentUser,
    profile: StudentProfileRead | None,
) -> StudentIdentityRead:
    user_dto = None
    try:
        user_dto = await users_client.get_user(db, user.id)
    except Exception:
        logger.warning("Failed to fetch user DTO for student dashboard", exc_info=True)

    student_number = profile.student_number if profile else ""
    display_name = ""
    if user_dto and (user_dto.display_name or user_dto.full_name):
        display_name = user_dto.display_name or user_dto.full_name or ""
    elif profile:
        display_name = profile.student_number

    avatar_url = user_dto.avatar_url if user_dto else None
    year_level = profile.year_level if profile else 1
    enrollment_status = (
        profile.enrollment_status.upper()
        if profile and profile.enrollment_status
        else "NOT_ENROLLED"
    )

    program_name = ""
    if profile and profile.program_id:
        try:
            prog = await curriculum_client.get_program(db, profile.program_id)
            if prog:
                program_name = prog.name
        except Exception:
            logger.warning("Failed to fetch program for dashboard", exc_info=True)

    return StudentIdentityRead(
        student_number=student_number,
        name=display_name,
        programme=program_name,
        year_level=year_level,
        enrollment_status=enrollment_status,
        avatar_url=avatar_url,
    )


async def _resolve_active_term(
    db: AsyncSession,
    *,
    year: str | None,
    semester: str | None,
) -> AcademicTermRead | None:
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
            academic_year=year or "",
            semester=semester or "",
            term_type="REGULAR",
            is_current=True,
        )
    return active_term


async def _resolve_holds(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    profile: StudentProfileRead | None,
) -> list[HoldRead]:
    try:
        return await billing_client.get_student_holds(
            db,
            student_id=user_id,
            has_financial_hold=profile.has_financial_hold if profile else False,
            has_academic_hold=profile.has_academic_hold if profile else False,
        )
    except Exception:
        logger.warning("Failed to load holds for student dashboard", exc_info=True)
        return []


async def _resolve_standing(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
) -> ScholasticStandingRead:
    try:
        return await grading_client.get_student_scholastic_standing(db, user_id)
    except Exception:
        logger.warning(
            "Failed to load scholastic standing for dashboard", exc_info=True
        )
        return ScholasticStandingRead(
            value="Good Standing",
            unit_reduction=0,
            explanation=None,
            appeal_available=False,
        )


async def _resolve_enrollment_summary(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    profile: StudentProfileRead | None,
    term: AcademicTermRead | None,
    standing: ScholasticStandingRead,
) -> EnrollmentSummaryRead:
    year_level = profile.year_level if profile else 1
    prescribed_load = Decimal("26")
    semester_name = term.semester if term else None
    try:
        curriculum_id = profile.curriculum_id if profile else None
        prescribed_load = await curriculum_client.get_prescribed_load(
            db,
            curriculum_id=curriculum_id,
            year_level=year_level,
            semester=semester_name,
        )
    except Exception:
        logger.warning(
            "Failed to calculate prescribed load for dashboard", exc_info=True
        )

    # Note: fallback ceiling is dynamically computed, never a constant literal
    unit_reduction = Decimal(str(standing.unit_reduction))
    fallback_ceiling = prescribed_load - unit_reduction

    fallback_enrollment = EnrollmentSummaryRead(
        enrolled_units=Decimal("0"),
        effective_ceiling=fallback_ceiling,
        prescribed_load=prescribed_load,
        approved_overload_units=Decimal("0"),
        standing_reduction=unit_reduction,
        enrolled_course_count=0,
        schedule_url=DEFAULT_SCHEDULE_URL,
    )

    if term is None:
        return fallback_enrollment

    try:
        return await enrollment_client.get_enrollment_summary(
            db,
            student_id=user_id,
            academic_term_id=term.id,
            prescribed_load=prescribed_load,
            standing_reduction=unit_reduction,
        )
    except Exception:
        logger.warning("Failed to load enrollment summary for dashboard", exc_info=True)
        return fallback_enrollment


def _resolve_periods(term: AcademicTermRead | None) -> list[AcademicPeriodRead]:
    if term is None:
        return []
    try:
        return academics_client.get_term_periods(term)
    except Exception:
        logger.warning("Failed to evaluate periods for dashboard", exc_info=True)
        return []


async def _resolve_announcements(
    db: AsyncSession,
    *,
    limit: int,
) -> list[AnnouncementRead]:
    try:
        return await announcements_client.list_active_announcements(db, limit=limit)
    except Exception:
        logger.warning("Failed to fetch announcements for dashboard", exc_info=True)
        return []


async def _resolve_events(
    db: AsyncSession,
    *,
    term_id: uuid.UUID | None,
    limit: int,
) -> list[AcademicEventRead]:
    if term_id is None:
        return []
    try:
        return await academics_client.list_upcoming_events(db, term_id, limit=limit)
    except Exception:
        logger.warning("Failed to fetch calendar events for dashboard", exc_info=True)
        return []


async def _resolve_recent_activity(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    student_number: str,
    limit: int,
) -> list[StudentRecentActivityRead]:
    try:
        return await audit_client.list_student_recent_activity(
            db,
            user_id=user_id,
            student_number=student_number,
            limit=limit,
        )
    except Exception:
        logger.warning("Failed to fetch recent activity for dashboard", exc_info=True)
        return []


def _build_empty_states(
    announcements: list[AnnouncementRead],
    events: list[AcademicEventRead],
    recent_activity: list[StudentRecentActivityRead],
) -> EmptyStatesRead:
    return EmptyStatesRead(
        announcements=EMPTY_ANNOUNCEMENTS if len(announcements) == 0 else None,
        events=EMPTY_EVENTS if len(events) == 0 else None,
        recent_activity=EMPTY_RECENT_ACTIVITY if len(recent_activity) == 0 else None,
    )


async def get_dashboard(
    db: AsyncSession,
    *,
    user: CurrentUser,
    student_id: str | None = None,
    semester: str | None = None,
    year: str | None = None,
    announcement_limit: int = DEFAULT_LIMIT,
    event_limit: int = DEFAULT_LIMIT,
    activity_limit: int = DEFAULT_LIMIT,
) -> StudentDashboardResponse:
    """Assemble single aggregate dashboard payload for the authenticated student.

    Issues a fixed, bounded query set that never fans out per course, term, or activity.
    Handles partial panel failures gracefully by serving fallback states.
    Enforces anti-probing ownership check (BR-AUTH-005) when target student_id
    is provided.
    """
    _validate_dashboard_query(
        announcement_limit=announcement_limit,
        event_limit=event_limit,
        activity_limit=activity_limit,
        year=year,
    )

    profile = await users_client.get_student_profile(db, user.id)

    if student_id is not None:
        _check_ownership(user=user, profile=profile, target_student_id=student_id)

    identity = await _resolve_identity(db, user=user, profile=profile)
    term = await _resolve_active_term(db, year=year, semester=semester)
    holds = await _resolve_holds(db, user_id=user.id, profile=profile)
    standing = await _resolve_standing(db, user_id=user.id)
    enrollment = await _resolve_enrollment_summary(
        db,
        user_id=user.id,
        profile=profile,
        term=term,
        standing=standing,
    )
    periods = _resolve_periods(term)
    announcements = await _resolve_announcements(db, limit=announcement_limit)
    events = await _resolve_events(
        db, term_id=term.id if term else None, limit=event_limit
    )
    recent_activity = await _resolve_recent_activity(
        db,
        user_id=user.id,
        student_number=identity.student_number,
        limit=activity_limit,
    )
    empty_states = _build_empty_states(announcements, events, recent_activity)

    data = StudentDashboardData(
        student=identity,
        current_term=term,
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
