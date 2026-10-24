"""Synthetic schedule fixtures usable by SQLite and isolated PostgreSQL."""

import uuid
from datetime import UTC, date, datetime, time
from decimal import Decimal
from types import SimpleNamespace

from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_database.models.academics import AcademicEvent, AcademicTerm
from cbpupsis_database.models.curriculum import Course, Program
from cbpupsis_database.models.enrollment import StudentCourseEnrollment, TermEnrollment
from cbpupsis_database.models.sections import CourseSection, SectionSchedule
from cbpupsis_database.models.users import StudentProfile, User, UserProfile

NUMBER = "2021-00123-MN-0"
PASSWORD = "Synthetic-Student-Password123!"


async def seed_schedule(db: AsyncSession, password_hash: str, course_count: int = 2):
    suffix = uuid.uuid4().hex[:8]
    program = Program(
        code=f"CS-{suffix}",
        name="Computer Science",
        department="CS",
        total_units_required=Decimal(120),
    )
    student = User(
        email=f"student-{suffix}@example.com",
        password_hash=password_hash,
        full_name="Ana Peña",
        email_verified_at=datetime.now(UTC),
    )
    faculty = User(
        email=f"faculty-{suffix}@example.com",
        password_hash=password_hash,
        full_name="Synthetic Faculty",
    )
    now = datetime(2026, 8, 1, tzinfo=UTC)
    term = AcademicTerm(
        academic_year="2026-2027",
        semester="first",
        start_date=date(2026, 8, 3),
        end_date=date(2026, 12, 18),
        is_current=True,
        regular_enrollment_start=now,
        regular_enrollment_end=now,
        add_drop_deadline=now,
        grade_submission_deadline=now,
    )
    db.add_all([program, student, faculty, term])
    await db.flush()
    enrollment = TermEnrollment(
        student_id=student.id, academic_term_id=term.id, enrollment_status="enrolled"
    )
    db.add_all(
        [
            enrollment,
            StudentProfile(
                user_id=student.id,
                student_number=NUMBER,
                program_id=program.id,
                year_level=1,
            ),
            UserProfile(
                user_id=student.id,
                first_name="Ana",
                last_name="Peña",
                birthdate=date(2003, 5, 14),
            ),
            AcademicEvent(
                academic_term_id=term.id,
                title="Synthetic holiday",
                event_type="holiday",
                start_date=date(2026, 8, 10),
                end_date=date(2026, 8, 12),
            ),
        ]
    )
    await db.flush()
    section_ids, registration_ids, meeting_ids = [], [], []
    for index in range(course_count):
        course = Course(
            course_code=f"CS{index + 1:03}",
            title="Algorithms & <Practice>" if index == 0 else f"Course {index + 1}",
            lecture_units=Decimal(2),
            lab_units=Decimal(1),
        )
        db.add(course)
        await db.flush()
        section = CourseSection(
            academic_term_id=term.id,
            course_id=course.id,
            faculty_id=faculty.id,
            section_name=f"BSCS 1-{index + 1}",
            capacity=40,
        )
        db.add(section)
        await db.flush()
        registration = StudentCourseEnrollment(
            term_enrollment_id=enrollment.id,
            academic_term_id=term.id,
            section_id=section.id,
        )
        lecture = SectionSchedule(
            section_id=section.id,
            day_of_week=0,
            start_time=time(8 + index % 6),
            end_time=time(10 + index % 6),
            room="R101",
            schedule_type="lecture",
        )
        lab = SectionSchedule(
            section_id=section.id,
            day_of_week=2,
            start_time=time(13),
            end_time=time(16),
            room="LAB2",
            schedule_type="lab",
        )
        db.add_all([registration, lecture, lab])
        await db.flush()
        section_ids.append(section.id)
        registration_ids.append(registration.id)
        meeting_ids.extend([lecture.id, lab.id])
    result = SimpleNamespace(
        student_id=student.id,
        faculty_id=faculty.id,
        term_id=term.id,
        enrollment_id=enrollment.id,
        section_ids=section_ids,
        registration_ids=registration_ids,
        meeting_ids=meeting_ids,
    )
    await db.commit()
    return result
