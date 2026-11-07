import pytest

from cbpupsis_database.models.curriculum import Course
from scripts.seed_course_classifications import (
    CourseClassification,
    stage_classifications,
)


async def test_reviewed_course_seed_dry_run_then_apply(db):
    ordinary = Course(course_code="SYNTH-O", title="Synthetic ordinary")
    pe = Course(course_code="SYNTH-P", title="Synthetic PE")
    db.add_all([ordinary, pe])
    await db.flush()
    entries = [
        CourseClassification(course_code="SYNTH-O", is_nstp=False, pe_sequence=None),
        CourseClassification(course_code="SYNTH-P", is_nstp=False, pe_sequence=1),
    ]
    assert await stage_classifications(db, entries, apply=False) == 2
    assert ordinary.is_nstp is None and pe.pe_sequence is None
    unknown = CourseClassification(
        course_code="UNKNOWN", is_nstp=True, pe_sequence=None
    )
    with pytest.raises(ValueError, match="Unknown course"):
        await stage_classifications(db, [*entries, unknown], apply=True)
    assert ordinary.is_nstp is None
    assert await stage_classifications(db, entries, apply=True) == 2
    assert ordinary.is_nstp is False and ordinary.pe_sequence is None
    assert pe.is_nstp is False and pe.pe_sequence == 1
    assert await stage_classifications(db, entries, apply=True) == 0
