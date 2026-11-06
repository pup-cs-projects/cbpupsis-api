"""One read-only academic evaluator for preview, submit stage 7 and later consumers."""

from __future__ import annotations

import uuid
from collections.abc import Collection
from dataclasses import dataclass, field, replace
from decimal import Decimal, InvalidOperation

from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_shared.domains.academics.schemas import AcademicTermRead
from cbpupsis_shared.domains.auth.exceptions import ResourceNotFoundError
from cbpupsis_shared.domains.curriculum import client as curriculum
from cbpupsis_shared.domains.curriculum.schemas import AcademicCatalog
from cbpupsis_shared.domains.enrollment import repository
from cbpupsis_shared.domains.enrollment.constants import PENDING_STATES
from cbpupsis_shared.domains.enrollment.exceptions import (
    AcademicDataUnavailableError,
    AcademicRefusalError,
)
from cbpupsis_shared.domains.enrollment.schemas import (
    AcademicBlock,
    AcademicSectionVerdict,
    AcademicVerdict,
)
from cbpupsis_shared.domains.grading.schemas import AcademicRecord
from cbpupsis_shared.domains.sections import client as sections
from cbpupsis_shared.domains.sections.schemas import RecordSection
from cbpupsis_shared.domains.users import client as users
from cbpupsis_shared.domains.users.schemas import StudentProfileRead


def academic_data_unavailable():
    return AcademicDataUnavailableError()


def passed(mark: str) -> bool:
    try:
        value = Decimal(mark)
        return value.is_finite() and Decimal("1.00") <= value <= Decimal("3.00")
    except InvalidOperation:
        return False


@dataclass
class AcademicFacts:
    student: StudentProfileRead
    term: AcademicTermRead
    proposed: list[RecordSection]
    active: list[RecordSection]
    catalog: AcademicCatalog
    record: AcademicRecord
    # Small immutable edge facts; no ORM objects cross the public client.
    waivers: list[tuple[uuid.UUID, str, str]]
    unfinished_courses: set[uuid.UUID]
    retake_courses: set[uuid.UUID] = field(default_factory=set)
    # Courses a preview can still pair with this one in the same selection.
    co_selectable: set[uuid.UUID] = field(default_factory=set)


_SEMESTER_TYPES = {
    "first": "REGULAR",
    "second": "REGULAR",
    "first semester": "REGULAR",
    "second semester": "REGULAR",
    "1st": "REGULAR",
    "2nd": "REGULAR",
    "1st semester": "REGULAR",
    "2nd semester": "REGULAR",
    "summer": "SUMMER",
    "summer term": "SUMMER",
    "summer semester": "SUMMER",
}


async def load_facts(
    db: AsyncSession,
    *,
    student_id: uuid.UUID,
    term: AcademicTermRead,
    section_ids: Collection[uuid.UUID],
    preview: bool = False,
) -> AcademicFacts:
    # Grading reads enrollment history through the enrollment client.
    from cbpupsis_shared.domains.grading import client as grading  # noqa: PLC0415

    if term is None:
        raise academic_data_unavailable()
    if term.term_type is None:
        term = term.model_copy(
            update={"term_type": _SEMESTER_TYPES.get(term.semester.strip().lower())}
        )
    student = await users.get_student_profile(db, student_id)
    if student is None:
        raise ResourceNotFoundError
    proposed = await sections.record_sections(db, set(section_ids))
    if len(proposed) != len(set(section_ids)) or any(
        s.academic_term_id != term.id for s in proposed
    ):
        raise ResourceNotFoundError
    active_rows = await repository.active_registrations(db, student_id, term.id)
    active = await sections.record_sections(db, {r.section_id for r in active_rows})
    if len(active) != len({r.section_id for r in active_rows}):
        raise academic_data_unavailable()
    catalog = await curriculum.academic_catalog(
        db, student.curriculum_id, {s.course_id for s in [*proposed, *active]}
    )
    if student.curriculum_id is None:
        raise academic_data_unavailable()
    if preview:
        proposed = [s for s in proposed if s.course_id in catalog.placements]
    elif any(s.course_id not in catalog.placements for s in proposed):
        raise academic_data_unavailable()
    record = await grading.academic_record(db, student_id)
    published_ids = {a.enrollment_item_id for a in record.attempts}
    # Any attempt lacking a published final mark remains unfinished. A separate
    # completed pass still satisfies the edge, regardless of this additional attempt.
    history = await repository.list_record_enrollments(db, student_id)
    unfinished_sections = await sections.record_sections(
        db, {r.section_id for r in history if r.id not in published_ids}
    )
    waivers = await repository.academic_waivers(db, student_id, section_ids)
    unfinished_active_sections = {
        r.section_id for r in active_rows if r.id not in published_ids
    }
    return AcademicFacts(
        student,
        term,
        proposed,
        active,
        catalog,
        record,
        [(w.section_id, w.prerequisite_course_code, w.state) for w in waivers],
        {s.course_id for s in unfinished_sections},
        {s.course_id for s in active if s.id in unfinished_active_sections},
    )


def eligible_conditions(facts: AcademicFacts, prerequisite_id) -> set[str]:
    conditions = set()
    selected = {s.course_id for s in facts.proposed} | facts.retake_courses
    if prerequisite_id in selected and any(
        a.course_id == prerequisite_id and a.mark == "5.00"
        for a in facts.record.attempts
    ):
        conditions.add("REPEATED_PREREQUISITE")
    gwa = facts.record.cumulative_gwa
    if (
        gwa is not None
        and gwa.is_finite()
        and Decimal("1.00") <= gwa <= Decimal("1.75")
    ):
        conditions.add("SUPERIOR_STANDING")
    if (
        facts.student.is_graduating_senior
        and facts.student.graduating_final_term_id == facts.term.id
    ):
        conditions.add("GRADUATING")
    return conditions


def evaluate_facts(facts: AcademicFacts) -> AcademicVerdict:
    courses = {c.id: c for c in facts.catalog.courses}
    if any(
        e.prerequisite_course_id not in courses for e in facts.catalog.prerequisites
    ):
        raise academic_data_unavailable()
    selected_ids = {s.course_id for s in [*facts.proposed, *facts.active]}
    if facts.student.curriculum_id is None or any(
        cid not in courses or courses[cid].is_nstp is None for cid in selected_ids
    ):
        raise academic_data_unavailable()
    passed_ids = {a.course_id for a in facts.record.attempts if passed(a.mark)}
    verdict = AcademicVerdict()
    prereq_blocks, gate_blocks, pe_blocks = [], [], []
    selected_pe = {cid for cid in selected_ids if courses[cid].pe_sequence is not None}
    final_term = (
        facts.student.is_graduating_senior
        and facts.student.graduating_final_term_id == facts.term.id
    )
    if len(selected_pe) > 1 and facts.term.term_type not in {"REGULAR", "SUMMER"}:
        raise academic_data_unavailable()
    for section in sorted(
        facts.proposed, key=lambda s: (courses[s.course_id].course_code, str(s.id))
    ):
        course = courses[section.course_id]
        section_result = AcademicSectionVerdict()
        verdict.sections[section.id] = section_result
        edges = sorted(
            (e for e in facts.catalog.prerequisites if e.course_id == course.id),
            key=lambda e: courses[e.prerequisite_course_id].course_code,
        )
        for edge in edges:
            required = courses[edge.prerequisite_course_id]
            if required.id in passed_ids:
                continue
            if edge.is_corequisite and required.id in (
                selected_ids | facts.co_selectable
            ):
                continue
            states = {
                state
                for sid, code, state in facts.waivers
                if sid == section.id and code == required.course_code
            }
            if "APPROVED" in states:
                continue
            pending = bool(states.intersection(PENDING_STATES))
            marks = sorted(
                {a.mark for a in facts.record.attempts if a.course_id == required.id}
            )
            if required.id in facts.unfinished_courses:
                marks.append("in progress")
            block = AcademicBlock(
                rule="ENROLL_WAIVER_PENDING"
                if pending
                else "ENROLL_PREREQUISITE_NOT_MET",
                course_code=required.course_code,
                blocking_mark=", ".join(marks) if marks else "not taken",
                required_range="1.00-3.00",
            )
            section_result.prerequisites_satisfied = False
            section_result.blocks.append(block)
            prereq_blocks.append(block)
            section_result.waiver_available |= not pending and bool(
                eligible_conditions(facts, required.id)
            )
        if facts.catalog.placements.get(course.id, 0) >= 3:
            curriculum_ids = set(facts.catalog.placements)
            if any(
                cid not in courses or courses[cid].is_nstp is None
                for cid in curriculum_ids
            ):
                raise academic_data_unavailable()
            pe_done = {
                courses[cid].pe_sequence
                for cid in passed_ids.intersection(curriculum_ids)
                if courses[cid].pe_sequence is not None
            }
            nstp_done = sum(
                (
                    courses[cid].total_units
                    for cid in passed_ids.intersection(curriculum_ids)
                    if courses[cid].is_nstp
                ),
                Decimal(0),
            )
            if not {1, 2, 3, 4}.issubset(pe_done) or nstp_done < 6:
                block = AcademicBlock(
                    rule="ENROLL_THIRD_YEAR_GATE",
                    course_code=course.course_code,
                    details={
                        "peCompleted": len(pe_done),
                        "peRequired": 4,
                        "nstpCompleted": str(nstp_done),
                        "nstpRequired": 6,
                    },
                )
                section_result.blocks.append(block)
                gate_blocks.append(block)
        if (
            course.pe_sequence is not None
            and len(selected_pe) > 1
            and facts.term.term_type == "REGULAR"
            and not final_term
        ):
            block = AcademicBlock(
                rule="ENROLL_PE_SEQUENCE", course_code=course.course_code
            )
            section_result.blocks.append(block)
            pe_blocks.append(block)
        if section_result.blocks:
            section_result.block_reason = "; ".join(
                block_message(b) for b in section_result.blocks
            )
    verdict.blockers = [*prereq_blocks, *gate_blocks, *pe_blocks]
    return verdict


def block_message(block: AcademicBlock) -> str:
    if block.rule in {"ENROLL_PREREQUISITE_NOT_MET", "ENROLL_WAIVER_PENDING"}:
        pending = "Waiver pending. " if block.rule == "ENROLL_WAIVER_PENDING" else ""
        return (
            f"{pending}{block.course_code}: {block.blocking_mark}; "
            f"required range {block.required_range}."
        )
    if block.rule == "ENROLL_THIRD_YEAR_GATE":
        d = block.details
        return (
            f"{block.course_code}: complete PE 1-4 ({d['peCompleted']}/4) "
            f"and NSTP ({d['nstpCompleted']}/6 units)."
        )
    return (
        f"{block.course_code}: only one PE course is allowed in a regular term "
        "without the server-confirmed graduating final-term exception."
    )


async def evaluate_academics(
    db: AsyncSession, *, student_id, term, section_ids
) -> AcademicVerdict:
    """Evaluate authenticated identity and resolved term; writes/commits nothing."""
    return evaluate_facts(
        await load_facts(db, student_id=student_id, term=term, section_ids=section_ids)
    )


async def evaluate_registered(
    db: AsyncSession,
    *,
    student_id: uuid.UUID,
    term: AcademicTermRead,
    section_ids: Collection[uuid.UUID],
) -> AcademicVerdict:
    """Re-check an existing registration, counting each registered course once.

    ``load_facts`` also reads these sections as active history. They are the
    subject here, so their own in-progress attempts must not count against them.
    """
    facts = await load_facts(
        db, student_id=student_id, term=term, section_ids=section_ids
    )
    registered = {s.id for s in facts.proposed}
    own_courses = {s.course_id for s in facts.proposed}
    return evaluate_facts(
        replace(
            facts,
            active=[s for s in facts.active if s.id not in registered],
            unfinished_courses=facts.unfinished_courses - own_courses,
            retake_courses=facts.retake_courses - own_courses,
        )
    )


async def evaluate_changes(
    db: AsyncSession,
    *,
    student_id: uuid.UUID,
    term: AcademicTermRead,
    add_section_ids: Collection[uuid.UUID],
    drop_section_ids: Collection[uuid.UUID],
) -> AcademicVerdict:
    """Check additions against the registration that remains after the drops."""
    facts = await load_facts(
        db, student_id=student_id, term=term, section_ids=add_section_ids
    )
    dropped = set(drop_section_ids)
    active = [s for s in facts.active if s.id not in dropped]
    dropped_courses = {s.course_id for s in facts.active if s.id in dropped}
    return evaluate_facts(
        replace(
            facts,
            active=active,
            retake_courses=facts.retake_courses - dropped_courses,
        )
    )


async def preview_academics(
    db: AsyncSession,
    *,
    student_id: uuid.UUID,
    term: AcademicTermRead,
    section_ids: Collection[uuid.UUID],
) -> dict[uuid.UUID, AcademicSectionVerdict]:
    """Load authoritative facts once; candidates are alternatives, not a selection."""
    facts = await load_facts(
        db, student_id=student_id, term=term, section_ids=section_ids, preview=True
    )
    offered = {section.course_id for section in facts.proposed}
    return {
        section.id: evaluate_facts(
            replace(facts, proposed=[section], co_selectable=offered)
        ).sections[section.id]
        for section in facts.proposed
    }


def require_academics(verdict: AcademicVerdict) -> None:
    """#169 calls after stage 6 and before allocating seats, inside its transaction."""
    if not verdict.blockers:
        return
    first = verdict.blockers[0]
    details = first.details.copy()
    if first.rule in {"ENROLL_PREREQUISITE_NOT_MET", "ENROLL_WAIVER_PENDING"}:
        details["unmet"] = [
            {
                "course": b.course_code,
                "mark": b.blocking_mark,
                "requiredRange": b.required_range,
            }
            for b in verdict.blockers
            if b.rule in {"ENROLL_PREREQUISITE_NOT_MET", "ENROLL_WAIVER_PENDING"}
        ]
    raise AcademicRefusalError(
        block_message(first),
        code=first.rule,
        response_fields={"details": details},
    )
