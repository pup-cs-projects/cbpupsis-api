"""Scoped waiver lifecycle; audit and durable notice share the state commit."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_core.events import Event
from cbpupsis_core.exceptions import ForbiddenError
from cbpupsis_core.pagination import Page
from cbpupsis_shared.domains.academics import client as academics
from cbpupsis_shared.domains.audit import client as audit
from cbpupsis_shared.domains.auth.exceptions import ResourceNotFoundError
from cbpupsis_shared.domains.curriculum import client as curriculum
from cbpupsis_shared.domains.enrollment import repository
from cbpupsis_shared.domains.enrollment.academic import (
    eligible_conditions,
    load_facts,
    passed,
)
from cbpupsis_shared.domains.enrollment.constants import PENDING_STATES, REVIEW_STEPS
from cbpupsis_shared.domains.enrollment.exceptions import (
    DecisionRemarksRequiredError,
    RequestAlreadyDecidedError,
    VpaaReferenceRequiredError,
    WaiverEligibilityError,
    WaiverPendingError,
    WaiverStepMismatchError,
)
from cbpupsis_shared.domains.enrollment.schemas import (
    WaiverDecision,
    WaiverFile,
    WaiverFiled,
    WaiverRead,
    WaiverReviewer,
)
from cbpupsis_shared.domains.sections import client as sections
from cbpupsis_shared.domains.users import client as users
from cbpupsis_shared.outbox import publish_transactional


def _read(row) -> WaiverRead:
    return WaiverRead(
        waiver_request_id=row.id,
        state=row.state,
        student_id=row.student_id,
        section_id=row.section_id,
        prerequisite_course_code=row.prerequisite_course_code,
        condition=row.condition,
        justification=row.justification,
        review_remarks=row.review_remarks,
        vpaa_reference=row.vpaa_token,
        created_at=row.created_at,
    )


async def file_waiver(
    db: AsyncSession, *, student_id, data: WaiverFile, ip_address=None
):
    try:
        # Shared account lock serializes duplicate filings; the partial unique
        # index also protects against future writers that omit this mutex.
        if await users.lock_active_user(db, student_id) is None:
            raise ResourceNotFoundError
        selected = await sections.record_sections(db, {data.section_id})
        if not selected:
            raise ResourceNotFoundError
        terms = await academics.record_terms(db, {selected[0].academic_term_id})
        if not terms:
            raise ResourceNotFoundError
        term = await academics.get_term(db, terms[0].academic_year, terms[0].semester)
        facts = await load_facts(
            db, student_id=student_id, term=term, section_ids=[data.section_id]
        )
        courses = {c.id: c for c in facts.catalog.courses}
        edge = next(
            (
                e
                for e in facts.catalog.prerequisites
                if e.course_id == selected[0].course_id
                and courses[e.prerequisite_course_id].course_code
                == data.prerequisite_course_code
            ),
            None,
        )
        if (
            edge is None
            or any(
                a.course_id == edge.prerequisite_course_id and passed(a.mark)
                for a in facts.record.attempts
            )
            or data.condition
            not in eligible_conditions(facts, edge.prerequisite_course_id)
        ):
            raise WaiverEligibilityError
        existing = await repository.academic_waivers(db, student_id, [data.section_id])
        if any(
            w.prerequisite_course_code == data.prerequisite_course_code
            and w.state == "APPROVED"
            for w in existing
        ):
            raise RequestAlreadyDecidedError("This waiver is already decided.")
        if any(
            w.prerequisite_course_code == data.prerequisite_course_code
            and w.state in PENDING_STATES
            for w in existing
        ):
            raise WaiverPendingError
        row = repository.stage_waiver(db, student_id=student_id, **data.model_dump())
        audit.stage(
            db,
            action="enrollment.waiver_filed",
            actor_id=student_id,
            target_type="course_waiver",
            target_id=row.id,
            new_state={"state": row.state},
            payload={"role": "student", "ip_address": ip_address},
        )
        result = WaiverFiled(waiver_request_id=row.id, state=row.state)
        await db.commit()
        return result
    except Exception:
        await db.rollback()
        raise


async def _scope(db, reviewer: WaiverReviewer):
    if reviewer.position not in REVIEW_STEPS:
        raise ForbiddenError(
            "This position cannot review waivers.", code="PERMISSION_DENIED"
        )
    if reviewer.position == "registrar":
        return None
    if reviewer.position == "chairperson":
        programs = await curriculum.program_ids_in_scope(
            db, department=reviewer.department
        )
    else:
        programs = await curriculum.program_ids_in_scope(db, college=reviewer.college)
    return await users.student_ids_in_programs(db, programs)


async def list_waivers(db, *, reviewer: WaiverReviewer, limit=50, offset=0):
    scope = await _scope(db, reviewer)
    rows, total = await repository.scoped_waivers(db, scope, limit=limit, offset=offset)
    return Page[WaiverRead](
        items=[_read(r) for r in rows], total=total, limit=limit, offset=offset
    )


async def decide_waiver(
    db: AsyncSession,
    *,
    reviewer: WaiverReviewer,
    waiver_id,
    data: WaiverDecision,
    ip_address=None,
):
    try:
        scope = await _scope(db, reviewer)
        row = await repository.lock_scoped_waiver(db, waiver_id, scope)
        if row is None:
            raise ResourceNotFoundError
        if row.state not in PENDING_STATES:
            raise RequestAlreadyDecidedError("This waiver is already decided.")
        if REVIEW_STEPS[reviewer.position] != row.state:
            raise WaiverStepMismatchError
        remarks = (data.remarks or "").strip()
        reference = (data.vpaa_reference or "").strip()
        if data.decision == "REJECT" and not remarks:
            raise DecisionRemarksRequiredError("Rejection remarks are required.")
        if row.state == "PENDING_VPAA" and data.decision == "APPROVE" and not reference:
            raise VpaaReferenceRequiredError
        prior = row.state
        next_state = (
            "REJECTED"
            if data.decision == "REJECT"
            else {
                "PENDING_CHAIR": "PENDING_DEAN",
                "PENDING_DEAN": "PENDING_VPAA",
                "PENDING_VPAA": "APPROVED",
            }[prior]
        )
        fields = {"state": next_state, "review_remarks": remarks or None}
        now = datetime.now(UTC)
        if data.decision == "APPROVE":
            if prior == "PENDING_CHAIR":
                fields.update(chair_approved_by=reviewer.id, chair_approved_at=now)
            elif prior == "PENDING_DEAN":
                fields.update(dean_approved_by=reviewer.id, dean_approved_at=now)
            else:
                fields.update(
                    registrar_approved_by=reviewer.id,
                    vpaa_approved_at=now,
                    vpaa_token=reference,
                )
        repository.stage_waiver_decision(row, **fields)
        audit.stage(
            db,
            action="enrollment.waiver_decided",
            actor_id=reviewer.id,
            target_type="course_waiver",
            target_id=row.id,
            prior_state={"state": prior},
            new_state={"state": next_state},
            payload={
                "role": "admin",
                "position": reviewer.position,
                "decision": data.decision,
                "ip_address": ip_address,
                "remarks": remarks or None,
                "vpaaReference": reference if next_state == "APPROVED" else None,
            },
        )
        publish_transactional(
            db,
            Event(
                name="enrollment.waiver_decided",
                payload={
                    "user_id": str(row.student_id),
                    "waiverRequestId": str(row.id),
                    "priorState": prior,
                    "state": next_state,
                },
            ),
        )
        result = _read(row)
        await db.commit()
        return result
    except Exception:
        await db.rollback()
        raise
