"""Business logic for the academics domain."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, time

from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_shared.domains.academics import repository
from cbpupsis_shared.domains.academics.schemas import (
    AcademicEventRead,
    AcademicPeriodRead,
    AcademicTermRead,
)


def _to_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


async def get_current_term(db: AsyncSession) -> AcademicTermRead | None:
    """Return the currently active academic term."""
    row = await repository.get_current_term(db)
    if row is None:
        return None
    return AcademicTermRead.model_validate(row)


async def get_term(
    db: AsyncSession, academic_year: str, semester: str
) -> AcademicTermRead | None:
    """Return the specified academic term, or None."""
    row = await repository.get_term_by_year_and_semester(db, academic_year, semester)
    if row is None:
        return None
    return AcademicTermRead.model_validate(row)


def get_term_periods(
    term: AcademicTermRead, as_of: datetime | None = None
) -> list[AcademicPeriodRead]:
    """Calculate open and upcoming periods (within 7 days) for the term."""
    now = _to_utc(as_of or datetime.now(UTC))
    periods: list[AcademicPeriodRead] = []

    # 1. Enrollment Period
    open_enrl = term.early_enrollment_start or term.regular_enrollment_start
    close_enrl = term.late_enrollment_end or term.regular_enrollment_end
    if open_enrl and close_enrl:
        o_enrl = _to_utc(open_enrl)
        c_enrl = _to_utc(close_enrl)
        is_open = o_enrl <= now <= c_enrl
        opens_within_7_days = (o_enrl > now) and (
            (o_enrl - now).total_seconds() <= 7 * 86400
        )

        if is_open or opens_within_7_days:
            diff_seconds = (
                (c_enrl - now).total_seconds()
                if is_open
                else (o_enrl - now).total_seconds()
            )
            days_remaining = max(0, int(diff_seconds // 86400))
            periods.append(
                AcademicPeriodRead(
                    type="ENROLLMENT",
                    label=f"{term.semester} Enrollment",
                    opens_at=o_enrl,
                    closes_at=c_enrl,
                    is_open=is_open,
                    days_remaining=days_remaining,
                )
            )

    # 2. Add/Drop Period (Adjustment Period)
    if term.regular_enrollment_start and term.add_drop_deadline:
        o_adj = _to_utc(term.regular_enrollment_end or term.regular_enrollment_start)
        c_adj = _to_utc(term.add_drop_deadline)
        is_open = o_adj <= now <= c_adj
        opens_within_7_days = (o_adj > now) and (
            (o_adj - now).total_seconds() <= 7 * 86400
        )

        if is_open or opens_within_7_days:
            diff_seconds = (
                (c_adj - now).total_seconds()
                if is_open
                else (o_adj - now).total_seconds()
            )
            days_remaining = max(0, int(diff_seconds // 86400))
            periods.append(
                AcademicPeriodRead(
                    type="ADD_DROP",
                    label="Adjustment Period",
                    opens_at=o_adj,
                    closes_at=c_adj,
                    is_open=is_open,
                    days_remaining=days_remaining,
                )
            )

    return periods


async def list_upcoming_events(
    db: AsyncSession, term_id: uuid.UUID, limit: int = 10
) -> list[AcademicEventRead]:
    """Return academic calendar events for the given term."""
    rows = await repository.list_events_for_term(db, term_id, limit=limit)
    events: list[AcademicEventRead] = []
    for r in rows:
        starts_at = datetime.combine(r.start_date, time.min, tzinfo=UTC)
        ends_date = r.end_date or r.start_date
        ends_at = datetime.combine(ends_date, time.max, tzinfo=UTC)
        is_multi_day = bool(r.end_date and r.end_date > r.start_date)
        events.append(
            AcademicEventRead(
                id=f"evt_{str(r.id)[:8]}",
                title=r.title,
                starts_at=starts_at,
                ends_at=ends_at,
                is_multi_day=is_multi_day,
                category=(r.event_type or "ACADEMIC_CALENDAR").upper(),
                description=r.description,
            )
        )
    return events
