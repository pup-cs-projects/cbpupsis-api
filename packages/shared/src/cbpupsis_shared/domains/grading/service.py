"""Business logic for the grading domain."""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_shared.domains.grading import repository
from cbpupsis_shared.domains.grading.schemas import ScholasticStandingRead

STANDING_TIERS: dict[str, tuple[int, str | None, bool]] = {
    "Good Standing": (0, None, False),
    "Warning Level 1": (0, "Academic warning level 1", True),
    "Warning Level 2": (3, "Academic warning level 2 with 3 units reduction", True),
    "Probation": (6, "Academic probation with 6 units reduction", True),
    "College Dropping": (0, "College dropping", True),
    "University Dismissal": (0, "University dismissal", True),
}


async def get_student_scholastic_standing(
    db: AsyncSession, student_id: uuid.UUID
) -> ScholasticStandingRead:
    """Return student's scholastic standing adhering strictly to Handbook tiers.

    Uses Handbook Title 5 Section 12 tiers.
    """
    row = await repository.get_latest_student_term_gpa(db, student_id)
    raw_val = (
        row.academic_standing.strip()
        if row and row.academic_standing
        else "Good Standing"
    )

    # Enforce Audit 2.3: 'Suspension' is a Title 9 disciplinary sanction
    if "SUSPENSION" in raw_val.upper():
        raw_val = "Probation"

    matched = None
    for tier in STANDING_TIERS:
        if tier.lower() == raw_val.lower():
            matched = tier
            break

    if not matched:
        matched = "Good Standing"

    reduction, explanation, appeal = STANDING_TIERS[matched]
    return ScholasticStandingRead(
        value=matched,
        unit_reduction=reduction,
        explanation=explanation,
        appeal_available=appeal,
    )
