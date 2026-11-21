"""US-STU-003: AC-103.1 through AC-103.8 and private export failures."""

import asyncio
import logging
import threading
import time
from datetime import UTC, date, datetime, timedelta
from datetime import time as local_time
from decimal import Decimal
from io import BytesIO

import pytest
from httpx import ASGITransport, AsyncClient
from icalendar import Calendar
from pypdf import PdfReader
from sqlalchemy import delete, func, select, update

from cbpupsis_api_student.domains.schedule import exports, service
from cbpupsis_api_student.domains.schedule.constants import READ_SCHEDULE
from cbpupsis_core.logging import JsonFormatter
from cbpupsis_database.models.academics import AcademicEvent, AcademicTerm
from cbpupsis_database.models.enrollment import StudentCourseEnrollment, TermEnrollment
from cbpupsis_database.models.iam import PolicyPermission
from cbpupsis_database.models.schedule import ScheduleExportState
from cbpupsis_database.models.sections import SectionSchedule
from cbpupsis_database.models.users import StudentProfile, User
from cbpupsis_shared.domains.auth.dependencies import CurrentUser, get_current_user
from cbpupsis_shared.domains.auth.security import hash_password
from main import app
from tests.domains.schedule.helpers import NUMBER, PASSWORD, seed_schedule

URL = "/api/v1/student/schedule"
GUARDED_PATHS = [
    URL,
    f"{URL}/export?format=pdf",
    f"{URL}/export?format=ics",
]


@pytest.fixture(scope="session")
def password_hash():
    return hash_password(PASSWORD)


@pytest.fixture
async def setup(db, client, grant, password_hash):
    data = await seed_schedule(db, password_hash)
    await grant(data.student_id, READ_SCHEDULE)
    response = await client.post(
        "/api/v1/auth/student/login",
        json={
            "student_number": NUMBER,
            "birthdate": "2003-05-14",
            "password": PASSWORD,
        },
    )
    assert response.status_code == 200, response.text
    data.headers = {"Authorization": f"Bearer {response.json()['access_token']}"}
    return data


@pytest.fixture
async def handler_client(client):
    """Share dependency overrides but observe global-handler 500 responses."""
    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as ac:
        yield ac


def events(response):
    assert response.status_code == 200, response.text
    return Calendar.from_ical(response.content).walk("VEVENT")


async def test_ac_103_1_fields_and_ac_103_4_computed_totals(client, setup):
    response = await client.get(URL, headers=setup.headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {
        "student_name",
        "student_number",
        "term",
        "courses",
        "total_credits",
        "total_lecture_hours",
        "total_lab_hours",
        "state",
        "message",
    }
    assert body["student_name"] == "Ana Peña" and body["student_number"] == NUMBER
    assert set(body["term"]) == {
        "id",
        "year",
        "semester",
        "start_date",
        "end_date",
    }
    assert body["term"]["year"] == "2026-2027"
    assert len(body["courses"]) == 2
    course = body["courses"][0]
    assert {
        "code",
        "title",
        "section",
        "section_id",
        "room",
        "faculty_name",
        "units",
        "status",
        "lecture_hours",
        "lab_hours",
        "meetings",
    } == course.keys()
    for meeting in course["meetings"]:
        assert set(meeting) == {
            "id",
            "day_of_week",
            "start_time",
            "end_time",
            "room",
            "type",
        }
    assert course["room"] == "LAB2, R101"
    assert course["lecture_hours"] == "2.0" or Decimal(course["lecture_hours"]) == 2
    assert Decimal(course["lab_hours"]) == 3
    assert [meeting["type"] for meeting in course["meetings"]] == [
        "lecture",
        "laboratory",
    ]
    assert [
        Decimal(body[key])
        for key in ("total_credits", "total_lecture_hours", "total_lab_hours")
    ] == [6, 4, 6]
    assert response.headers["cache-control"] == "private, no-store"


async def test_ac_103_2_historical_empty_and_missing_terms(client, setup, db):
    current = await db.get(AcademicTerm, setup.term_id)
    current.is_current = False
    await db.commit()
    missing = await client.get(URL, headers=setup.headers)
    assert missing.status_code == 200 and missing.json()["state"] == "termUnavailable"
    historical = await client.get(
        URL, params={"year": "2026-2027", "semester": "first"}, headers=setup.headers
    )
    assert historical.status_code == 200 and len(historical.json()["courses"]) == 2
    for params in (
        {"year": "2025-2026", "semester": "first"},
        {"year": "2026-2027", "semester": "first"},
    ):
        await db.execute(update(TermEnrollment).values(enrollment_status="pending"))
        await db.commit()
        response = await client.get(URL, params=params, headers=setup.headers)
        body = response.json()
        assert response.status_code == 200 and body["courses"] == [] and body["message"]
        assert Decimal(body["total_credits"]) == 0


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("cs001", 1),
        ("algorithms", 1),
        ("synthetic faculty", 2),
        ("no match", 0),
        ("", 2),
        ("  ", 2),
    ],
)
async def test_ac_103_3_filter_does_not_mutate_enrollment(
    client, setup, db, query, expected
):
    response = await client.get(URL, params={"q": query}, headers=setup.headers)
    assert response.status_code == 200 and len(response.json()["courses"]) == expected
    assert Decimal(response.json()["total_credits"]) == expected * 3
    assert (
        await db.scalar(select(func.count()).select_from(StudentCourseEnrollment)) == 2
    )
    assert len((await client.get(URL, headers=setup.headers)).json()["courses"]) == 2


async def test_ac_103_5_pdf_identity_full_table_and_private_headers(client, setup):
    response = await client.get(
        f"{URL}/export",
        params={"format": "pdf", "q": "nonexistent"},
        headers=setup.headers,
    )
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/pdf"
    assert response.headers["content-disposition"].startswith("attachment;")
    assert response.headers["cache-control"] == "private, no-store"
    text = "\n".join(
        page.extract_text() for page in PdfReader(BytesIO(response.content)).pages
    )
    for value in (
        "Polytechnic University",
        "Ana Peña",
        NUMBER,
        "2026-2027",
        "first",
        "CS001",
        "CS002",
        "Algorithms & <Practice>",
        "LAB2",
        "R101",
        "Synthetic Faculty",
        "enrolled",
    ):
        assert value in text


async def test_ac_103_6_recurrence_timezone_holidays_and_distinct_meetings(
    client, setup
):
    response = await client.get(
        f"{URL}/export", params={"format": "ics"}, headers=setup.headers
    )
    assert response.headers["content-type"].startswith("text/calendar")
    parsed = events(response)
    assert len(parsed) == 4
    assert len({str(event["uid"]) for event in parsed}) == 4
    for event in parsed:
        assert event["dtstart"].params["TZID"] == "Asia/Manila"
        assert event.decoded("dtstart").date() >= date(2026, 8, 3)
        assert event["rrule"]["FREQ"] == ["WEEKLY"]
        assert event["rrule"]["UNTIL"][0].astimezone(exports.MANILA).date() == date(
            2026, 12, 18
        )
        excluded = event["exdate"]
        values = excluded if isinstance(excluded, list) else [excluded]
        assert {value.dt.date() for item in values for value in item.dts} <= {
            date(2026, 8, 10),
            date(2026, 8, 12),
        }
        assert int(event["sequence"]) == 1
        assert NUMBER not in str(event["uid"])


async def test_ac_stu_003_6_calendar_client_fixture_tzid_until_exdate_sequence(
    client, setup, db, tmp_path
):
    """#156 wire regression and import fixtures, not calendar-client acceptance."""
    # Keep one lecture in CS001 and one lab in CS002, so dropping CS001 leaves
    # an independently observable series. Start midweek to exercise alignment.
    await db.execute(
        delete(SectionSchedule).where(
            SectionSchedule.id.in_([setup.meeting_ids[1], setup.meeting_ids[2]])
        )
    )
    await db.execute(
        update(AcademicTerm)
        .where(AcademicTerm.id == setup.term_id)
        .values(start_date=date(2026, 8, 4))
    )
    await db.execute(
        update(AcademicEvent)
        .where(AcademicEvent.academic_term_id == setup.term_id)
        .values(start_date=date(2026, 8, 17), end_date=date(2026, 8, 17))
    )
    await db.commit()

    async def exported(filename):
        response = await client.get(
            f"{URL}/export", params={"format": "ics"}, headers=setup.headers
        )
        parsed = events(response)
        assert response.headers["content-type"].startswith("text/calendar")
        assert response.headers["content-disposition"] == (
            'attachment; filename="course-schedule.ics"'
        )
        assert response.headers["cache-control"] == "private, no-store"
        assert len(parsed) == 2
        assert len({str(event["uid"]) for event in parsed}) == 2
        assert NUMBER.encode() not in response.content
        (tmp_path / filename).write_bytes(response.content)
        return {str(event["summary"]).split(":", 1)[0]: event for event in parsed}

    first = await exported("01-before-drop.ics")
    assert set(first) == {"CS001", "CS002"}
    expected = {
        "CS001": (
            date(2026, 8, 10),
            local_time(8),
            local_time(10),
            "MO",
            date(2026, 12, 14),
        ),
        "CS002": (
            date(2026, 8, 5),
            local_time(13),
            local_time(16),
            "WE",
            date(2026, 12, 16),
        ),
    }
    for code, event in first.items():
        start_date, start_time, end_time, weekday, last_date = expected[code]
        start = datetime.combine(start_date, start_time, exports.MANILA)
        assert event["dtstart"].params["TZID"] == "Asia/Manila"
        assert event["dtend"].params["TZID"] == "Asia/Manila"
        assert event.decoded("dtstart") == start
        assert event.decoded("dtend") == datetime.combine(
            start_date, end_time, exports.MANILA
        )
        assert event["rrule"]["FREQ"] == ["WEEKLY"]
        assert event["rrule"]["BYDAY"] == [weekday]
        until = event["rrule"]["UNTIL"][0]
        assert until == datetime(2026, 12, 18, 15, 59, 59, tzinfo=UTC)
        assert b"UNTIL=20261218T155959Z" in event.to_ical()
        assert int(event["sequence"]) == 1
        assert event["status"] == "CONFIRMED"
        excluded = set()
        if code == "CS001":
            exdate = event["exdate"]
            assert exdate.params["TZID"] == "Asia/Manila"
            excluded = {item.dt for item in exdate.dts}
            assert excluded == {datetime(2026, 8, 17, 8, tzinfo=exports.MANILA)}
        else:
            assert "exdate" not in event
        # Enumerate this explicitly weekly rule; these are wire expectations,
        # not evidence that an external importer actually honors EXDATE/UNTIL.
        occurrences = []
        occurrence = start
        while occurrence <= until:
            if occurrence not in excluded:
                occurrences.append(occurrence)
            occurrence += timedelta(weeks=1)
        assert occurrences[0] == start
        assert occurrences[-1].date() == last_date
        assert all(
            date(2026, 8, 4) <= item.date() <= date(2026, 12, 18)
            for item in occurrences
        )
        assert all(item.date() != date(2026, 8, 17) for item in occurrences)

    unchanged = await exported("02-unchanged.ics")
    for code, event in unchanged.items():
        assert event.to_ical() == first[code].to_ical()

    # Synthetic committed drop, not an invocation of the approval workflow.
    await db.execute(
        update(StudentCourseEnrollment)
        .where(StudentCourseEnrollment.id == setup.registration_ids[0])
        .values(status="dropped")
    )
    await db.commit()
    dropped = await exported("03-after-drop.ics")
    assert set(dropped) == set(first)
    assert dropped["CS001"]["status"] == "CANCELLED"
    assert dropped["CS002"]["status"] == "CONFIRMED"
    for code, event in dropped.items():
        assert event["uid"] == first[code]["uid"]
        assert int(event["sequence"]) == 2
        for field in ("dtstart", "dtend", "rrule"):
            assert event[field].to_ical() == first[code][field].to_ical()
        if code == "CS001":
            assert event["exdate"].to_ical() == first[code]["exdate"].to_ical()
    print(f"Issue #156 synthetic import files: {tmp_path}")


async def test_ac_stu_003_7_foreign_and_absent_identified_routes_return_404(
    client, setup, db, password_hash
):
    foreign = User(email="foreign@example.com", password_hash=password_hash)
    db.add(foreign)
    await db.flush()
    program_id = await db.scalar(
        select(StudentProfile.program_id).where(
            StudentProfile.user_id == setup.student_id
        )
    )
    db.add(
        StudentProfile(
            user_id=foreign.id,
            student_number="2021-00999-MN-1",
            program_id=program_id,
            year_level=1,
        )
    )
    await db.commit()
    bodies = []
    for number in (NUMBER, "2021-00999-MN-1", "not-a-real-student"):
        response = await client.get(
            f"/api/v1/students/{number}/schedule", headers=setup.headers
        )
        assert response.status_code == 404
        body = response.json()
        body.pop("request_id", None)
        bodies.append(body)
    assert bodies[0] == bodies[1] == bodies[2]


async def test_ac_103_8_drop_readd_stable_uids_monotonic_sequence_and_tombstones(
    client, setup, db
):
    async def exported():
        return {
            str(event["uid"]): event
            for event in events(
                await client.get(
                    f"{URL}/export", params={"format": "ics"}, headers=setup.headers
                )
            )
        }

    first = await exported()
    assert all(int(event["sequence"]) == 1 for event in (await exported()).values())
    await db.execute(
        update(StudentCourseEnrollment)
        .where(StudentCourseEnrollment.id == setup.registration_ids[0])
        .values(status="dropped")
    )
    await db.commit()
    assert len((await client.get(URL, headers=setup.headers)).json()["courses"]) == 1
    dropped = await exported()
    assert set(dropped) == set(first)
    assert all(int(event["sequence"]) == 2 for event in dropped.values())
    assert sum(event["status"] == "CANCELLED" for event in dropped.values()) == 2
    await db.execute(
        update(StudentCourseEnrollment)
        .where(StudentCourseEnrollment.id == setup.registration_ids[0])
        .values(status="enrolled")
    )
    await db.commit()
    readded = await exported()
    assert set(readded) == set(first)
    assert all(
        int(event["sequence"]) == 3 and event["status"] == "CONFIRMED"
        for event in readded.values()
    )


@pytest.mark.parametrize("path", GUARDED_PATHS)
async def test_no_session_returns_401(client, path):
    response = await client.get(path)
    assert response.status_code == 401
    assert response.headers["cache-control"] == "private, no-store"
    assert not response.content.startswith(b"%PDF")


@pytest.mark.parametrize("path", GUARDED_PATHS)
async def test_requires_permission_and_student_role(client, setup, path):
    for user in (
        CurrentUser(id=setup.faculty_id, email="faculty@example.com", role="faculty"),
        CurrentUser(id=setup.student_id, email="student@example.com", role="faculty"),
    ):
        app.dependency_overrides[get_current_user] = lambda principal=user: principal
        assert (await client.get(path)).status_code == 403


@pytest.mark.parametrize("path", GUARDED_PATHS)
async def test_live_student_without_read_schedule_is_denied(client, setup, db, path):
    await db.execute(delete(PolicyPermission))
    await db.commit()
    response = await client.get(path, headers=setup.headers)
    assert response.status_code == 403
    assert response.headers["cache-control"] == "private, no-store"
    assert "content-disposition" not in response.headers


@pytest.mark.parametrize(
    "params",
    [
        {"year": "2026-2027"},
        {"semester": "first"},
        {"year": " ", "semester": "first"},
        {"q": "x" * 201},
    ],
)
async def test_invalid_selectors_and_filter_are_422(client, setup, params):
    assert (
        await client.get(URL, params=params, headers=setup.headers)
    ).status_code == 422


async def test_invalid_export_format_is_422(client, setup):
    assert (
        await client.get(f"{URL}/export?format=zip", headers=setup.headers)
    ).status_code == 422


@pytest.mark.parametrize("format", ["pdf", "ics"])
async def test_unexpected_export_failure_reaches_global_handler_and_rolls_back(
    handler_client, setup, db, monkeypatch, caplog, format
):
    private_values = (
        "private-student-data-must-not-be-logged",
        NUMBER,
        str(setup.student_id),
        "Ana Peña",
    )

    def fail(*args):
        raise RuntimeError(" ".join(private_values))

    monkeypatch.setattr(
        exports, "render_pdf" if format == "pdf" else "render_calendar", fail
    )
    response = await handler_client.get(
        f"{URL}/export?format={format}", headers=setup.headers
    )
    assert response.status_code == 500
    assert response.json()["detail"] == "Internal server error"
    assert response.json()["request_id"]
    assert "retry-after" not in response.headers
    assert response.headers["cache-control"] == "private, no-store"
    assert "content-disposition" not in response.headers
    records = [
        record
        for record in caplog.records
        if record.name == "cbpupsis_core.exceptions"
        and record.getMessage() == "request.unexpected_error"
    ]
    assert len(records) == 1
    assert records[0].error_type == "RuntimeError"
    assert records[0].levelno == logging.ERROR
    assert records[0].exc_info is None and records[0].stack_info is None
    logs = "\n".join(JsonFormatter().format(record) for record in caplog.records)
    for value in private_values:
        assert value not in logs
        assert value not in caplog.text
        assert value not in response.text
    assert not db.in_transaction()
    assert await db.scalar(select(func.count()).select_from(ScheduleExportState)) == 0


@pytest.mark.parametrize("format", ["pdf", "ics"])
async def test_generation_deadline_rolls_back_slow_worker_and_recovers(
    client, setup, db, monkeypatch, format
):
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    renderer_name = "render_pdf" if format == "pdf" else "render_calendar"
    original = getattr(exports, renderer_name)

    def slow(*args):
        entered.set()
        try:
            release.wait(timeout=3)
            return original(*args)
        finally:
            finished.set()

    monkeypatch.setattr(service, "EXPORT_TIMEOUT_SECONDS", 0.5)
    monkeypatch.setattr(exports, renderer_name, slow)
    try:
        response = await client.get(
            f"{URL}/export?format={format}", headers=setup.headers
        )
        assert entered.is_set()
        assert response.status_code == 503
        assert response.json()["code"] == "SERVICE_UNAVAILABLE"
        assert response.headers["retry-after"] == "30"
        assert response.headers["cache-control"] == "private, no-store"
        assert "content-disposition" not in response.headers
        assert not db.in_transaction()
        assert (
            await db.scalar(select(func.count()).select_from(ScheduleExportState)) == 0
        )
    finally:
        release.set()
        assert await asyncio.to_thread(finished.wait, 3)
    monkeypatch.setattr(exports, renderer_name, original)
    monkeypatch.setattr(service, "EXPORT_TIMEOUT_SECONDS", 10)
    recovered = await client.get(f"{URL}/export?format={format}", headers=setup.headers)
    assert recovered.status_code == 200
    if format == "ics":
        assert all(int(event["sequence"]) == 1 for event in events(recovered))


async def test_invalid_meeting_and_ambiguous_current_term(db, client, setup):
    await db.execute(
        update(SectionSchedule)
        .where(SectionSchedule.id == setup.meeting_ids[0])
        .values(day_of_week=7)
    )
    await db.commit()
    assert (await client.get(URL, headers=setup.headers)).status_code == 503
    original = await db.get(AcademicTerm, setup.term_id)
    db.add(
        AcademicTerm(
            academic_year="2025-2026",
            semester="first",
            start_date=date(2025, 8, 1),
            end_date=date(2025, 12, 1),
            is_current=True,
            regular_enrollment_start=original.regular_enrollment_start,
            regular_enrollment_end=original.regular_enrollment_end,
            add_drop_deadline=original.add_drop_deadline,
            grade_submission_deadline=original.grade_submission_deadline,
        )
    )
    await db.commit()
    assert (await client.get(URL, headers=setup.headers)).json()[
        "state"
    ] == "termUnavailable"


async def test_twelve_course_pdf_under_three_seconds(db, password_hash, tmp_path):
    data = await seed_schedule(db, password_hash, course_count=12)
    started = time.perf_counter()
    content = await service.export_schedule(
        db, student_id=data.student_id, format="pdf"
    )
    assert time.perf_counter() - started < 3
    assert len(PdfReader(BytesIO(content)).pages) >= 1
    (tmp_path / "schedule-12-courses.pdf").write_bytes(content)


async def test_ac_103_7_request_logs_do_not_contain_student_identifiers(
    client, setup, caplog
):
    with caplog.at_level(logging.INFO, logger="cbpupsis_core.middleware.http"):
        await client.get(f"/api/v1/students/{NUMBER}/schedule", headers=setup.headers)
        await client.get(
            f"/api/v1/students/{NUMBER}/nonexistent", headers=setup.headers
        )
    records = [
        record
        for record in caplog.records
        if record.name == "cbpupsis_core.middleware.http"
    ]
    assert records
    assert all(NUMBER not in str(record.__dict__) for record in records)
    assert {record.path for record in records} == {
        "<unmatched>",
    }
