"""Focused rule/evaluator checks. Real submit integration remains #169-owned."""

import uuid
from decimal import Decimal

import pytest

from cbpupsis_core.exceptions import ConflictError
from cbpupsis_shared.domains.academics.schemas import AcademicTermRead
from cbpupsis_shared.domains.curriculum.schemas import AcademicCourse
from cbpupsis_shared.domains.enrollment.academic import (
    eligible_conditions,
    evaluate_academics,
    evaluate_facts,
    require_academics,
)
from cbpupsis_shared.domains.grading.schemas import AcademicAttempt
from cbpupsis_shared.domains.sections.schemas import RecordSection


def attempt(facts, mark, course_id=None):
    row = AcademicAttempt(
        enrollment_item_id=uuid.uuid4(),
        course_id=course_id or facts.catalog.courses[1].id,
        mark=mark,
    )
    facts.record.attempts.append(row)
    return row


@pytest.mark.parametrize("mark", ["2.50", "3.00"])
def test_ac_enrl_002_1_published_pass_and_exact_boundary(facts, mark):
    attempt(facts, mark)
    facts.unfinished_courses.add(facts.catalog.courses[1].id)
    assert not evaluate_facts(facts).blockers


def test_ac_enrl_002_2_failure_names_range_and_mark(facts):
    attempt(facts, "5.00")
    with pytest.raises(ConflictError) as error:
        require_academics(evaluate_facts(facts))
    assert error.value.code == "ENROLL_PREREQUISITE_NOT_MET"
    assert "PREREQ" in error.value.detail and "1.00-3.00" in error.value.detail
    assert error.value.response_fields["details"]["unmet"][0] == {
        "course": "PREREQ",
        "mark": "5.00",
        "requiredRange": "1.00-3.00",
    }


@pytest.mark.parametrize("mark", ["Inc", "W", "in progress"])
def test_ac_enrl_002_3_blocking_marks(facts, mark):
    if mark == "in progress":
        facts.unfinished_courses.add(facts.catalog.courses[1].id)
    else:
        attempt(facts, mark)
    block = evaluate_facts(facts).blockers[0]
    assert block.blocking_mark == mark
    assert block.required_range == "1.00-3.00"


def gate_courses(facts, complete):
    facts.catalog.prerequisites = []
    facts.catalog.placements[facts.proposed[0].course_id] = 3
    for n in range(1, 7):
        course = AcademicCourse(
            id=uuid.uuid4(),
            course_code=f"EXPLICIT-{n}",
            total_units=Decimal(3),
            pe_sequence=n if n <= 4 else None,
            is_nstp=n > 4,
        )
        facts.catalog.courses.append(course)
        facts.catalog.placements[course.id] = 1
        if complete or n != 6:
            attempt(facts, "3.00", course.id)
            attempt(facts, "2.50", course.id)


def test_ac_enrl_002_4_third_year_gate_no_duplicate_credit(facts):
    gate_courses(facts, complete=False)
    block = evaluate_facts(facts).blockers[0]
    assert block.rule == "ENROLL_THIRD_YEAR_GATE"
    assert block.details == {
        "peCompleted": 4,
        "peRequired": 4,
        "nstpCompleted": "3",
        "nstpRequired": 6,
    }
    facts.record.attempts = []
    assert evaluate_facts(facts).blockers[0].details["peCompleted"] == 0


def test_ac_enrl_002_5_complete_gate_uses_course_placement(facts):
    gate_courses(facts, complete=True)
    facts.student.year_level = 1
    assert not evaluate_facts(facts).blockers


def two_pe(facts):
    facts.catalog.prerequisites = []
    facts.catalog.courses[0].pe_sequence = 2
    facts.catalog.courses[1].pe_sequence = 1
    facts.active.append(
        RecordSection(
            id=uuid.uuid4(),
            course_id=facts.catalog.courses[1].id,
            academic_term_id=facts.term.id,
        )
    )


def test_ac_enrl_002_6_second_pe_combines_active_and_selection(facts):
    two_pe(facts)
    facts.student.is_graduating_senior = True
    assert evaluate_facts(facts).blockers[0].rule == "ENROLL_PE_SEQUENCE"


def test_ac_enrl_002_7_only_server_confirmed_final_term_exception(facts):
    two_pe(facts)
    facts.student.is_graduating_senior = True
    facts.student.graduating_final_term_id = facts.term.id
    assert not evaluate_facts(facts).blockers
    facts.student.graduating_final_term_id = uuid.uuid4()
    assert evaluate_facts(facts).blockers


def test_ac_enrl_002_8_no_prerequisites(facts):
    facts.catalog.prerequisites = []
    assert not evaluate_facts(facts).blockers


def test_waiver_named_edge_only_and_does_not_bypass_gate(facts):
    facts.waivers = [(facts.proposed[0].id, "PREREQ", "PENDING_VPAA")]
    assert evaluate_facts(facts).blockers[0].rule == "ENROLL_WAIVER_PENDING"
    facts.waivers[0] = (facts.proposed[0].id, "PREREQ", "APPROVED")
    assert not evaluate_facts(facts).blockers
    facts.catalog.placements[facts.proposed[0].course_id] = 3
    assert evaluate_facts(facts).blockers[0].rule == "ENROLL_THIRD_YEAR_GATE"
    facts.waivers[0] = (facts.proposed[0].id, "OTHER", "APPROVED")
    assert evaluate_facts(facts).blockers[0].rule == "ENROLL_PREREQUISITE_NOT_MET"


def test_each_waiver_condition_is_server_verified(facts):
    prerequisite = facts.catalog.courses[1].id
    assert not eligible_conditions(facts, prerequisite)
    facts.record.cumulative_gwa = Decimal("1.75")
    assert eligible_conditions(facts, prerequisite) == {"SUPERIOR_STANDING"}
    facts.record.cumulative_gwa = Decimal("1.76")
    attempt(facts, "5.00")
    assert not eligible_conditions(facts, prerequisite)
    facts.active.append(
        RecordSection(
            id=uuid.uuid4(), course_id=prerequisite, academic_term_id=facts.term.id
        )
    )
    facts.retake_courses.add(prerequisite)
    assert eligible_conditions(facts, prerequisite) == {"REPEATED_PREREQUISITE"}
    facts.student.is_graduating_senior = True
    assert "GRADUATING" not in eligible_conditions(facts, prerequisite)
    facts.student.graduating_final_term_id = facts.term.id
    assert "GRADUATING" in eligible_conditions(facts, prerequisite)


def test_unreviewed_metadata_fails_closed(facts):
    facts.catalog.courses[0].is_nstp = None
    with pytest.raises(ConflictError, match="metadata"):
        evaluate_facts(facts)


async def test_real_owner_reads_exclude_draft_unlocked_and_missing_submission(
    db, academic_db
):
    f = academic_db
    term = AcademicTermRead.model_validate(f.term)
    kwargs = {"student_id": f.student, "term": term, "section_ids": [f.section.id]}
    for draft, locked, submitted in [
        (True, True, f.grade.submitted_at),
        (False, False, f.grade.submitted_at),
        (False, True, None),
    ]:
        f.grade.final_grade = "3.00"
        f.grade.is_draft, f.grade.is_locked, f.grade.submitted_at = (
            draft,
            locked,
            submitted,
        )
        await db.commit()
        verdict = await evaluate_academics(db, **kwargs)
        assert verdict.blockers[0].blocking_mark == "in progress"
    f.grade.is_draft, f.grade.is_locked = False, True
    from datetime import UTC, datetime

    f.grade.submitted_at = datetime.now(UTC)
    await db.commit()
    assert not (await evaluate_academics(db, **kwargs)).blockers
