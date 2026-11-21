"""Business logic for the curriculum and catalog domain."""

from __future__ import annotations

import uuid
from collections import defaultdict, deque
from collections.abc import Collection, Iterable, Sequence
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_core.exceptions import ConflictError, NotFoundError, ValidationError
from cbpupsis_database.models.curriculum import Course, CoursePrerequisite, Program
from cbpupsis_shared.domains.audit import client as audit_client
from cbpupsis_shared.domains.auth.exceptions import (
    InsufficientRoleError,
    ResourceNotFoundError,
)
from cbpupsis_shared.domains.curriculum import repository
from cbpupsis_shared.domains.curriculum.classification import classify_course_code
from cbpupsis_shared.domains.curriculum.exceptions import (
    CurriculumVersionInUseError,
    PrerequisiteCycleError,
)
from cbpupsis_shared.domains.curriculum.schemas import (
    AcademicCatalog,
    AcademicCourse,
    CatalogCourseCreate,
    CatalogCourseRead,
    CatalogCourseUpdate,
    CurriculumCourseItemRead,
    CurriculumRead,
    CurriculumVersionCreate,
    CurriculumVersionRead,
    EnrollmentCourse,
    PrerequisiteEdge,
    ProgramRead,
    RecordCourse,
    ScheduleCourse,
    StudentDegreeCourseProgressRead,
    StudentDegreeProgressRead,
    SubstitutionApprovalRead,
    SubstitutionApproveRequest,
)
from cbpupsis_shared.domains.enrollment import client as enrollment_client
from cbpupsis_shared.domains.grading.constants import GRADING_MODES
from cbpupsis_shared.domains.grading.exceptions import GradeModeLockedError
from cbpupsis_shared.domains.users import client as users_client


def _normalize_grading_mode(value: str) -> str:
    mode = value.strip().upper()
    if mode not in GRADING_MODES:
        raise ValidationError(
            "Grading mode must be NUMERIC or PASS_FAIL.",
            code="VALIDATION_ERROR",
        )
    return mode


async def record_courses(db: AsyncSession, ids: Collection[uuid.UUID]):
    return [
        RecordCourse.model_validate(row)
        for row in await repository.list_record_courses(db, ids)
    ]


async def schedule_courses(
    db: AsyncSession, ids: list[uuid.UUID]
) -> list[ScheduleCourse]:
    return [
        ScheduleCourse.model_validate(row)
        for row in await repository.schedule_courses(db, ids)
    ]


async def active_programs_by_code(
    db: AsyncSession, codes: Iterable[str]
) -> dict[str, ProgramRead]:
    """Map each upper-cased code that names an active program to that program.

    Codes compare case-insensitively, so ``bscs`` and ``BSCS`` are one program.
    A code missing from the result is unknown or inactive.
    """
    wanted = {code.strip().upper() for code in codes if code and code.strip()}
    rows = await repository.list_active_programs_by_code(db, wanted)
    return {row.code.upper(): ProgramRead.model_validate(row) for row in rows}


async def get_program(db: AsyncSession, program_id: uuid.UUID) -> ProgramRead | None:
    """Return a program by id, or ``None``."""
    row = await repository.get_program(db, program_id)
    return None if row is None else ProgramRead.model_validate(row)


async def programs_by_ids(
    db: AsyncSession, ids: set[uuid.UUID]
) -> dict[uuid.UUID, ProgramRead]:
    return {
        row.id: ProgramRead.model_validate(row)
        for row in await repository.list_programs_by_ids(db, ids)
    }


async def get_curriculum(
    db: AsyncSession, curriculum_id: uuid.UUID
) -> CurriculumRead | None:
    """Return a curriculum by id, or ``None``."""
    row = await repository.get_curriculum(db, curriculum_id)
    return None if row is None else CurriculumRead.model_validate(row)


async def get_prescribed_load(
    db: AsyncSession,
    *,
    curriculum_id: uuid.UUID | None,
    year_level: int,
    semester: str,
) -> Decimal:
    """Return prescribed credit units for year level and semester."""
    return await repository.get_prescribed_load(
        db, curriculum_id=curriculum_id, year_level=year_level, semester=semester
    )


def _validate_prerequisite_graph(edges: set[tuple[uuid.UUID, uuid.UUID]]) -> None:
    dependents: dict[uuid.UUID, set[uuid.UUID]] = defaultdict(set)
    incoming: dict[uuid.UUID, int] = {}
    for course_id, prerequisite_id in edges:
        dependents[prerequisite_id].add(course_id)
        incoming.setdefault(prerequisite_id, 0)
        incoming[course_id] = incoming.get(course_id, 0) + 1
    ready = deque(node for node, count in incoming.items() if count == 0)
    visited = 0
    while ready:
        node = ready.popleft()
        visited += 1
        for dependent in dependents[node]:
            incoming[dependent] -= 1
            if incoming[dependent] == 0:
                ready.append(dependent)
    if visited != len(incoming):
        raise ValidationError(
            "Prerequisites cannot contain a self-dependency or cycle.",
            code="CURRICULUM_PREREQUISITE_CYCLE",
        )


async def stage_replace_prerequisites(
    db: AsyncSession,
    *,
    course_id: uuid.UUID,
    prerequisite_course_ids: Collection[uuid.UUID],
) -> None:
    """Validate and stage a course's full prerequisite replacement atomically.

    The caller owns commit/rollback. Every catalogue prerequisite writer must
    use this operation, with a READ COMMITTED PostgreSQL transaction. No routes
    or catalogue administration authority are introduced here. Retained edges
    preserve metadata; newly added edges use standard prerequisite defaults.
    """
    wanted = set(prerequisite_course_ids)
    await repository.lock_prerequisite_graph(db)
    courses = await repository.list_record_courses(db, wanted | {course_id})
    if {row.id for row in courses} != wanted | {course_id}:
        raise NotFoundError("Course not found.", code="RESOURCE_NOT_FOUND")
    graph = await repository.prerequisite_graph(db)
    previous = {prerequisite for course, prerequisite in graph if course == course_id}
    proposed = {(course, prereq) for course, prereq in graph if course != course_id}
    proposed.update((course_id, prerequisite) for prerequisite in wanted)
    _validate_prerequisite_graph(proposed)
    await repository.stage_replace_prerequisites(
        db,
        course_id=course_id,
        prerequisite_course_ids=wanted,
        previous_ids=previous,
    )


async def academic_catalog(
    db: AsyncSession,
    curriculum_id: uuid.UUID | None,
    course_ids: Collection[uuid.UUID],
) -> AcademicCatalog:
    courses, edges, placements = await repository.academic_catalog(
        db, curriculum_id, course_ids
    )
    return AcademicCatalog(
        courses=[AcademicCourse.model_validate(row) for row in courses],
        prerequisites=[PrerequisiteEdge.model_validate(row) for row in edges],
        placements=placements,
    )


async def program_ids_in_scope(
    db: AsyncSession,
    *,
    department: str | None = None,
    college: str | None = None,
) -> list[uuid.UUID]:
    return await repository.program_ids_in_scope(
        db, department=department, college=college
    )


async def get_course_read(
    db: AsyncSession, course_id: uuid.UUID
) -> CatalogCourseRead | None:
    """Return a single catalog course DTO by id, or ``None``."""
    course = await repository.get_course(db, course_id)
    if course is None:
        return None
    all_courses = await repository.list_all_courses(db)
    prereqs = await repository.list_all_course_prerequisites(db)
    programs = await repository.list_all_programs(db)
    pairs = await repository.list_all_curriculum_course_pairs(db)
    return _build_course_reads([course], all_courses, prereqs, programs, pairs)[0]


def _resolve_course_scope(
    course: Course,
    programs: Sequence[Program],
    curriculum_pairs: Sequence[tuple[uuid.UUID, uuid.UUID]],
) -> tuple[str | None, str | None]:
    """Resolve effective (department, college) for a course."""
    dept = course.department
    college = course.college
    programs_by_id = {p.id: p for p in programs}
    if dept is None:
        for prog_id, c_id in curriculum_pairs:
            if c_id == course.id and prog_id in programs_by_id:
                dept = programs_by_id[prog_id].department
                if college is None:
                    college = programs_by_id[prog_id].college
                break
    if college is None and dept is not None:
        for prog in programs:
            if prog.department == dept and prog.college is not None:
                college = prog.college
                break
    return dept, college


def _build_course_reads(
    target_courses: Sequence[Course],
    all_courses: Sequence[Course],
    prereqs: Sequence[CoursePrerequisite],
    programs: Sequence[Program],
    curriculum_pairs: Sequence[tuple[uuid.UUID, uuid.UUID]],
) -> list[CatalogCourseRead]:
    code_by_id = {c.id: c.course_code for c in all_courses}
    prereq_ids_by_course: dict[uuid.UUID, list[uuid.UUID]] = {}
    coreq_ids_by_course: dict[uuid.UUID, list[uuid.UUID]] = {}
    for edge in prereqs:
        bucket = coreq_ids_by_course if edge.is_corequisite else prereq_ids_by_course
        bucket.setdefault(edge.course_id, []).append(edge.prerequisite_course_id)

    reads: list[CatalogCourseRead] = []
    for course in target_courses:
        dept, college = _resolve_course_scope(course, programs, curriculum_pairs)
        p_ids = prereq_ids_by_course.get(course.id, [])
        c_ids = coreq_ids_by_course.get(course.id, [])
        total = (course.lecture_units or Decimal("0")) + (
            course.lab_units or Decimal("0")
        )
        lec_hours = (
            course.lecture_hours
            if course.lecture_hours is not None
            else int(course.lecture_units or 0)
        )
        lab_hours = (
            course.lab_hours
            if course.lab_hours is not None
            else int(course.lab_units or 0)
        )
        reads.append(
            CatalogCourseRead(
                id=course.id,
                code=course.course_code,
                course_code=course.course_code,
                title=course.title,
                description=course.description,
                department=dept,
                college=college,
                units=total,
                lecture_units=course.lecture_units,
                lab_units=course.lab_units,
                lecture_hours=lec_hours,
                lab_hours=lab_hours,
                total_units=total,
                grading_mode=course.grading_mode,
                term_offered=course.term_offered,
                is_active=course.is_active,
                prerequisite_ids=p_ids,
                corequisite_ids=c_ids,
                prerequisites=[code_by_id[i] for i in p_ids if i in code_by_id],
                corequisites=[code_by_id[i] for i in c_ids if i in code_by_id],
            )
        )
    return reads


def _course_in_admin_scope(
    course_read: CatalogCourseRead,
    *,
    position: str,
    department_id: str | None,
    college_id: str | None,
) -> bool:
    if position in {"registrar", "superadmin"}:
        return True
    if position == "chairperson":
        return department_id is not None and course_read.department == department_id
    if position == "dean":
        return college_id is not None and course_read.college == college_id
    return False


def _require_chair_position(position: str) -> None:
    if position not in {"chairperson", "superadmin"}:
        raise InsufficientRoleError


async def list_catalog_courses(
    db: AsyncSession,
    *,
    position: str,
    department_id: str | None,
    college_id: str | None,
) -> list[CatalogCourseRead]:
    """List catalog courses visible within the caller's role scope."""
    if position not in {"chairperson", "dean", "registrar", "superadmin"}:
        raise InsufficientRoleError
    courses = await repository.list_all_courses(db)
    prereqs = await repository.list_all_course_prerequisites(db)
    programs = await repository.list_all_programs(db)
    pairs = await repository.list_all_curriculum_course_pairs(db)
    reads = _build_course_reads(courses, courses, prereqs, programs, pairs)
    return [
        item
        for item in reads
        if _course_in_admin_scope(
            item,
            position=position,
            department_id=department_id,
            college_id=college_id,
        )
    ]


async def _resolve_dependency_refs(
    db: AsyncSession,
    *,
    self_id: uuid.UUID,
    self_code: str,
    refs: Sequence[uuid.UUID | str],
    all_courses: Sequence[Course],
) -> list[uuid.UUID]:
    by_id = {c.id: c for c in all_courses}
    by_code = {c.course_code.upper(): c for c in all_courses}
    resolved: list[uuid.UUID] = []
    for ref in refs:
        if isinstance(ref, uuid.UUID):
            if ref == self_id:
                raise PrerequisiteCycleError([self_code, self_code])
            target = by_id.get(ref) or await repository.get_course(db, ref)
            if target is None:
                raise ResourceNotFoundError
            resolved.append(target.id)
        else:
            ref_str = str(ref).strip()
            if ref_str.upper() == self_code.strip().upper():
                raise PrerequisiteCycleError([self_code, self_code])
            try:
                parsed_uuid = uuid.UUID(ref_str)
            except ValueError:
                parsed_uuid = None
            if parsed_uuid is not None:
                if parsed_uuid == self_id:
                    raise PrerequisiteCycleError([self_code, self_code])
                target = by_id.get(parsed_uuid) or await repository.get_course(
                    db, parsed_uuid
                )
            else:
                target = by_code.get(
                    ref_str.upper()
                ) or await repository.get_course_by_code(db, ref_str)
            if target is None:
                raise ResourceNotFoundError
            resolved.append(target.id)
    return resolved


def _validate_no_prerequisite_cycle(
    *,
    self_id: uuid.UUID,
    self_code: str,
    new_dep_ids: Sequence[uuid.UUID],
    all_courses: Sequence[Course],
    existing_edges: Sequence[CoursePrerequisite],
) -> None:
    """Detect direct or multi-hop cycles in the prerequisite/co-requisite graph."""
    code_by_id = {c.id: c.course_code for c in all_courses}
    code_by_id[self_id] = self_code

    adj: dict[uuid.UUID, list[uuid.UUID]] = {}
    for edge in existing_edges:
        if edge.course_id != self_id:
            adj.setdefault(edge.course_id, []).append(edge.prerequisite_course_id)
    adj[self_id] = list(new_dep_ids)

    for dep_id in new_dep_ids:
        if dep_id == self_id:
            raise PrerequisiteCycleError([self_code, self_code])

    # Search for a path from any dependency back to self_id (or any cycle reachable).
    path: list[uuid.UUID] = [self_id]
    on_stack: set[uuid.UUID] = {self_id}

    def dfs(current: uuid.UUID) -> None:
        for neighbor in adj.get(current, ()):
            if neighbor == self_id:
                cycle_codes = [code_by_id.get(uid, str(uid)) for uid in path] + [
                    self_code
                ]
                raise PrerequisiteCycleError(cycle_codes)
            if neighbor in on_stack:
                idx = path.index(neighbor)
                cycle_codes = [code_by_id.get(uid, str(uid)) for uid in path[idx:]] + [
                    code_by_id.get(neighbor, str(neighbor))
                ]
                raise PrerequisiteCycleError(cycle_codes)
            path.append(neighbor)
            on_stack.add(neighbor)
            dfs(neighbor)
            path.pop()
            on_stack.remove(neighbor)

    dfs(self_id)


async def create_catalog_course(
    db: AsyncSession,
    *,
    payload: CatalogCourseCreate,
    actor_id: uuid.UUID,
    position: str,
    department_id: str | None,
    college_id: str | None,
) -> CatalogCourseRead:
    """Create a course in the caller's department and record one audit entry."""
    _require_chair_position(position)
    if position == "chairperson" and not department_id:
        raise InsufficientRoleError

    programs = await repository.list_all_programs(db)
    resolved_college = college_id
    if resolved_college is None and department_id is not None:
        for prog in programs:
            if prog.department == department_id and prog.college is not None:
                resolved_college = prog.college
                break
    if position == "chairperson":
        if (
            payload.college is not None
            and resolved_college is not None
            and payload.college != resolved_college
        ):
            raise ValidationError(
                "Chairpersons cannot create courses for another college."
            )
    else:
        resolved_college = payload.college or resolved_college

    all_courses = await repository.list_all_courses(db)
    await repository.lock_prerequisite_graph(db)
    existing_edges = await repository.list_all_course_prerequisites(db)
    temp_id = uuid.uuid4()
    prereq_ids = await _resolve_dependency_refs(
        db,
        self_id=temp_id,
        self_code=payload.course_code,
        refs=payload.prerequisite_ids,
        all_courses=all_courses,
    )
    coreq_ids = await _resolve_dependency_refs(
        db,
        self_id=temp_id,
        self_code=payload.course_code,
        refs=payload.corequisite_ids,
        all_courses=all_courses,
    )
    _validate_no_prerequisite_cycle(
        self_id=temp_id,
        self_code=payload.course_code,
        new_dep_ids=[*prereq_ids, *coreq_ids],
        all_courses=all_courses,
        existing_edges=existing_edges,
    )

    lec_h = (
        payload.lecture_hours
        if payload.lecture_hours is not None
        else int(payload.lecture_units or 0)
    )
    lab_h = (
        payload.lab_hours
        if payload.lab_hours is not None
        else int(payload.lab_units or 0)
    )
    grading_mode = _normalize_grading_mode(payload.grading_mode)
    is_nstp, pe_sequence = classify_course_code(payload.course_code)
    course = repository.add_course(
        db,
        is_nstp=is_nstp,
        pe_sequence=pe_sequence,
        course_code=payload.course_code.strip(),
        title=payload.title.strip(),
        description=payload.description,
        department=department_id,
        college=resolved_college,
        lecture_units=payload.lecture_units,
        lab_units=payload.lab_units,
        lecture_hours=lec_h,
        lab_hours=lab_h,
        term_offered=payload.term_offered,
        grading_mode=grading_mode,
        is_active=payload.is_active,
    )
    try:
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        raise ConflictError(
            f"Course code {payload.course_code!r} already exists.",
            code="COURSE_ALREADY_EXISTS",
        ) from exc

    if prereq_ids or coreq_ids:
        await repository.replace_course_prerequisites(
            db,
            course_id=course.id,
            prerequisite_ids=prereq_ids,
            corequisite_ids=coreq_ids,
        )
        await db.flush()

    occurred_at = datetime.now(UTC)
    new_state = {
        "id": str(course.id),
        "code": course.course_code,
        "courseCode": course.course_code,
        "title": course.title,
        "department": course.department,
        "college": course.college,
        "lectureUnits": str(course.lecture_units),
        "labUnits": str(course.lab_units),
        "termOffered": course.term_offered,
        "isActive": course.is_active,
        "prerequisiteIds": [str(i) for i in prereq_ids],
        "corequisiteIds": [str(i) for i in coreq_ids],
    }
    audit_client.stage(
        db,
        action="catalog.course_created",
        actor_id=actor_id,
        target_type="course",
        target_id=course.id,
        occurred_at=occurred_at,
        prior_state=None,
        new_state=new_state,
    )
    await db.commit()
    result = await get_course_read(db, course.id)
    if result is None:
        raise ResourceNotFoundError
    return result


async def update_catalog_course(
    db: AsyncSession,
    *,
    course_id: uuid.UUID,
    payload: CatalogCourseUpdate,
    actor_id: uuid.UUID,
    position: str,
    department_id: str | None,
    college_id: str | None,
) -> CatalogCourseRead:
    """Update an in-scope course, checking for prerequisite cycles, and audit."""
    _require_chair_position(position)
    course = await repository.get_course(db, course_id)
    if course is None:
        raise ResourceNotFoundError

    programs = await repository.list_all_programs(db)
    pairs = await repository.list_all_curriculum_course_pairs(db)
    course_dept, _course_college = _resolve_course_scope(course, programs, pairs)
    if position == "chairperson" and (
        department_id is None or course_dept != department_id
    ):
        raise ResourceNotFoundError

    all_courses = await repository.list_all_courses(db)
    if payload.prerequisite_ids is not None or payload.corequisite_ids is not None:
        await repository.lock_prerequisite_graph(db)
    existing_edges = await repository.list_all_course_prerequisites(db)
    current_prereq_ids = [
        e.prerequisite_course_id
        for e in existing_edges
        if e.course_id == course.id and not e.is_corequisite
    ]
    current_coreq_ids = [
        e.prerequisite_course_id
        for e in existing_edges
        if e.course_id == course.id and e.is_corequisite
    ]

    prior_state = {
        "id": str(course.id),
        "code": course.course_code,
        "courseCode": course.course_code,
        "title": course.title,
        "description": course.description,
        "department": course_dept,
        "lectureUnits": str(course.lecture_units),
        "labUnits": str(course.lab_units),
        "gradingMode": course.grading_mode,
        "isActive": course.is_active,
        "prerequisiteIds": [str(i) for i in current_prereq_ids],
        "corequisiteIds": [str(i) for i in current_coreq_ids],
    }

    new_code = (
        payload.course_code.strip()
        if payload.course_code is not None
        else course.course_code
    )
    prereq_ids = (
        await _resolve_dependency_refs(
            db,
            self_id=course.id,
            self_code=new_code,
            refs=payload.prerequisite_ids,
            all_courses=all_courses,
        )
        if payload.prerequisite_ids is not None
        else current_prereq_ids
    )
    coreq_ids = (
        await _resolve_dependency_refs(
            db,
            self_id=course.id,
            self_code=new_code,
            refs=payload.corequisite_ids,
            all_courses=all_courses,
        )
        if payload.corequisite_ids is not None
        else current_coreq_ids
    )

    if payload.prerequisite_ids is not None or payload.corequisite_ids is not None:
        _validate_no_prerequisite_cycle(
            self_id=course.id,
            self_code=new_code,
            new_dep_ids=[*prereq_ids, *coreq_ids],
            all_courses=all_courses,
            existing_edges=existing_edges,
        )

    updates: dict[str, object] = {}
    if payload.course_code is not None:
        updates["course_code"] = new_code
        updates["is_nstp"], updates["pe_sequence"] = classify_course_code(new_code)
    if payload.title is not None:
        updates["title"] = payload.title.strip()
    if "description" in payload.model_fields_set:
        updates["description"] = payload.description
    if payload.lecture_units is not None:
        updates["lecture_units"] = payload.lecture_units
    if payload.lab_units is not None:
        updates["lab_units"] = payload.lab_units
    if payload.grading_mode is not None:
        grading_mode = _normalize_grading_mode(payload.grading_mode)
        current_mode = (course.grading_mode or "NUMERIC").strip().upper()
        if grading_mode != current_mode:
            # Grading imports curriculum at module load; defer client imports here.
            from cbpupsis_shared.domains.grading import (  # noqa: PLC0415
                client as grading_client,
            )
            from cbpupsis_shared.domains.sections import (  # noqa: PLC0415
                client as sections_client,
            )

            section_ids = await sections_client.ids_for_courses(db, [course.id])
            if section_ids and await grading_client.sections_have_marks(
                db, section_ids
            ):
                raise GradeModeLockedError
        updates["grading_mode"] = grading_mode
    if payload.is_active is not None:
        updates["is_active"] = payload.is_active

    if updates:
        repository.update_course(course, **updates)
    if payload.prerequisite_ids is not None or payload.corequisite_ids is not None:
        await repository.replace_course_prerequisites(
            db,
            course_id=course.id,
            prerequisite_ids=prereq_ids,
            corequisite_ids=coreq_ids,
        )

    if payload.lecture_hours is not None:
        course.lecture_hours = payload.lecture_hours
    if payload.lab_hours is not None:
        course.lab_hours = payload.lab_hours

    if "term_offered" in payload.model_fields_set:
        course.term_offered = payload.term_offered

    await db.flush()
    occurred_at = datetime.now(UTC)
    new_state = {
        "id": str(course.id),
        "code": course.course_code,
        "courseCode": course.course_code,
        "title": course.title,
        "description": course.description,
        "department": course_dept,
        "lectureUnits": str(course.lecture_units),
        "labUnits": str(course.lab_units),
        "termOffered": course.term_offered,
        "gradingMode": course.grading_mode,
        "isActive": course.is_active,
        "prerequisiteIds": [str(i) for i in prereq_ids],
        "corequisiteIds": [str(i) for i in coreq_ids],
    }
    audit_client.stage(
        db,
        action="catalog.course_updated",
        actor_id=actor_id,
        target_type="course",
        target_id=course.id,
        occurred_at=occurred_at,
        prior_state=prior_state,
        new_state=new_state,
    )
    await db.commit()
    result = await get_course_read(db, course.id)
    if result is None:
        raise ResourceNotFoundError
    return result


async def publish_curriculum_version(
    db: AsyncSession,
    *,
    program_code: str,
    payload: CurriculumVersionCreate,
    actor_id: uuid.UUID,
    position: str,
    department_id: str | None,
) -> CurriculumVersionRead:
    """Publish a curriculum version with effectiveAdmissionYear and audit."""
    _require_chair_position(position)
    program = await repository.get_program_by_code(db, program_code)
    if program is None:
        raise ResourceNotFoundError
    if position == "chairperson" and (
        department_id is None or program.department != department_id
    ):
        raise ResourceNotFoundError

    curriculum_year = (
        payload.curriculum_year.strip()
        if payload.curriculum_year
        else str(payload.effective_admission_year)
    )

    curriculum = repository.add_curriculum(
        db,
        program_id=program.id,
        curriculum_year=curriculum_year,
        effective_admission_year=payload.effective_admission_year,
        is_active=True,
    )
    try:
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        msg = (
            f"Curriculum version {curriculum_year!r} already exists for {program.code}."
        )
        raise ConflictError(
            msg,
            code="CURRICULUM_VERSION_ALREADY_EXISTS",
        ) from exc

    all_courses = await repository.list_all_courses(db)
    for item in payload.courses:
        course: Course | None = None
        if item.course_id is not None:
            course = await repository.get_course(db, item.course_id)
        elif item.course_code is not None:
            course = await repository.get_course_by_code(db, item.course_code)
        if course is None:
            await db.rollback()
            raise ResourceNotFoundError
        if item.prerequisite_ids or item.corequisite_ids:
            await repository.lock_prerequisite_graph(db)
            existing_edges = await repository.list_all_course_prerequisites(db)
            p_ids = await _resolve_dependency_refs(
                db,
                self_id=course.id,
                self_code=course.course_code,
                refs=item.prerequisite_ids,
                all_courses=all_courses,
            )
            c_ids = await _resolve_dependency_refs(
                db,
                self_id=course.id,
                self_code=course.course_code,
                refs=item.corequisite_ids,
                all_courses=all_courses,
            )
            try:
                _validate_no_prerequisite_cycle(
                    self_id=course.id,
                    self_code=course.course_code,
                    new_dep_ids=[*p_ids, *c_ids],
                    all_courses=all_courses,
                    existing_edges=existing_edges,
                )
            except PrerequisiteCycleError:
                await db.rollback()
                raise
            await repository.replace_course_prerequisites(
                db,
                course_id=course.id,
                prerequisite_ids=p_ids,
                corequisite_ids=c_ids,
            )
            await db.flush()
        repository.add_curriculum_course(
            db,
            curriculum_id=curriculum.id,
            course_id=course.id,
            year_level=item.year_level,
            semester=item.semester,
            course_type=item.course_type,
        )
    await db.flush()

    bound_student_ids = await users_client.bind_students_for_curriculum_version(
        db,
        program_id=program.id,
        curriculum_id=curriculum.id,
        effective_admission_year=payload.effective_admission_year,
    )

    mapped_rows = await repository.list_curriculum_courses(db, curriculum.id)
    course_items = [
        CurriculumCourseItemRead(
            course_id=c.id,
            course_code=c.course_code,
            title=c.title,
            units=(c.lecture_units or Decimal("0")) + (c.lab_units or Decimal("0")),
            year_level=cc.year_level,
            semester=cc.semester,
            course_type=cc.course_type,
        )
        for cc, c in mapped_rows
    ]

    occurred_at = datetime.now(UTC)
    audit_client.stage(
        db,
        action="catalog.curriculum_version_published",
        actor_id=actor_id,
        target_type="curriculum",
        target_id=curriculum.id,
        occurred_at=occurred_at,
        prior_state=None,
        new_state={
            "id": str(curriculum.id),
            "programCode": program.code,
            "version": curriculum.curriculum_year,
            "curriculumYear": curriculum.curriculum_year,
            "effectiveAdmissionYear": curriculum.effective_admission_year,
            "courseIds": [str(item.course_id) for item in course_items],
            "boundStudentIds": [str(sid) for sid in bound_student_ids],
        },
    )
    await db.commit()
    return CurriculumVersionRead(
        id=curriculum.id,
        program_id=program.id,
        program_code=program.code,
        version=curriculum.curriculum_year,
        curriculum_year=curriculum.curriculum_year,
        effective_admission_year=curriculum.effective_admission_year,
        is_active=curriculum.is_active,
        courses=course_items,
    )


async def delete_curriculum_version(
    db: AsyncSession,
    *,
    program_code: str | None = None,
    version_id: uuid.UUID,
    actor_id: uuid.UUID,
    position: str,
    department_id: str | None,
) -> None:
    """Delete an unused curriculum version, or raise 409 CATALOG_VERSION_IN_USE."""
    _require_chair_position(position)
    curriculum = await repository.get_curriculum(db, version_id)
    if curriculum is None:
        raise ResourceNotFoundError
    program = await repository.get_program(db, curriculum.program_id)
    if program is None:
        raise ResourceNotFoundError
    if (
        program_code is not None
        and program.code.strip().upper() != program_code.strip().upper()
    ):
        raise ResourceNotFoundError
    if position == "chairperson" and (
        department_id is None or program.department != department_id
    ):
        raise ResourceNotFoundError

    bound_count = await users_client.count_students_by_curriculum(db, version_id)
    if bound_count > 0:
        raise CurriculumVersionInUseError(bound_count)

    prior_state = {
        "id": str(curriculum.id),
        "programCode": program.code,
        "version": curriculum.curriculum_year,
        "curriculumYear": curriculum.curriculum_year,
        "effectiveAdmissionYear": curriculum.effective_admission_year,
    }
    await repository.delete_curriculum(db, curriculum)
    occurred_at = datetime.now(UTC)
    audit_client.stage(
        db,
        action="catalog.curriculum_version_deleted",
        actor_id=actor_id,
        target_type="curriculum",
        target_id=version_id,
        occurred_at=occurred_at,
        prior_state=prior_state,
        new_state=None,
    )
    await db.commit()


async def get_student_degree_progress(
    db: AsyncSession,
    student_id: uuid.UUID,
    *,
    position: str = "registrar",
    department_id: str | None = None,
    college_id: str | None = None,
) -> StudentDegreeProgressRead:
    """Compute a student's curriculum degree progress with approved substitutions."""
    profile = await users_client.get_student_profile(db, student_id)
    if profile is None:
        raise ResourceNotFoundError
    program = await repository.get_program(db, profile.program_id)
    if program is None:
        raise ResourceNotFoundError

    if position in {"registrar", "superadmin"}:
        pass
    elif position == "chairperson":
        if department_id is None or program.department != department_id:
            raise ResourceNotFoundError
    elif position == "dean":
        if college_id is None or program.college != college_id:
            raise ResourceNotFoundError
    else:
        raise ResourceNotFoundError

    total_required = (
        program.total_units_required if program is not None else Decimal("0")
    )

    mapped_rows = (
        await repository.list_curriculum_courses(db, profile.curriculum_id)
        if profile.curriculum_id is not None
        else []
    )
    curriculum_courses = {c.id: c for _cc, c in mapped_rows}
    if curriculum_courses:
        total_required = sum(
            (
                (c.lecture_units or Decimal("0")) + (c.lab_units or Decimal("0"))
                for c in curriculum_courses.values()
            ),
            Decimal("0"),
        )

    substitutions = await repository.list_substitutions_for_students(db, [student_id])
    sub_by_orig = {
        sub.original_course_id: sub.substitute_course_id for sub in substitutions
    }

    enrollments = await enrollment_client.record_enrollments(db, student_id)
    section_ids = [e.section_id for e in enrollments]
    if section_ids:
        from cbpupsis_shared.domains.sections import (  # noqa: PLC0415
            client as sections_client,
        )

        sections = await sections_client.record_sections(db, section_ids)
    else:
        sections = []
    taken_course_ids = {s.course_id for s in sections}

    all_referenced_ids = (
        set(curriculum_courses)
        | set(sub_by_orig.keys())
        | set(sub_by_orig.values())
        | taken_course_ids
    )
    courses_by_id = {
        c.id: c for c in await repository.list_record_courses(db, all_referenced_ids)
    }

    completed_ids: list[uuid.UUID] = []
    completed_codes: list[str] = []
    completed_units = Decimal("0")
    course_progress_items: list[StudentDegreeCourseProgressRead] = []

    for orig_id, orig_course in curriculum_courses.items():
        sub_id = sub_by_orig.get(orig_id)
        orig_units = (orig_course.lecture_units or Decimal("0")) + (
            orig_course.lab_units or Decimal("0")
        )
        if orig_id in taken_course_ids:
            completed_ids.append(orig_id)
            completed_codes.append(orig_course.course_code)
            completed_units += orig_units
            course_progress_items.append(
                StudentDegreeCourseProgressRead(
                    course_id=orig_id,
                    course_code=orig_course.course_code,
                    title=orig_course.title,
                    units=orig_units,
                    completed=True,
                )
            )
        elif sub_id is not None and (
            sub_id in taken_course_ids or not taken_course_ids
        ):
            sub_course = courses_by_id.get(sub_id)
            completed_ids.append(sub_id)
            if orig_id not in completed_ids:
                completed_ids.append(orig_id)
            if sub_course is not None:
                completed_codes.append(sub_course.course_code)
            completed_codes.append(orig_course.course_code)
            units_to_add = orig_units
            if units_to_add == Decimal("0") and sub_course is not None:
                units_to_add = Decimal(str(sub_course.total_units or 0))
            completed_units += units_to_add
            course_progress_items.append(
                StudentDegreeCourseProgressRead(
                    course_id=orig_id,
                    course_code=orig_course.course_code,
                    title=orig_course.title,
                    units=units_to_add,
                    completed=True,
                    substituted_by_course_id=sub_id,
                    substituted_by_course_code=(
                        sub_course.course_code if sub_course is not None else None
                    ),
                )
            )
        else:
            course_progress_items.append(
                StudentDegreeCourseProgressRead(
                    course_id=orig_id,
                    course_code=orig_course.course_code,
                    title=orig_course.title,
                    units=orig_units,
                    completed=False,
                )
            )

    # Also handle substitutions when original course was not in curriculum_courses
    for orig_id, sub_id in sub_by_orig.items():
        if orig_id not in curriculum_courses and (
            sub_id in taken_course_ids or not taken_course_ids
        ):
            sub_course = courses_by_id.get(sub_id)
            orig_course = courses_by_id.get(orig_id)
            if sub_id not in completed_ids:
                completed_ids.append(sub_id)
            if orig_id not in completed_ids:
                completed_ids.append(orig_id)
            if sub_course is not None and sub_course.course_code not in completed_codes:
                completed_codes.append(sub_course.course_code)
            if (
                orig_course is not None
                and orig_course.course_code not in completed_codes
            ):
                completed_codes.append(orig_course.course_code)
            ref_course = orig_course or sub_course
            ref_units = (
                Decimal(str(ref_course.total_units or 0))
                if ref_course is not None
                else Decimal("0")
            )
            completed_units += ref_units
            if orig_course is not None:
                course_progress_items.append(
                    StudentDegreeCourseProgressRead(
                        course_id=orig_id,
                        course_code=orig_course.course_code,
                        title=orig_course.title,
                        units=ref_units,
                        completed=True,
                        substituted_by_course_id=sub_id,
                        substituted_by_course_code=(
                            sub_course.course_code if sub_course is not None else None
                        ),
                    )
                )

    return StudentDegreeProgressRead(
        student_id=student_id,
        curriculum_id=profile.curriculum_id,
        completed_units=completed_units,
        total_units_required=total_required,
        completed_course_ids=completed_ids,
        completed_course_codes=completed_codes,
        substituted_course_ids={str(orig): sub for orig, sub in sub_by_orig.items()},
        courses=course_progress_items,
    )


async def approve_course_substitution(
    db: AsyncSession,
    *,
    payload: SubstitutionApproveRequest,
    actor_id: uuid.UUID,
    position: str,
    department_id: str | None,
) -> SubstitutionApprovalRead:
    """Approve a course substitution for named students and record one audit row."""
    _require_chair_position(position)
    orig_course = await repository.get_course(db, payload.original_course_id)
    sub_course = await repository.get_course(db, payload.substitute_course_id)
    if orig_course is None or sub_course is None:
        raise ResourceNotFoundError

    programs = await repository.list_all_programs(db)
    pairs = await repository.list_all_curriculum_course_pairs(db)
    orig_dept, _ = _resolve_course_scope(orig_course, programs, pairs)
    if position == "chairperson" and (
        department_id is None or orig_dept != department_id
    ):
        raise ResourceNotFoundError

    student_profiles = await users_client.list_student_profiles(db, payload.student_ids)
    if len(student_profiles) != len(set(payload.student_ids)):
        raise ResourceNotFoundError

    programs_by_id = {p.id: p for p in programs}
    if position == "chairperson":
        for sp in student_profiles:
            prog = programs_by_id.get(sp.program_id)
            if prog is not None and prog.department != department_id:
                raise ResourceNotFoundError

    existing_subs = [
        sub
        for sub in await repository.list_substitutions_for_students(
            db, payload.student_ids
        )
        if sub.original_course_id == payload.original_course_id
    ]
    prior_state = (
        {
            "originalCourseId": str(payload.original_course_id),
            "studentIds": [str(sid) for sid in payload.student_ids],
            "previousSubstitutions": [
                {
                    "studentId": str(sub.student_id),
                    "originalCourseId": str(sub.original_course_id),
                    "substituteCourseId": str(sub.substitute_course_id),
                }
                for sub in existing_subs
            ],
        }
        if existing_subs
        else None
    )

    for student_id in payload.student_ids:
        await repository.upsert_course_substitution(
            db,
            student_id=student_id,
            original_course_id=payload.original_course_id,
            substitute_course_id=payload.substitute_course_id,
            approved_by=actor_id,
        )
    await db.flush()

    progress_list = [
        await get_student_degree_progress(db, sid) for sid in payload.student_ids
    ]

    occurred_at = datetime.now(UTC)
    new_state = {
        "original_course_id": str(payload.original_course_id),
        "originalCourseId": str(payload.original_course_id),
        "substitute_course_id": str(payload.substitute_course_id),
        "substituteCourseId": str(payload.substitute_course_id),
        "studentIds": [str(sid) for sid in payload.student_ids],
    }
    audit_client.stage(
        db,
        action="catalog.substitution_approved",
        actor_id=actor_id,
        target_type="course_substitution",
        target_id=payload.original_course_id,
        occurred_at=occurred_at,
        prior_state=prior_state,
        new_state=new_state,
    )
    await db.commit()

    return SubstitutionApprovalRead(
        original_course_id=payload.original_course_id,
        substitute_course_id=payload.substitute_course_id,
        student_ids=payload.student_ids,
        degree_progress=progress_list,
    )


async def enrollment_courses(
    db: AsyncSession,
    *,
    curriculum_id: uuid.UUID | None,
    program_id: uuid.UUID,
    year_level: int,
    semester: str,
) -> list[EnrollmentCourse]:
    return [
        EnrollmentCourse.model_validate(row)
        for row in await repository.list_enrollment_courses(
            db,
            curriculum_id=curriculum_id,
            program_id=program_id,
            year_level=year_level,
            semester=semester,
        )
    ]


async def active_curriculum_courses(
    db: AsyncSession,
    *,
    curriculum_id: uuid.UUID | None,
    program_id: uuid.UUID,
) -> list[EnrollmentCourse]:
    """Return active curriculum courses that may have sections in this term."""
    return [
        EnrollmentCourse.model_validate(row)
        for row in await repository.list_active_curriculum_courses(
            db,
            curriculum_id=curriculum_id,
            program_id=program_id,
        )
    ]


async def list_curriculum_course_ids(
    db: AsyncSession, curriculum_id: uuid.UUID
) -> set[uuid.UUID]:
    return await repository.list_curriculum_course_ids(db, curriculum_id)
