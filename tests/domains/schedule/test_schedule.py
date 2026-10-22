"""US-STU-003: AC-103.1 through AC-103.8 and private export failures."""

import asyncio
import logging
import threading
import time
from datetime import date
from decimal import Decimal
from io import BytesIO

import pytest
from icalendar import Calendar
from pypdf import PdfReader
from sqlalchemy import delete, func, select, update

from cbpupsis_api_student.domains.schedule import exports, service
from cbpupsis_api_student.domains.schedule.constants import READ_SCHEDULE
from cbpupsis_database.models.academics import AcademicTerm
from cbpupsis_database.models.enrollment import StudentCourseEnrollment, TermEnrollment
from cbpupsis_database.models.iam import PolicyPermission
from cbpupsis_database.models.schedule import ScheduleExportState
from cbpupsis_database.models.sections import SectionSchedule
from cbpupsis_database.models.users import StudentProfile, User
from cbpupsis_shared.domains.auth.dependencies import CurrentUser, get_current_user
from cbpupsis_shared.domains.auth.security import hash_password
from main import app
from tests.domains.schedule.helpers import NUMBER, PASSWORD, seed_schedule

URL = "/api/v1/students/me/schedule"
COMPAT_URL = "/api/v1/student/schedule"
GUARDED_PATHS = [
    path
    for base in (URL, COMPAT_URL)
    for path in (base, f"{base}/export?format=pdf", f"{base}/export?format=ics")
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


def events(response):
    assert response.status_code == 200, response.text
    return Calendar.from_ical(response.content).walk("VEVENT")


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"year": "2026-2027", "semester": "first", "q": "CS001"},
        {"year": "2025-2026", "semester": "first"},
        {"q": "no match"},
        {"q": "  "},
    ],
)
async def test_compatibility_read_matches_canonical_self_route(client, setup, params):
    canonical = await client.get(URL, params=params, headers=setup.headers)
    compatibility = await client.get(COMPAT_URL, params=params, headers=setup.headers)
    assert canonical.status_code == compatibility.status_code == 200
    assert canonical.json() == compatibility.json()
    assert compatibility.headers["cache-control"] == "private, no-store"
    assert "location" not in compatibility.headers


@pytest.mark.parametrize("format", ["pdf", "ics"])
async def test_compatibility_exports_share_complete_content_and_revisions(
    client, setup, format
):
    params = {
        "format": format,
        "year": "2026-2027",
        "semester": "first",
        "q": "no match",
    }
    canonical = await client.get(f"{URL}/export", params=params, headers=setup.headers)
    compatibility = await client.get(
        f"{COMPAT_URL}/export", params=params, headers=setup.headers
    )
    assert canonical.status_code == compatibility.status_code == 200
    for header in ("content-type", "content-disposition", "cache-control"):
        assert compatibility.headers[header] == canonical.headers[header]
    assert "location" not in compatibility.headers
    if format == "pdf":
        texts = [
            "\n".join(
                page.extract_text()
                for page in PdfReader(BytesIO(response.content)).pages
            )
            for response in (canonical, compatibility)
        ]
        assert texts[0] == texts[1]
        assert "CS001" in texts[1] and "CS002" in texts[1]
    else:
        revisions = [
            {str(event["uid"]): int(event["sequence"]) for event in events(response)}
            for response in (canonical, compatibility)
        ]
        assert revisions[0] == revisions[1]
        assert len(revisions[1]) == 4
        assert set(revisions[1].values()) == {1}


async def test_ac_103_1_fields_and_ac_103_4_computed_totals(client, setup):
    response = await client.get(URL, headers=setup.headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["studentName"] == "Ana Peña" and body["studentNumber"] == NUMBER
    assert body["term"]["year"] == "2026-2027"
    assert len(body["courses"]) == 2
    course = body["courses"][0]
    assert {
        "code",
        "title",
        "section",
        "sectionId",
        "room",
        "facultyName",
        "units",
        "status",
        "lectureHours",
        "labHours",
        "meetings",
    } <= course.keys()
    assert course["room"] == "LAB2, R101"
    assert course["lectureHours"] == "2.0" or Decimal(course["lectureHours"]) == 2
    assert Decimal(course["labHours"]) == 3
    assert [meeting["type"] for meeting in course["meetings"]] == [
        "lecture",
        "laboratory",
    ]
    assert [
        Decimal(body[key])
        for key in ("totalCredits", "totalLectureHours", "totalLabHours")
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
        assert Decimal(body["totalCredits"]) == 0


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
    assert Decimal(response.json()["totalCredits"]) == expected * 3
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


async def test_ac_103_7_own_identifier_works_foreign_and_absent_are_identical(
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
    assert (
        await client.get(f"/api/v1/students/{NUMBER}/schedule", headers=setup.headers)
    ).status_code == 200
    bodies = []
    for number in ("2021-00999-MN-1", "not-a-real-student"):
        response = await client.get(
            f"/api/v1/students/{number}/schedule", headers=setup.headers
        )
        assert response.status_code == 404
        body = response.json()
        assert body["code"] == "RESOURCE_NOT_FOUND"
        body.pop("request_id", None)
        bodies.append(body)
    assert bodies[0] == bodies[1]


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
async def test_partial_generation_failure_returns_only_503_and_rolls_back(
    client, setup, db, monkeypatch, format
):
    def fail(*args):
        raise RuntimeError("private-student-data-must-not-be-logged")

    monkeypatch.setattr(
        exports, "render_pdf" if format == "pdf" else "render_calendar", fail
    )
    response = await client.get(f"{URL}/export?format={format}", headers=setup.headers)
    assert response.status_code == 503
    assert response.json()["code"] == "SERVICE_UNAVAILABLE"
    assert response.headers["retry-after"] == "30"
    assert "content-disposition" not in response.headers
    assert "private-student" not in response.text
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
        "/students/{student_number}/schedule",
        "<unmatched>",
    }
