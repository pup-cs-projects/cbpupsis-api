"""Give the demo student something to show: a profile, contacts, and a term of classes.

``seed_demo`` creates the accounts; this fills them in, so a demo of the student
portal shows real rows read through the API rather than empty states. Fictional
data only. Run it after ``seed_demo``, never against production::

    uv run python -m scripts.seed_demo_data

What it writes:

- The demo student's contact details and two emergency contacts (one primary).
- A current term, reusing the one already marked current when there is one: a
  second current term would make every student's schedule ambiguous.
- Six BSCS first-year courses, each with a section taught by the demo faculty
  account and weekly meetings (two have a separate laboratory slot).
- The demo student's official enrollment in all six.
- Calendar events for the term and a few published announcements.

Idempotent, and a re-run is also the reset: rows are found by their natural keys
(course code, section name, announcement title), and the demo student's contacts
and enrollment are rewritten to the list below.
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

from sqlalchemy import delete, select

from cbpupsis_core.config import settings
from cbpupsis_database.models.academics import AcademicEvent, AcademicTerm
from cbpupsis_database.models.announcements import Announcement
from cbpupsis_database.models.curriculum import Course
from cbpupsis_database.models.enrollment import (
    StudentCourseEnrollment,
    TermEnrollment,
)
from cbpupsis_database.models.sections import CourseSection, SectionSchedule
from cbpupsis_database.models.users import (
    EmergencyContact,
    StudentProfile,
    User,
    UserProfile,
)
from cbpupsis_database.session import AsyncSessionLocal

STUDENT_EMAIL = "demo-student@example.com"
FACULTY_EMAIL = "demo-faculty@example.com"
ADMIN_EMAIL = "demo-admin@example.com"

#: Used only when no term is marked current.
TERM_YEAR = "2026-2027"
TERM_SEMESTER = "first"
TERM_START = date(2026, 8, 24)
TERM_END = date(2026, 12, 19)

SECTION = "BSCS 1-1"

#: (code, title, lecture units, lab units, meetings: (day, start, end, room, type)).
#: day_of_week: Monday = 0.
COURSES: list[tuple[str, str, str, str, list[tuple[int, str, str, str, str]]]] = [
    (
        "COMP 001",
        "Introduction to Computing",
        "2",
        "1",
        [
            (0, "07:30", "09:30", "S508 Main", "lecture"),
            (2, "07:30", "10:30", "CL 3 Main", "laboratory"),
        ],
    ),
    (
        "COMP 002",
        "Computer Programming 1",
        "2",
        "1",
        [
            (1, "10:30", "12:30", "S511 Main", "lecture"),
            (3, "10:30", "13:30", "CL 1 Main", "laboratory"),
        ],
    ),
    (
        "MATH 101",
        "Mathematics in the Modern World",
        "3",
        "0",
        [
            (0, "13:00", "14:30", "N302 Main", "lecture"),
            (3, "13:00", "14:30", "N302 Main", "lecture"),
        ],
    ),
    (
        "GEED 004",
        "Purposive Communication",
        "3",
        "0",
        [(1, "15:00", "18:00", "W405 Main", "lecture")],
    ),
    (
        "GEED 005",
        "Understanding the Self",
        "3",
        "0",
        [(4, "07:30", "10:30", "E210 Main", "lecture")],
    ),
    (
        "PATHFIT 1",
        "Movement Competency Training",
        "2",
        "0",
        [(5, "08:00", "10:00", "PUP Gymnasium", "lecture")],
    ),
]

PROFILE = {
    "phone_number": "09171234567",
    "personal_email": "demo.student.personal@example.com",
    "home_address": "123 Mabini Street, Barangay San Roque, Antipolo City, Rizal",
    "current_address": "Unit 4B, 1016 Anonas Street, Sta. Mesa, Manila",
}

CONTACTS = [
    {
        "contact_name": "Maria Santos",
        "relationship": "Mother",
        "phone_number": "09181234567",
        "email": "maria.santos@example.com",
        "address": "123 Mabini Street, Barangay San Roque, Antipolo City, Rizal",
        "is_primary": True,
    },
    {
        "contact_name": "Jose Santos",
        "relationship": "Father",
        "phone_number": "09191234567",
        "email": None,
        "address": "123 Mabini Street, Barangay San Roque, Antipolo City, Rizal",
        "is_primary": False,
    },
]

#: (title, offset in days from the term start, length in days, type).
EVENTS = [
    ("Start of Classes", 0, 1, "academic"),
    ("Last Day of Adding and Dropping", 14, 1, "deadline"),
    ("Midterm Examinations", 56, 5, "exam"),
    ("All Saints' Day", None, 1, "holiday"),
    ("Final Examinations", 105, 5, "exam"),
]

ANNOUNCEMENTS = [
    (
        "Midterm Examination Schedule Released",
        "The midterm examination schedule for the first semester is now posted. "
        "Bring your Certificate of Registration on exam days.",
        "ACADEMIC",
        True,
    ),
    (
        "Update Your Emergency Contacts",
        "Please review your emergency contacts in your Student Profile so the "
        "University can reach your family when needed.",
        "GENERAL",
        False,
    ),
    (
        "Library Extended Hours",
        "The Ninoy Aquino Library and Learning Resources Center is open until 9:00 PM "
        "on weekdays during the examination period.",
        "GENERAL",
        False,
    ),
]


def _clock(text: str) -> time:
    hour, minute = text.split(":")
    return time(int(hour), int(minute))


async def _user_id(session, email: str) -> uuid.UUID:
    user_id = await session.scalar(
        select(User.id).where(User.email == email, User.deleted_at.is_(None))
    )
    if user_id is None:
        sys.exit(f"{email} not found; run scripts.seed_demo first.")
    return user_id


async def _current_term(session) -> AcademicTerm:
    current = list(
        await session.scalars(
            select(AcademicTerm).where(AcademicTerm.is_current.is_(True))
        )
    )
    if len(current) > 1:
        sys.exit("More than one academic term is marked current; fix that first.")
    if current:
        return current[0]
    term = await session.scalar(
        select(AcademicTerm).where(
            AcademicTerm.academic_year == TERM_YEAR,
            AcademicTerm.semester == TERM_SEMESTER,
        )
    )
    if term is None:

        def at(day: date) -> datetime:
            return datetime(day.year, day.month, day.day, tzinfo=UTC)

        term = AcademicTerm(
            academic_year=TERM_YEAR,
            semester=TERM_SEMESTER,
            start_date=TERM_START,
            end_date=TERM_END,
            regular_enrollment_start=at(TERM_START - timedelta(days=21)),
            regular_enrollment_end=at(TERM_START - timedelta(days=1)),
            add_drop_deadline=at(TERM_START + timedelta(days=14)),
            grade_submission_deadline=at(TERM_END + timedelta(days=14)),
        )
        session.add(term)
    term.is_current = True
    await session.flush()
    return term


async def _course(session, code: str, title: str, lec: str, lab: str) -> Course:
    course = await session.scalar(select(Course).where(Course.course_code == code))
    if course is None:
        course = Course(course_code=code, title=title)
        session.add(course)
    course.lecture_units = Decimal(lec)
    course.lab_units = Decimal(lab)
    await session.flush()
    return course


async def _section(session, term, course, faculty_id, meetings) -> CourseSection:
    section = await session.scalar(
        select(CourseSection).where(
            CourseSection.academic_term_id == term.id,
            CourseSection.course_id == course.id,
            CourseSection.section_name == SECTION,
        )
    )
    if section is None:
        section = CourseSection(
            academic_term_id=term.id,
            course_id=course.id,
            section_name=SECTION,
            capacity=40,
        )
        session.add(section)
    section.faculty_id = faculty_id
    await session.flush()
    await session.execute(
        delete(SectionSchedule).where(SectionSchedule.section_id == section.id)
    )
    for day, start, end, room, kind in meetings:
        session.add(
            SectionSchedule(
                section_id=section.id,
                day_of_week=day,
                start_time=_clock(start),
                end_time=_clock(end),
                room=room,
                schedule_type=kind,
            )
        )
    return section


async def _profile(session, student_id: uuid.UUID) -> None:
    profile = await session.get(UserProfile, student_id)
    if profile is None:
        sys.exit("The demo student has no profile row; re-run scripts.seed_demo.")
    for column, value in PROFILE.items():
        setattr(profile, column, value)
    student = await session.get(StudentProfile, student_id)
    if student is not None:
        student.enrollment_status = "enrolled"
        student.section = SECTION
    await session.execute(
        delete(EmergencyContact).where(EmergencyContact.user_id == student_id)
    )
    for contact in CONTACTS:
        session.add(EmergencyContact(user_id=student_id, **contact))


async def _enroll(session, student_id, term, sections) -> None:
    enrollment = await session.scalar(
        select(TermEnrollment).where(
            TermEnrollment.student_id == student_id,
            TermEnrollment.academic_term_id == term.id,
        )
    )
    if enrollment is None:
        enrollment = TermEnrollment(student_id=student_id, academic_term_id=term.id)
        session.add(enrollment)
    enrollment.enrollment_status = "enrolled"
    await session.flush()
    await session.execute(
        delete(StudentCourseEnrollment).where(
            StudentCourseEnrollment.term_enrollment_id == enrollment.id
        )
    )
    for section in sections:
        session.add(
            StudentCourseEnrollment(
                term_enrollment_id=enrollment.id,
                academic_term_id=term.id,
                section_id=section.id,
                status="enrolled",
            )
        )


async def _calendar(session, term: AcademicTerm) -> None:
    for title, offset, days, kind in EVENTS:
        start = (
            date(term.start_date.year, 11, 1)
            if offset is None
            else term.start_date + timedelta(days=offset)
        )
        event = await session.scalar(
            select(AcademicEvent).where(
                AcademicEvent.academic_term_id == term.id, AcademicEvent.title == title
            )
        )
        if event is None:
            event = AcademicEvent(academic_term_id=term.id, title=title)
            session.add(event)
        event.start_date = start
        event.end_date = start + timedelta(days=days - 1) if days > 1 else None
        event.event_type = kind


async def _announcements(session, author_id: uuid.UUID) -> None:
    now = datetime.now(UTC)
    for index, (title, content, category, pinned) in enumerate(ANNOUNCEMENTS):
        row = await session.scalar(
            select(Announcement).where(Announcement.title == title)
        )
        if row is None:
            row = Announcement(author_id=author_id, title=title)
            session.add(row)
        row.content = content
        row.category = category
        row.is_pinned = pinned
        row.is_published = True
        row.published_at = now - timedelta(days=index)
        row.expires_at = None


async def seed_demo_data() -> int:
    if settings.environment == "production":
        print("Refusing to seed demo data in production.", file=sys.stderr)
        return 1
    async with AsyncSessionLocal() as session:
        student_id = await _user_id(session, STUDENT_EMAIL)
        faculty_id = await _user_id(session, FACULTY_EMAIL)
        admin_id = await _user_id(session, ADMIN_EMAIL)

        term = await _current_term(session)
        sections = []
        for code, title, lec, lab, meetings in COURSES:
            course = await _course(session, code, title, lec, lab)
            sections.append(await _section(session, term, course, faculty_id, meetings))
        await _profile(session, student_id)
        await _enroll(session, student_id, term, sections)
        await _calendar(session, term)
        await _announcements(session, admin_id)
        await session.commit()

    units = sum(Decimal(lec) + Decimal(lab) for _, _, lec, lab, _ in COURSES)
    print(
        f"Demo data seeded for {term.academic_year} {term.semester}: "
        f"{len(COURSES)} courses ({units} units) in {SECTION}, taught by "
        f"{FACULTY_EMAIL}; profile, {len(CONTACTS)} emergency contacts, "
        f"{len(EVENTS)} calendar events, {len(ANNOUNCEMENTS)} announcements."
    )
    return 0


def main() -> None:
    sys.exit(asyncio.run(seed_demo_data()))


if __name__ == "__main__":
    main()
