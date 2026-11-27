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

from sqlalchemy.exc import SQLAlchemyError
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
    PanelErrorsRead,
    StudentDashboardData,
    StudentDashboardResponse,
    StudentIdentityRead,
)
from cbpupsis_core.exceptions import AppError
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
from cbpupsis_shared.domains.billing.exceptions import BillingAccountUnavailableError
from cbpupsis_shared.domains.billing.schemas import HoldRead
from cbpupsis_shared.domains.curriculum import client as curriculum_client
from cbpupsis_shared.domains.enrollment import client as enrollment_client
from cbpupsis_shared.domains.enrollment.schemas import EnrollmentSummaryRead
from cbpupsis_shared.domains.grading import client as grading_client
from cbpupsis_shared.domains.grading.exceptions import GradeRecordUnavailableError
from cbpupsis_shared.domains.grading.schemas import ScholasticStandingRead
from cbpupsis_shared.domains.users import client as users_client
from cbpupsis_shared.domains.users.exceptions import UserNotFoundError
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


async def _resolve_profile(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
) -> tuple[StudentProfileRead | None, str | None]:
    try:
        profile = await users_client.get_student_profile(db, user_id)
    except (AppError, SQLAlchemyError):
        logger.warning("Failed to fetch student profile for dashboard", exc_info=True)
        return None, "Unable to load student identity details."
    if profile is None:
        return None, "Student profile record was not found."
    return profile, None


async def _resolve_identity(
    db: AsyncSession,
    *,
    user: CurrentUser,
    profile: StudentProfileRead | None,
    profile_error: str | None,
    panel_errors: PanelErrorsRead,
) -> StudentIdentityRead:
    identity_error = profile_error
    user_dto = None
    try:
        user_dto = await users_client.get_user(db, user.id)
    except (UserNotFoundError, SQLAlchemyError):
        logger.warning("Failed to fetch user DTO for student dashboard", exc_info=True)
        identity_error = identity_error or "Unable to load student identity details."

    student_number = profile.student_number if profile else None
    display_name: str | None = None
    if user_dto and (user_dto.display_name or user_dto.full_name):
        display_name = user_dto.display_name or user_dto.full_name

    avatar_url = user_dto.avatar_url if user_dto else None
    year_level = profile.year_level if profile else None
    enrollment_status = (
        profile.enrollment_status.upper()
        if profile and profile.enrollment_status
        else None
    )

    program_name: str | None = None
    if profile and profile.program_id:
        try:
            prog = await curriculum_client.get_program(db, profile.program_id)
            if prog:
                program_name = prog.name
        except (AppError, SQLAlchemyError):
            logger.warning("Failed to fetch program for dashboard", exc_info=True)
            identity_error = (
                identity_error or "Unable to load student identity details."
            )

    if identity_error is not None:
        panel_errors.identity = identity_error
        return StudentIdentityRead(
            student_number=student_number,
            name=display_name,
            programme=program_name,
            year_level=year_level,
            enrollment_status=enrollment_status,
            avatar_url=avatar_url,
            state="error",
            error=identity_error,
        )

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
    panel_errors: PanelErrorsRead,
) -> AcademicTermRead | None:
    try:
        if year and semester:
            return await academics_client.get_term(db, year, semester)
        return await academics_client.get_current_term(db)
    except (AppError, SQLAlchemyError):
        logger.warning("Failed to load academic term for dashboard", exc_info=True)
        panel_errors.term = "Unable to load academic term."
        return None


async def _resolve_holds(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    profile: StudentProfileRead | None,
    panel_errors: PanelErrorsRead,
) -> list[HoldRead] | None:
    try:
        return await billing_client.get_student_holds(
            db,
            student_id=user_id,
            has_financial_hold=profile.has_financial_hold if profile else False,
            has_academic_hold=profile.has_academic_hold if profile else False,
        )
    except (BillingAccountUnavailableError, AppError, SQLAlchemyError):
        logger.warning("Failed to load holds for student dashboard", exc_info=True)
        panel_errors.holds = "Unable to load account holds."
        return None


async def _resolve_standing(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    panel_errors: PanelErrorsRead,
) -> ScholasticStandingRead:
    try:
        return await grading_client.get_student_scholastic_standing(db, user_id)
    except (GradeRecordUnavailableError, AppError, SQLAlchemyError):
        logger.warning(
            "Failed to load scholastic standing for dashboard", exc_info=True
        )
        message = "Unable to load scholastic standing."
        panel_errors.standing = message
        return ScholasticStandingRead(
            value=None,
            unit_reduction=None,
            explanation=None,
            appeal_available=False,
            state="error",
            error=message,
        )


async def _resolve_enrollment_summary(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    profile: StudentProfileRead | None,
    term: AcademicTermRead | None,
    standing: ScholasticStandingRead,
    panel_errors: PanelErrorsRead,
) -> EnrollmentSummaryRead:
    standing_reduction = (
        Decimal(str(standing.unit_reduction))
        if standing.state != "error" and standing.unit_reduction is not None
        else None
    )

    if term is None:
        return EnrollmentSummaryRead(
            enrolled_units=None,
            effective_ceiling=None,
            unit_ceiling=None,
            prescribed_load=None,
            approved_overload_units=None,
            standing_reduction=standing_reduction,
            enrolled_course_count=0,
            schedule_url=DEFAULT_SCHEDULE_URL,
            state="unavailable",
            message="No active academic term found.",
        )

    prescribed_load: Decimal | None = None
    if profile is not None:
        try:
            prescribed_load = await curriculum_client.get_prescribed_load(
                db,
                curriculum_id=profile.curriculum_id,
                year_level=profile.year_level,
                semester=term.semester,
            )
        except (AppError, SQLAlchemyError):
            logger.warning(
                "Failed to calculate prescribed load for dashboard", exc_info=True
            )
            message = "Unable to load prescribed curriculum load."
            panel_errors.enrollment = message
            return EnrollmentSummaryRead(
                enrolled_units=None,
                effective_ceiling=None,
                unit_ceiling=None,
                prescribed_load=None,
                approved_overload_units=None,
                standing_reduction=standing_reduction,
                enrolled_course_count=0,
                schedule_url=DEFAULT_SCHEDULE_URL,
                state="error",
                message=message,
                error=message,
            )

    try:
        return await enrollment_client.get_enrollment_summary(
            db,
            student_id=user_id,
            academic_term_id=term.id,
            prescribed_load=prescribed_load,
            standing_reduction=standing_reduction,
        )
    except (AppError, SQLAlchemyError):
        logger.warning("Failed to load enrollment summary for dashboard", exc_info=True)
        message = "Unable to load enrollment summary."
        panel_errors.enrollment = message
        return EnrollmentSummaryRead(
            enrolled_units=None,
            effective_ceiling=None,
            unit_ceiling=None,
            prescribed_load=prescribed_load,
            approved_overload_units=None,
            standing_reduction=standing_reduction,
            enrolled_course_count=0,
            schedule_url=DEFAULT_SCHEDULE_URL,
            state="error",
            message=message,
            error=message,
        )


def _resolve_periods(
    term: AcademicTermRead | None, *, panel_errors: PanelErrorsRead
) -> list[AcademicPeriodRead]:
    if term is None:
        return []
    try:
        return academics_client.get_term_periods(term)
    except (AppError, ValueError):
        logger.warning("Failed to evaluate periods for dashboard", exc_info=True)
        panel_errors.periods = "Unable to evaluate academic periods."
        return []


async def _resolve_announcements(
    db: AsyncSession,
    *,
    limit: int,
    panel_errors: PanelErrorsRead,
) -> list[AnnouncementRead]:
    try:
        return await announcements_client.list_active_announcements(db, limit=limit)
    except (AppError, SQLAlchemyError):
        logger.warning("Failed to fetch announcements for dashboard", exc_info=True)
        panel_errors.announcements = "Unable to load announcements."
        return []


async def _resolve_events(
    db: AsyncSession,
    *,
    term_id: uuid.UUID | None,
    limit: int,
    panel_errors: PanelErrorsRead,
) -> list[AcademicEventRead]:
    if term_id is None:
        return []
    try:
        return await academics_client.list_upcoming_events(db, term_id, limit=limit)
    except (AppError, SQLAlchemyError):
        logger.warning("Failed to fetch calendar events for dashboard", exc_info=True)
        panel_errors.events = "Unable to load calendar events."
        return []


async def _resolve_recent_activity(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    student_number: str | None,
    limit: int,
    panel_errors: PanelErrorsRead,
) -> list[StudentRecentActivityRead]:
    try:
        return await audit_client.list_student_recent_activity(
            db,
            user_id=user_id,
            student_number=student_number or "",
            limit=limit,
        )
    except (AppError, SQLAlchemyError):
        logger.warning("Failed to fetch recent activity for dashboard", exc_info=True)
        panel_errors.recent_activity = "Unable to load recent activity."
        return []


def _build_empty_states(
    announcements: list[AnnouncementRead],
    events: list[AcademicEventRead],
    recent_activity: list[StudentRecentActivityRead],
    panel_errors: PanelErrorsRead,
) -> EmptyStatesRead:
    return EmptyStatesRead(
        announcements=(
            EMPTY_ANNOUNCEMENTS
            if len(announcements) == 0 and panel_errors.announcements is None
            else None
        ),
        events=(
            EMPTY_EVENTS if len(events) == 0 and panel_errors.events is None else None
        ),
        recent_activity=(
            EMPTY_RECENT_ACTIVITY
            if len(recent_activity) == 0 and panel_errors.recent_activity is None
            else None
        ),
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
    Handles expected upstream panel failures with explicit error states while letting
    unexpected errors propagate as HTTP 500.
    """
    _validate_dashboard_query(
        announcement_limit=announcement_limit,
        event_limit=event_limit,
        activity_limit=activity_limit,
        year=year,
    )

    panel_errors = PanelErrorsRead()
    profile, profile_error = await _resolve_profile(db, user_id=user.id)

    if student_id is not None:
        _check_ownership(user=user, profile=profile, target_student_id=student_id)

    identity = await _resolve_identity(
        db,
        user=user,
        profile=profile,
        profile_error=profile_error,
        panel_errors=panel_errors,
    )
    term = await _resolve_active_term(
        db, year=year, semester=semester, panel_errors=panel_errors
    )
    holds = await _resolve_holds(
        db, user_id=user.id, profile=profile, panel_errors=panel_errors
    )
    standing = await _resolve_standing(db, user_id=user.id, panel_errors=panel_errors)
    enrollment = await _resolve_enrollment_summary(
        db,
        user_id=user.id,
        profile=profile,
        term=term,
        standing=standing,
        panel_errors=panel_errors,
    )
    periods = _resolve_periods(term, panel_errors=panel_errors)
    announcements = await _resolve_announcements(
        db, limit=announcement_limit, panel_errors=panel_errors
    )
    events = await _resolve_events(
        db,
        term_id=term.id if term else None,
        limit=event_limit,
        panel_errors=panel_errors,
    )
    recent_activity = await _resolve_recent_activity(
        db,
        user_id=user.id,
        student_number=identity.student_number,
        limit=activity_limit,
        panel_errors=panel_errors,
    )
    empty_states = _build_empty_states(
        announcements, events, recent_activity, panel_errors
    )

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
        panel_errors=panel_errors,
    )

    return StudentDashboardResponse(status="success", data=data)
