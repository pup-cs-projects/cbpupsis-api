"""In-memory schedule documents. Renderers never access the database."""

import hashlib
import uuid
from datetime import UTC, datetime, time, timedelta
from html import escape
from io import BytesIO
from pathlib import Path
from zoneinfo import ZoneInfo

import reportlab
from icalendar import Calendar, Event, Timezone
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from cbpupsis_api_student.domains.schedule.constants import MAX_EVENT_HISTORY
from cbpupsis_api_student.domains.schedule.exceptions import ScheduleUnavailableError
from cbpupsis_api_student.domains.schedule.schemas import ScheduleRead
from cbpupsis_shared.domains.academics.schemas import HolidayRead

MANILA = ZoneInfo("Asia/Manila")
WEEKDAYS = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")
DAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
FONT_DIR = Path(reportlab.__file__).parent / "fonts"
pdfmetrics.registerFont(TTFont("ScheduleSans", str(FONT_DIR / "Vera.ttf")))
pdfmetrics.registerFont(TTFont("ScheduleSansBold", str(FONT_DIR / "VeraBd.ttf")))


def render_pdf(schedule: ScheduleRead) -> bytes:
    """Render a printable table with wrapping, repeat headers, and Unicode names."""
    buffer = BytesIO()
    style = ParagraphStyle("Schedule", fontName="ScheduleSans", fontSize=8, leading=11)
    heading = ParagraphStyle(
        "Heading", parent=style, fontName="ScheduleSansBold", fontSize=15, leading=20
    )

    def paragraph(value: object) -> Paragraph:
        return Paragraph(escape(str(value), quote=False).replace("\n", "<br/>"), style)

    term = schedule.term
    term_label = f"{term.year} / {term.semester}" if term else "Term unavailable"
    story = [
        Paragraph("Polytechnic University of the Philippines", heading),
        paragraph("Student Course Schedule"),
        Spacer(1, 10),
        paragraph(f"{schedule.student_name} | {schedule.student_number}"),
        paragraph(f"Term: {term_label}"),
        Spacer(1, 14),
    ]
    rows = [
        [
            paragraph(label)
            for label in (
                "Code",
                "Course title",
                "Section",
                "Meetings (Asia/Manila)",
                "Room",
                "Faculty",
                "Units",
                "Lecture\nh/wk",
                "Lab h/wk",
                "Status",
            )
        ]
    ]
    for course in schedule.courses:
        meetings = [
            f"{DAY_NAMES[meeting.day_of_week]} "
            f"{meeting.start_time:%H:%M}-{meeting.end_time:%H:%M} "
            f"{meeting.type} ({meeting.room or 'TBA'})"
            for meeting in course.meetings
        ]
        cells = [
            course.code,
            course.title,
            course.section,
            "\n".join(meetings) or "TBA",
            course.room or "TBA",
            course.faculty_name or "TBA",
            course.units,
            course.lecture_hours,
            course.lab_hours,
            course.status,
        ]
        rows.append(
            [
                Paragraph(escape(str(value), quote=False).replace("\n", "<br/>"), style)
                for value in cells
            ]
        )
    if schedule.courses:
        table = Table(
            rows,
            colWidths=[55, 140, 64, 150, 55, 98, 35, 46, 42, 81],
            repeatRows=1,
            splitInRow=1,
        )
        table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eee6e6")),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#aaaaaa")),
                    ("TOPPADDING", (0, 0), (-1, -1), 6),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                ]
            )
        )
        story.append(table)
    else:
        story.append(paragraph(schedule.message or "No courses for this term."))
    story.extend(
        [
            Spacer(1, 12),
            paragraph(
                f"Total units: {schedule.total_credits} | "
                f"Weekly lecture hours: {schedule.total_lecture_hours} | "
                f"Weekly laboratory hours: {schedule.total_lab_hours}"
            ),
        ]
    )

    def footer(canvas, doc):
        canvas.setFont("ScheduleSans", 8)
        canvas.drawString(36, 20, f"{term_label} | Page {doc.page}")

    SimpleDocTemplate(
        buffer,
        pagesize=landscape(A4),
        leftMargin=36,
        rightMargin=36,
        topMargin=32,
        bottomMargin=36,
        title="Student Course Schedule",
        author="Polytechnic University of the Philippines",
    ).build(story, onFirstPage=footer, onLaterPages=footer)
    return buffer.getvalue()


def calendar_events(
    schedule: ScheduleRead, student_id: uuid.UUID, holidays: list[HolidayRead]
) -> dict[str, dict]:
    """Represent each distinct weekly meeting without exposing student identifiers."""
    if schedule.term is None:
        return {}
    term = schedule.term
    if len(holidays) > 1000:
        raise ScheduleUnavailableError
    closed = set()
    for holiday in holidays:
        if holiday.end_date is not None and holiday.end_date < holiday.start_date:
            raise ScheduleUnavailableError
        start = max(holiday.start_date, term.start_date)
        end = min(holiday.end_date or holiday.start_date, term.end_date)
        closed.update(
            start + timedelta(days=day) for day in range((end - start).days + 1)
        )
    events = {}
    for course in schedule.courses:
        for meeting in course.meetings:
            first = term.start_date + timedelta(
                days=(meeting.day_of_week - term.start_date.weekday()) % 7
            )
            if first > term.end_date:
                continue
            uid = (
                hashlib.sha256(
                    f"{student_id}:{term.id}:{course.section_id}:{meeting.id}".encode()
                ).hexdigest()
                + "@schedule.cbpupsis"
            )
            events[uid] = {
                "start": datetime.combine(
                    first, meeting.start_time, MANILA
                ).isoformat(),
                "end": datetime.combine(first, meeting.end_time, MANILA).isoformat(),
                "until": datetime.combine(term.end_date, time.max, MANILA)
                .astimezone(UTC)
                .isoformat(),
                "weekday": WEEKDAYS[meeting.day_of_week],
                "summary": f"{course.code}: {course.title} ({meeting.type})",
                "location": meeting.room or "TBA",
                "description": (
                    f"Section: {course.section}\n"
                    f"Faculty: {course.faculty_name or 'TBA'}"
                ),
                "excluded": [
                    datetime.combine(day, meeting.start_time, MANILA).isoformat()
                    for day in sorted(closed)
                    if day.weekday() == meeting.day_of_week and day >= first
                ],
                "cancelled": False,
            }
    return events


def merge_events(
    previous: dict[str, dict], current: dict[str, dict]
) -> dict[str, dict]:
    """Retain tombstones so files exported before earlier drops are superseded."""
    merged = {
        uid: {**value, "cancelled": True}
        for uid, value in previous.items()
        if uid not in current
    }
    merged.update(current)
    if len(merged) > MAX_EVENT_HISTORY:
        raise ScheduleUnavailableError
    return merged


def render_calendar(
    events: dict[str, dict], sequence: int, changed_at: datetime
) -> bytes:
    """Serialize RFC 5545 content with timezone, UTC UNTIL, and stable revisions."""
    calendar = Calendar()
    calendar.add("prodid", "-//CBPUPSIS//Student Schedule//EN")
    calendar.add("version", "2.0")
    calendar.add("calscale", "GREGORIAN")
    if events:
        first = min(
            datetime.fromisoformat(value["start"]).date() for value in events.values()
        )
        last = max(
            datetime.fromisoformat(value["until"]).astimezone(MANILA).date()
            for value in events.values()
        )
        calendar.add_component(
            Timezone.from_tzinfo(MANILA, first_date=first, last_date=last)
        )
    stamp = (
        changed_at.replace(tzinfo=UTC)
        if changed_at.tzinfo is None
        else changed_at.astimezone(UTC)
    )
    for uid, value in sorted(events.items()):
        event = Event()
        event.add("uid", uid)
        event.add("sequence", sequence)
        event.add("dtstamp", stamp)
        event.add("last-modified", stamp)
        event.add("dtstart", datetime.fromisoformat(value["start"]).astimezone(MANILA))
        event.add("dtend", datetime.fromisoformat(value["end"]).astimezone(MANILA))
        event.add(
            "rrule",
            {
                "freq": "weekly",
                "byday": value["weekday"],
                "until": datetime.fromisoformat(value["until"]),
            },
        )
        for excluded in value["excluded"]:
            event.add("exdate", datetime.fromisoformat(excluded).astimezone(MANILA))
        event.add("summary", value["summary"])
        event.add("location", value["location"])
        event.add("description", value["description"])
        event.add("status", "CANCELLED" if value["cancelled"] else "CONFIRMED")
        calendar.add_component(event)
    return calendar.to_ical()
