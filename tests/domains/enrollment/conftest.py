"""Small explicit academic fixtures, not institutional mapping evidence."""

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from cbpupsis_database.models.academics import AcademicTerm
from cbpupsis_database.models.curriculum import (
    Course,
    CoursePrerequisite,
    Curriculum,
    CurriculumCourse,
    Program,
)
from cbpupsis_database.models.enrollment import StudentCourseEnrollment, TermEnrollment
from cbpupsis_database.models.grading import StudentGrade
from cbpupsis_database.models.sections import CourseSection
from cbpupsis_database.models.users import StudentProfile, User
from cbpupsis_shared.domains.academics.schemas import AcademicTermRead
from cbpupsis_shared.domains.curriculum.schemas import (
    AcademicCatalog,
    AcademicCourse,
    PrerequisiteEdge,
)
from cbpupsis_shared.domains.enrollment.academic import AcademicFacts
from cbpupsis_shared.domains.enrollment.schemas import WaiverFile, WaiverReviewer
from cbpupsis_shared.domains.grading.schemas import AcademicRecord
from cbpupsis_shared.domains.sections.schemas import RecordSection
from cbpupsis_shared.domains.users.schemas import StudentProfileRead


@pytest.fixture
def facts():
    target, prerequisite, term, student = [uuid.uuid4() for _ in range(4)]
    selected = RecordSection(id=uuid.uuid4(), course_id=target, academic_term_id=term)
    return AcademicFacts(
        student=StudentProfileRead(
            user_id=student,
            student_number="S1",
            program_id=uuid.uuid4(),
            curriculum_id=uuid.uuid4(),
            year_level=1,
            enrollment_status="enrolled",
        ),
        term=AcademicTermRead(
            id=term, academic_year="2026-2027", semester="FIRST", term_type="REGULAR"
        ),
        proposed=[selected],
        active=[],
        catalog=AcademicCatalog(
            courses=[
                AcademicCourse(
                    id=target,
                    course_code="TARGET",
                    total_units=Decimal(3),
                    is_nstp=False,
                    pe_sequence=None,
                ),
                AcademicCourse(
                    id=prerequisite,
                    course_code="PREREQ",
                    total_units=Decimal(3),
                    is_nstp=False,
                    pe_sequence=None,
                ),
            ],
            prerequisites=[
                PrerequisiteEdge(course_id=target, prerequisite_course_id=prerequisite)
            ],
            placements={target: 1, prerequisite: 1},
        ),
        record=AcademicRecord(attempts=[], cumulative_gwa=None),
        waivers=[],
        unfinished_courses=set(),
    )


@pytest.fixture
async def academic_db(db):
    async def user():
        row = User(
            id=uuid.uuid4(),
            email=f"{uuid.uuid4()}@example.com",
            password_hash="!fixture",
            full_name="Synthetic Student",
        )
        db.add(row)
        await db.flush()
        return row.id

    student = await user()
    reviewers = {}
    for position in ("chairperson", "dean", "registrar"):
        reviewers[position] = WaiverReviewer(
            id=await user(), position=position, department="DEPT-A", college="COLLEGE-A"
        )
    program = Program(
        id=uuid.uuid4(),
        code="SYNTH",
        name="Synthetic",
        department="DEPT-A",
        college="COLLEGE-A",
        total_units_required=120,
    )
    db.add(program)
    await db.flush()
    curriculum = Curriculum(
        id=uuid.uuid4(), program_id=program.id, curriculum_year="2026"
    )
    db.add(curriculum)
    now = datetime.now(UTC)
    term = AcademicTerm(
        id=uuid.uuid4(),
        academic_year="2026-2027",
        semester="FIRST",
        term_type="REGULAR",
        start_date=date(2020, 1, 1),
        end_date=date(2020, 5, 1),
        regular_enrollment_start=now,
        regular_enrollment_end=now,
        add_drop_deadline=now,
        grade_submission_deadline=now,
    )
    db.add(term)
    prerequisite = Course(
        id=uuid.uuid4(),
        course_code="PREREQ",
        title="Prerequisite",
        lecture_units=3,
        is_nstp=False,
    )
    target = Course(
        id=uuid.uuid4(),
        course_code="TARGET",
        title="Target",
        lecture_units=3,
        is_nstp=False,
    )
    db.add_all([prerequisite, target])
    await db.flush()
    profile = StudentProfile(
        user_id=student,
        student_number="SYNTH001",
        program_id=program.id,
        curriculum_id=curriculum.id,
        year_level=1,
    )
    db.add(profile)
    for course in (prerequisite, target):
        db.add(
            CurriculumCourse(
                curriculum_id=curriculum.id,
                course_id=course.id,
                year_level=1,
                semester="FIRST",
            )
        )
    db.add(
        CoursePrerequisite(course_id=target.id, prerequisite_course_id=prerequisite.id)
    )
    target_section = CourseSection(
        id=uuid.uuid4(),
        academic_term_id=term.id,
        course_id=target.id,
        section_name="A",
        capacity=30,
    )
    prereq_section = CourseSection(
        id=uuid.uuid4(),
        academic_term_id=term.id,
        course_id=prerequisite.id,
        section_name="A",
        capacity=30,
    )
    db.add_all([target_section, prereq_section])
    await db.flush()
    enrollment = TermEnrollment(
        id=uuid.uuid4(), student_id=student, academic_term_id=term.id
    )
    db.add(enrollment)
    await db.flush()
    item = StudentCourseEnrollment(
        id=uuid.uuid4(),
        term_enrollment_id=enrollment.id,
        academic_term_id=term.id,
        section_id=prereq_section.id,
        status="completed",
    )
    db.add(item)
    await db.flush()
    grade = StudentGrade(
        enrollment_item_id=item.id,
        final_grade="5.00",
        is_draft=False,
        is_locked=True,
        submitted_at=now,
    )
    db.add(grade)
    await db.commit()
    return SimpleNamespace(
        student=student,
        reviewers=reviewers,
        program=program,
        curriculum=curriculum,
        term=term,
        term_id=term.id,
        profile=profile,
        target=target,
        prerequisite=prerequisite,
        section=target_section,
        section_id=target_section.id,
        prereq_section=prereq_section,
        item=item,
        grade=grade,
        filing=WaiverFile(
            section_id=target_section.id,
            prerequisite_course_code="PREREQ",
            condition="GRADUATING",
            justification="Synthetic final-term evidence",
        ),
    )
