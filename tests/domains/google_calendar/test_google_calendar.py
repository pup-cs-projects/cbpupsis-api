"""OAuth ownership, secret handling, durable reconciliation and withdrawal."""

import ast
import asyncio
import base64
import copy
import hashlib
import json
import uuid
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy import delete, select, update

from cbpupsis_api_student.domains.google_calendar import repository, security, service
from cbpupsis_api_student.domains.google_calendar import router as calendar_router
from cbpupsis_api_student.domains.google_calendar.exceptions import (
    CalendarUnavailable,
    LeaseLost,
    ProviderError,
)
from cbpupsis_api_student.domains.google_calendar.provider import SCOPE
from cbpupsis_api_student.domains.google_calendar.router import PERMISSION, get_provider
from cbpupsis_api_student.domains.schedule.exceptions import ScheduleUnavailableError
from cbpupsis_api_student.main import app as student_app
from cbpupsis_core.config import settings
from cbpupsis_database.models.academics import AcademicTerm
from cbpupsis_database.models.enrollment import StudentCourseEnrollment
from cbpupsis_database.models.google_calendar import GoogleCalendarOAuthState
from cbpupsis_database.models.iam import PolicyPermission
from cbpupsis_database.models.sections import SectionSchedule
from cbpupsis_database.session import get_db
from cbpupsis_shared.domains.auth.dependencies import CurrentUser, get_current_user
from main import app
from tests.domains.schedule.test_schedule import password_hash as password_hash
from tests.domains.schedule.test_schedule import setup as setup

URL = "/api/v1/student/schedule/google-calendar"
SCHEDULE_URL = "/api/v1/student/schedule"


class FakeGoogle:
    def __init__(self):
        self.calendars = {}
        self.deleted = set()
        self.exchanges = 0
        self.refreshes = 0
        self.revocations = 0
        self.fail = None
        self.fail_insert_once = False
        self.fail_delete_once = False
        self.subject = "opaque-google-subject"

    async def exchange(self, code, verifier, nonce):
        self.exchanges += 1
        assert code == "synthetic-code" and verifier and nonce
        return "synthetic-refresh-secret", self.subject

    async def refresh(self, refresh):
        self.refreshes += 1
        assert refresh == "synthetic-refresh-secret"
        if self.fail:
            raise ProviderError(self.fail)
        return "synthetic-access-secret", None

    async def revoke(self, refresh):
        self.revocations += 1

    async def create_calendar(self, token, summary):
        calendar_id = f"synthetic-{len(self.calendars)}@group.calendar.google.com"
        self.calendars[calendar_id] = {}
        if self.fail == "createUncertain":
            raise ProviderError(uncertain=True)
        return calendar_id

    async def event(self, token, calendar_id, event_id, *, method="GET", body=None):
        assert calendar_id != "primary"
        events = self.calendars[calendar_id]
        if method == "DELETE":
            events.pop(event_id, None)
            self.deleted.add((calendar_id, event_id))
            if self.fail_delete_once:
                self.fail_delete_once = False
                raise ProviderError(uncertain=True)
            return 204, {}
        if (calendar_id, event_id) in self.deleted:
            return 410, {}
        if event_id not in events:
            return 404, {}
        if method == "PUT":
            events[event_id] = copy.deepcopy(body)
        return 200, copy.deepcopy(events[event_id])

    async def insert_event(self, token, calendar_id, event_id, body):
        events = self.calendars[calendar_id]
        if event_id in events:
            return 409, {}
        events[event_id] = copy.deepcopy(body)
        if self.fail_insert_once:
            self.fail_insert_once = False
            raise ProviderError(uncertain=True)
        return 200, {"id": event_id}


@pytest.fixture(autouse=True)
def configuration(monkeypatch):
    monkeypatch.setattr(settings, "google_calendar_enabled", True)
    monkeypatch.setattr(settings, "google_calendar_sync_seconds", 300)
    monkeypatch.setattr(settings, "google_calendar_client_id", "synthetic-client")
    monkeypatch.setattr(
        settings, "google_calendar_client_secret", SecretStr("synthetic-client-secret")
    )
    monkeypatch.setattr(
        settings,
        "google_calendar_redirect_uri",
        "http://localhost:3000/calendar/google/callback",
    )
    monkeypatch.setattr(
        settings,
        "google_calendar_encryption_key",
        SecretStr(base64.urlsafe_b64encode(b"x" * 32).decode()),
    )


@pytest.fixture
def google(client):
    fake = FakeGoogle()
    app.dependency_overrides[get_provider] = lambda: fake
    return fake


async def begin(client, setup):
    result = await client.post(f"{URL}/authorization", json={}, headers=setup.headers)
    assert result.status_code == 200, result.text
    assert result.headers["cache-control"] == "private, no-store"
    assert set(result.json()) == {"authorization_url", "expires_in"}
    assert result.json()["expires_in"] == 600
    query = parse_qs(urlsplit(result.json()["authorization_url"]).query)
    return query["state"][0], query


async def connect(client, setup, google):
    state, _ = await begin(client, setup)
    result = await client.post(
        f"{URL}/callback",
        headers=setup.headers,
        json={"state": state, "code": "synthetic-code"},
    )
    assert result.status_code == 200, result.text
    assert set(result.json()) == {
        "enabled",
        "connected",
        "disconnecting",
        "last_sync_at",
        "next_sync_at",
        "last_error",
        "targets",
    }
    assert set(result.json()["targets"][0]) == {"year", "semester", "provisioned"}
    assert result.json()["connected"] is True
    assert "synthetic-refresh-secret" not in result.text
    return state


def test_google_openapi_exposes_snake_case_response_properties():
    schemas = app.openapi()["components"]["schemas"]
    assert set(schemas["AuthorizationRead"]["properties"]) == {
        "authorization_url",
        "expires_in",
    }
    assert set(schemas["ConnectionRead"]["properties"]) == {
        "enabled",
        "connected",
        "disconnecting",
        "last_sync_at",
        "next_sync_at",
        "last_error",
        "targets",
    }


async def due(db, setup):
    await repository.save_account(
        db, setup.student_id, {"next_sync_at": service.now() - timedelta(seconds=1)}
    )
    await db.commit()


async def add_terms(db, count):
    terms = []
    for year in range(2000, 2000 + count):
        start = datetime(year, 8, 1, tzinfo=UTC)
        term = AcademicTerm(
            academic_year=f"{year}-{year + 1}",
            semester="first",
            start_date=start.date(),
            end_date=date(year, 12, 18),
            regular_enrollment_start=start,
            regular_enrollment_end=start,
            add_drop_deadline=start,
            grade_submission_deadline=start,
        )
        db.add(term)
        terms.append({"year": term.academic_year, "semester": term.semester})
    await db.commit()
    return terms


@pytest.mark.parametrize("application", [student_app, app], ids=["student", "composed"])
def test_required_five_production_paths(application):
    paths = application.openapi()["paths"]
    expected = {
        ("get", URL),
        ("delete", URL),
        ("post", f"{URL}/authorization"),
        ("post", f"{URL}/callback"),
        ("post", f"{URL}/sync"),
    }
    actual = {
        (method, path)
        for path, operations in paths.items()
        if "schedule/google-calendar" in path
        for method in operations
    }
    assert actual == expected


@pytest.mark.parametrize("application", [student_app, app], ids=["student", "composed"])
async def test_ac_stu_005_8_canonical_guards_in_both_production_apps(
    application, client, setup, google, db
):
    previous = application.dependency_overrides.copy()
    application.dependency_overrides[get_db] = lambda: db
    application.dependency_overrides[get_provider] = lambda: google
    operations = (
        ("GET", "", None),
        ("POST", "/authorization", {}),
        ("POST", "/callback", {"state": "s" * 43, "code": "synthetic-code"}),
        ("POST", "/sync", {}),
        ("DELETE", "", None),
    )
    try:
        async with AsyncClient(
            transport=ASGITransport(app=application), base_url="http://test"
        ) as target:
            for method, suffix, body in operations:
                response = await target.request(method, URL + suffix, json=body)
                assert response.status_code == 401
                assert response.headers["cache-control"] == "private, no-store"
            await db.execute(delete(PolicyPermission))
            await db.commit()
            for method, suffix, body in operations:
                response = await target.request(
                    method, URL + suffix, json=body, headers=setup.headers
                )
                assert response.status_code == (202 if method == "DELETE" else 403)
                assert response.headers["cache-control"] == "private, no-store"
            assert google.exchanges == 0 and not google.calendars
    finally:
        application.dependency_overrides.clear()
        application.dependency_overrides.update(previous)


async def test_ac_stu_005_3_initial_weekly_series_have_term_bounds_and_holidays(
    client, setup, google, db
):
    await connect(client, setup, google)
    assert not google.calendars
    await service.run_due(db, google)
    assert len(google.calendars) == 1
    calendar = next(iter(google.calendars.values()))
    assert len(calendar) == len(setup.meeting_ids) == 4
    for event in calendar.values():
        start = datetime.fromisoformat(event["start"]["dateTime"])
        end = datetime.fromisoformat(event["end"]["dateTime"])
        assert date(2026, 8, 3) <= start.date() <= date(2026, 8, 5)
        assert start < end and start.date() == end.date()
        assert start.utcoffset() == timedelta(hours=8)
        assert event["start"]["timeZone"] == event["end"]["timeZone"] == "Asia/Manila"
        weekday = "MO" if start.weekday() == 0 else "WE"
        assert event["recurrence"][0] == (
            f"RRULE:FREQ=WEEKLY;BYDAY={weekday};UNTIL=20261218T155959Z"
        )
        holiday = "20260810" if weekday == "MO" else "20260812"
        excluded = start.astimezone(UTC).strftime("T%H%M%SZ")
        assert event["recurrence"][1:] == [f"EXDATE:{holiday}{excluded}"]


async def test_ac_stu_005_4_ten_terms_queue_and_status_tracks_completion(
    client, setup, google, db
):
    await connect(client, setup, google)
    await service.run_due(db, google)
    last_success = (await client.get(URL, headers=setup.headers)).json()["last_sync_at"]
    assert last_success is not None
    for selection in await add_terms(db, 9):
        response = await client.post(
            f"{URL}/sync", json=selection, headers=setup.headers
        )
        assert response.status_code == 202
        assert response.json()["last_sync_at"] == last_success
        assert response.json()["next_sync_at"] is not None
        assert response.json()["last_error"] is None
    status = (await client.get(URL, headers=setup.headers)).json()
    assert len(status["targets"]) == 10
    assert sum(t["provisioned"] for t in status["targets"]) == 1
    assert len(google.calendars) == 1  # HTTP queues; it does not run the worker.
    await service.run_due(db, google)
    status = (await client.get(URL, headers=setup.headers)).json()
    assert all(t["provisioned"] for t in status["targets"])
    assert status["last_error"] is None and len(google.calendars) == 10


async def test_ac_stu_005_9_private_status_before_during_and_after_consent(
    client, setup, google, db
):
    fields = {
        "enabled",
        "connected",
        "disconnecting",
        "last_sync_at",
        "next_sync_at",
        "last_error",
        "targets",
    }
    state, _ = await begin(client, setup)
    before = await client.get(URL, headers=setup.headers)
    assert set(before.json()) == fields
    assert not before.json()["connected"] and before.json()["targets"] == []
    assert before.json()["last_sync_at"] is before.json()["next_sync_at"] is None
    response = await client.post(
        f"{URL}/callback",
        headers=setup.headers,
        json={"state": state, "code": "synthetic-code"},
    )
    assert response.status_code == 200 and response.json()["connected"]
    await service.run_due(db, google)
    after = await client.get(URL, headers=setup.headers)
    assert set(after.json()) == fields
    assert after.json()["last_sync_at"] and after.json()["next_sync_at"]
    assert after.json()["last_error"] is None
    assert after.json()["targets"] == [
        {"year": "2026-2027", "semester": "first", "provisioned": True}
    ]
    assert after.headers["cache-control"] == "private, no-store"
    for private in (
        "synthetic-refresh-secret",
        "synthetic-access-secret",
        google.subject,
        next(iter(google.calendars)),
        str(setup.student_id),
        state,
    ):
        assert private not in before.text + response.text + after.text


@pytest.mark.parametrize("case", ["connected", "revoking", "running", "termLimit"])
async def test_ac_stu_005_11_conflicts_include_status_hint(
    client, setup, google, db, case
):
    await connect(client, setup, google)
    suffix = "authorization"
    selection = {}
    if case == "revoking":
        await client.delete(URL, headers=setup.headers)
    elif case == "running":
        assert await repository.claim_due(db, service.now())
        suffix = "sync"
    elif case == "termLimit":
        terms = await add_terms(db, 10)
        for term in terms[:9]:
            assert (
                await client.post(f"{URL}/sync", json=term, headers=setup.headers)
            ).status_code == 202
        selection, suffix = terms[9], "sync"
    response = await client.post(
        f"{URL}/{suffix}", json=selection, headers=setup.headers
    )
    assert response.status_code == 409
    assert response.json()["code"] == "GOOGLE_CALENDAR_CONFLICT"
    assert "status" in response.json()["detail"].lower()
    row = await repository.account(db, setup.student_id)
    assert len(row.targets) == (10 if case == "termLimit" else 1)


async def test_ac_stu_005_12_unchanged_worker_skips_google_and_retries_failure(
    client, setup, google, db, monkeypatch
):
    await connect(client, setup, google)
    await service.run_due(db, google)
    assert settings.google_calendar_sync_seconds == 300
    row = await repository.account(db, setup.student_id)
    assert (
        timedelta(seconds=299)
        <= row.next_sync_at - row.last_sync_at
        <= timedelta(seconds=301)
    )

    async def no_google(*args, **kwargs):
        pytest.fail("Unchanged background reconciliation must not call Google")

    with monkeypatch.context() as patch:
        for method in (
            "exchange",
            "refresh",
            "revoke",
            "create_calendar",
            "event",
            "insert_event",
        ):
            patch.setattr(google, method, no_google)
        await due(db, setup)
        assert await service.run_due(db, google)
    await client.post(f"{URL}/sync", json={}, headers=setup.headers)
    google.fail = "providerUnavailable"
    failed_at = service.now().replace(tzinfo=None)
    await service.run_due(db, google)
    status = (await client.get(URL, headers=setup.headers)).json()
    assert status["last_error"] == "providerUnavailable"
    row = await repository.account(db, setup.student_id)
    assert row.attempts == 1 and row.next_sync_at.replace(tzinfo=None) > failed_at
    assert not await service.run_due(db, google)  # Observe the retry delay.
    google.fail = None
    await due(db, setup)
    await service.run_due(db, google)
    row = await repository.account(db, setup.student_id)
    assert row.last_error is None and row.attempts == 0
    assert (
        len(google.calendars) == 1 and len(next(iter(google.calendars.values()))) == 4
    )


async def test_ac_stu_005_14_repeated_disconnect_keeps_calendar_and_blocks_sync(
    client, setup, google, db
):
    await connect(client, setup, google)
    await service.run_due(db, google)
    original = copy.deepcopy(google.calendars)
    for _ in range(2):
        response = await client.delete(URL, headers=setup.headers)
        assert response.status_code == 202 and not response.json()["connected"]
    await service.run_due(db, google)
    response = await client.delete(URL, headers=setup.headers)
    assert response.status_code == 202 and not response.json()["disconnecting"]
    assert not await service.run_due(db, google)
    assert google.revocations == 1 and google.calendars == original
    response = await client.post(f"{URL}/sync", json={}, headers=setup.headers)
    assert response.status_code == 409
    assert response.json()["code"] == "GOOGLE_CALENDAR_CONFLICT"


@pytest.mark.parametrize("failure", ["refused", "identity", "timeout"])
async def test_ac_stu_005_15_callback_failures_attach_nothing_and_consume_state(
    client, setup, google, db, monkeypatch, failure
):
    state, _ = await begin(client, setup)
    original_timeout = asyncio.timeout
    deadlines = []

    async def exchange(*args):
        if failure == "timeout":
            # Expire only once state is consumed and DB work has finished;
            # cancelling aiosqlite IO would destroy the in-memory connection.
            deadlines[0].reschedule(asyncio.get_running_loop().time())
            await asyncio.sleep(60)
        raise ProviderError(
            "authorizationFailed" if failure == "identity" else "providerUnavailable"
        )

    def capture_deadline(seconds):
        assert seconds == 25
        deadline = original_timeout(seconds)
        deadlines.append(deadline)
        return deadline

    monkeypatch.setattr(google, "exchange", exchange)
    if failure == "timeout":
        monkeypatch.setattr(calendar_router.asyncio, "timeout", capture_deadline)
    body = {"state": state, "code": "synthetic-code"}
    response = await client.post(f"{URL}/callback", json=body, headers=setup.headers)
    assert response.status_code == 503
    assert response.json()["code"] == "GOOGLE_CALENDAR_UNAVAILABLE"
    assert response.headers["retry-after"] == "30"
    row = await repository.account(db, setup.student_id)
    assert row.refresh_cipher is None and row.subject_hash is None and row.targets == {}
    assert not google.calendars
    retry = await client.post(f"{URL}/callback", json=body, headers=setup.headers)
    assert retry.status_code == 400
    assert retry.json()["code"] == "GOOGLE_CALENDAR_INVALID_STATE"


async def test_ac_stu_005_16_sync_repairs_google_edits_without_enrollment_changes(
    client, setup, google, db
):
    await connect(client, setup, google)
    await service.run_due(db, google)
    calendar = next(iter(google.calendars.values()))
    original = copy.deepcopy(calendar)
    enrollments = (
        await db.execute(
            select(
                StudentCourseEnrollment.id,
                StudentCourseEnrollment.status,
                StudentCourseEnrollment.section_id,
            )
        )
    ).all()
    edited = calendar[next(iter(calendar))]
    edited.update(summary="GOOGLE EDIT", location="GOOGLE ROOM", recurrence=[])
    response = await client.post(f"{URL}/sync", json={}, headers=setup.headers)
    assert response.status_code == 202
    await service.run_due(db, google)
    assert calendar == original and len(google.calendars) == 1
    assert (
        await db.execute(
            select(
                StudentCourseEnrollment.id,
                StudentCourseEnrollment.status,
                StudentCourseEnrollment.section_id,
            )
        )
    ).all() == enrollments


@pytest.mark.parametrize("stage", ["source", "provider", "reconcile"])
async def test_unexpected_sync_runtime_error_propagates_without_source_unavailable(
    client, setup, google, db, monkeypatch, stage
):
    await connect(client, setup, google)
    before = await repository.account(db, setup.student_id)
    previous = (before.last_error, before.attempts, before.last_sync_at)

    async def unexpected(*args, **kwargs):
        raise RuntimeError("synthetic unexpected failure")

    target, name = {
        "source": (service.schedule_service, "calendar_snapshot"),
        "provider": (google, "refresh"),
        "reconcile": (service, "reconcile"),
    }[stage]
    monkeypatch.setattr(target, name, unexpected)
    with pytest.raises(RuntimeError, match="synthetic unexpected failure"):
        await service.run_due(db, google)
    await db.rollback()
    after = await repository.account(db, setup.student_id)
    assert after.last_error != "sourceUnavailable"
    assert (after.last_error, after.attempts, after.last_sync_at) == previous


@pytest.mark.parametrize(
    ("exception", "expected"),
    [(ScheduleUnavailableError, "sourceUnavailable"), (TimeoutError, "syncTimedOut")],
)
async def test_expected_source_and_timeout_failures_keep_retry_status(
    client, setup, google, db, monkeypatch, exception, expected
):
    await connect(client, setup, google)

    async def unavailable(*args, **kwargs):
        raise exception

    monkeypatch.setattr(service.schedule_service, "calendar_snapshot", unavailable)
    assert await service.run_due(db, google)
    row = await repository.account(db, setup.student_id)
    assert row.last_error == expected and row.attempts == 1
    assert row.next_sync_at is not None and row.lease_id is None


def test_issue157_guide_error_registry_and_no_catch_all():
    root = Path(__file__).resolve().parents[3]
    guide = (root / "docs/tech-book/GOOGLE-CALENDAR.md").read_text(encoding="utf-8")
    assert len(guide.splitlines()) <= 150
    assert "user approved" not in guide.lower().replace("-", " ")
    for heading in (
        "## What it does",
        "## Google Cloud setup",
        "## Environment variables",
        "## Running locally",
        "## Troubleshooting",
    ):
        assert heading in guide
    for variable in ("GOOGLE_CALENDAR_ENABLED", "GOOGLE_CALENDAR_ENCRYPTION_KEY"):
        assert variable in guide
    errors = (root / "docs/tech-book/ERROR-CODES.md").read_text(encoding="utf-8")
    for status, code in (
        (400, "GOOGLE_CALENDAR_INVALID_STATE"),
        (409, "GOOGLE_CALENDAR_CONFLICT"),
        (503, "GOOGLE_CALENDAR_UNAVAILABLE"),
    ):
        assert f"| {status} | `{code}` |" in errors
    tree = ast.parse(Path(service.__file__).read_text(encoding="utf-8"))
    for handler in (
        node for node in ast.walk(tree) if isinstance(node, ast.ExceptHandler)
    ):
        assert handler.type is not None
        assert not any(
            isinstance(node, ast.Name) and node.id in {"Exception", "BaseException"}
            for node in ast.walk(handler.type)
        )


async def test_ac_stu_005_1_consent_scope_pkce_encryption_and_connection(
    client, setup, google, db
):
    state, query = await begin(client, setup)
    assert SCOPE == "https://www.googleapis.com/auth/calendar.app.created"
    assert set(query["scope"][0].split()) == {"openid", SCOPE}
    assert query["access_type"] == ["offline"]
    assert query["code_challenge_method"] == ["S256"]
    stored = await db.scalar(select(GoogleCalendarOAuthState))
    assert stored.state_hash != state and state not in stored.payload_cipher
    payload = json.loads(
        security.crypt(stored.payload_cipher, setup.student_id, "state", decrypt=True)
    )
    assert query["code_challenge"] == [
        base64.urlsafe_b64encode(hashlib.sha256(payload["verifier"].encode()).digest())
        .decode()
        .rstrip("=")
    ]
    body = {"state": state, "code": "synthetic-code"}
    assert (
        await client.post(f"{URL}/callback", json=body, headers=setup.headers)
    ).status_code == 200
    row = await repository.account(db, setup.student_id)
    assert row.refresh_cipher != "synthetic-refresh-secret"
    assert (
        await client.post(f"{URL}/callback", json=body, headers=setup.headers)
    ).status_code == 400
    assert google.exchanges == 1


@pytest.mark.parametrize(
    "case",
    ["foreignStudent", "otherSession", "expired", "disconnect", "used", "superseded"],
)
async def test_ac_stu_005_2_refuse_foreign_expired_used_or_superseded_state(
    client, setup, google, db, case
):
    state, _ = await begin(client, setup)
    if case in {"foreignStudent", "otherSession"}:
        principal = CurrentUser(
            id=setup.student_id if case == "otherSession" else setup.faculty_id,
            email="synthetic@example.com",
            role="student",
            session_id=uuid.uuid4(),
        )
        if case == "foreignStudent":
            app.dependency_overrides[PERMISSION[0].dependency] = lambda: None
        app.dependency_overrides[get_current_user] = lambda: principal
    elif case == "expired":
        await db.execute(
            update(GoogleCalendarOAuthState).values(
                expires_at=service.now() - timedelta(seconds=1)
            )
        )
        await db.commit()
    elif case == "used":
        response = await client.post(
            f"{URL}/callback",
            headers=setup.headers,
            json={"state": state, "code": "synthetic-code"},
        )
        assert response.status_code == 200
        await client.delete(URL, headers=setup.headers)
        await service.run_due(db, google)
    elif case == "superseded":
        await begin(client, setup)
    else:
        assert (await client.delete(URL, headers=setup.headers)).status_code == 202
    exchanges = google.exchanges
    response = await client.post(
        f"{URL}/callback",
        headers=setup.headers,
        json={"state": state, "code": "synthetic-code"},
    )
    assert response.status_code == 400
    assert response.json()["code"] == "GOOGLE_CALENDAR_INVALID_STATE"
    assert google.exchanges == exchanges
    assert (await repository.account(db, setup.student_id)).refresh_cipher is None


@pytest.mark.parametrize(
    ("method", "suffix", "body"),
    [
        ("GET", "", None),
        ("POST", "/authorization", {}),
        ("POST", "/callback", {"state": "s" * 43, "code": "code"}),
        ("POST", "/sync", {}),
        ("DELETE", "", None),
    ],
)
async def test_ac_stu_005_8_session_permission_guards_and_disconnect_exception(
    client, setup, google, db, method, suffix, body
):
    response = await client.request(method, URL + suffix, json=body)
    assert response.status_code == 401
    assert response.headers["cache-control"] == "private, no-store"
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        id=setup.student_id, email="synthetic@example.com", role="faculty"
    )
    response = await client.request(method, URL + suffix, json=body)
    assert response.status_code == 403
    app.dependency_overrides.pop(get_current_user)
    await db.execute(delete(PolicyPermission))
    await db.commit()
    response = await client.request(
        method, URL + suffix, json=body, headers=setup.headers
    )
    assert response.status_code == (202 if method == "DELETE" else 403)
    assert google.exchanges == 0 and not google.calendars


async def test_ac_stu_005_6_permission_withdrawal_still_revokes_google_access(
    client, setup, google, db
):
    await connect(client, setup, google)
    await db.execute(delete(PolicyPermission))
    await db.commit()
    assert (
        await client.post(f"{URL}/sync", json={}, headers=setup.headers)
    ).status_code == 403
    response = await client.delete(URL, headers=setup.headers)
    assert response.status_code == 202 and response.json()["connected"] is False
    assert response.json()["disconnecting"] is True
    assert await service.run_due(db, google)
    assert google.revocations == 1
    assert (await repository.account(db, setup.student_id)).revoke_cipher is None


async def test_callback_validation_never_echoes_credentials(
    client, setup, google, monkeypatch
):
    monkeypatch.setattr(settings, "debug", True)
    response = await client.post(
        f"{URL}/callback",
        headers=setup.headers,
        json={"state": "PRIVATE-STATE", "code": "PRIVATE-CODE" * 300},
    )
    assert response.status_code == 422
    assert "PRIVATE" not in response.text and '"input"' not in response.text
    assert response.headers["cache-control"] == "private, no-store"


async def test_malformed_callback_json_is_private_and_redacted(client, setup, google):
    response = await client.post(
        f"{URL}/callback",
        headers={**setup.headers, "Content-Type": "application/json"},
        content=b'{"code":"PRIVATE-CODE",',
    )
    assert response.status_code == 422
    assert response.headers["cache-control"] == "private, no-store"
    assert "PRIVATE-CODE" not in response.text


async def test_ac_stu_005_10_disabled_integration_preserves_schedule_and_exports(
    client, setup, google, monkeypatch
):
    monkeypatch.setattr(settings, "google_calendar_enabled", False)
    assert (await client.get(URL, headers=setup.headers)).json()["enabled"] is False
    assert (
        await client.post(f"{URL}/authorization", json={}, headers=setup.headers)
    ).status_code == 503
    assert (
        await client.get("/api/v1/student/schedule", headers=setup.headers)
    ).status_code == 200
    for format in ("pdf", "ics"):
        response = await client.get(
            f"{SCHEDULE_URL}/export", params={"format": format}, headers=setup.headers
        )
        assert response.status_code == 200
        assert response.content
    assert google.exchanges == 0 and not google.calendars


async def test_ac_stu_005_5_committed_updates_drop_readd_without_duplicates(
    client, setup, google, db
):
    await connect(client, setup, google)
    assert await service.run_due(db, google)
    calendar = next(iter(google.calendars.values()))
    assert len(calendar) == 4
    ids = set(calendar)
    for body in calendar.values():
        assert body["start"]["timeZone"] == "Asia/Manila"
        assert "UNTIL=20261218T155959Z" in body["recurrence"][0]
        assert any(line.startswith("EXDATE:") for line in body["recurrence"])
    await due(db, setup)
    assert await service.run_due(db, google)
    assert google.refreshes == 1 and set(calendar) == ids
    await db.execute(
        update(SectionSchedule)
        .where(SectionSchedule.id == setup.meeting_ids[0])
        .values(room="UPDATED", start_time=time(9), end_time=time(11))
    )
    await db.commit()
    await due(db, setup)
    await service.run_due(db, google)
    assert set(calendar) == ids
    assert any(
        body["location"] == "UPDATED" and "T09:00:00" in body["start"]["dateTime"]
        for body in calendar.values()
    )
    await db.execute(
        update(StudentCourseEnrollment)
        .where(StudentCourseEnrollment.id == setup.registration_ids[0])
        .values(status="dropped")
    )
    await db.commit()
    await due(db, setup)
    await service.run_due(db, google)
    assert len(calendar) == 2
    retained = set(calendar)
    await db.execute(
        update(StudentCourseEnrollment)
        .where(StudentCourseEnrollment.id == setup.registration_ids[0])
        .values(status="enrolled")
    )
    await db.commit()
    await due(db, setup)
    await service.run_due(db, google)
    assert len(calendar) == 4 and retained <= set(calendar)
    assert ids - retained != set(calendar) - retained


async def test_lost_insert_ack_retries_without_duplicates(client, setup, google, db):
    await connect(client, setup, google)
    google.fail_insert_once = True
    await service.run_due(db, google)
    assert (
        await repository.account(db, setup.student_id)
    ).last_error == "providerUnavailable"
    await due(db, setup)
    await service.run_due(db, google)
    assert (
        len(google.calendars) == 1 and len(next(iter(google.calendars.values()))) == 4
    )


async def test_uncertain_calendar_creation_is_not_blindly_retried(
    client, setup, google, db
):
    await connect(client, setup, google)
    original_refresh = google.refresh

    async def refresh(token):
        google.fail = None
        result = await original_refresh(token)
        google.fail = "createUncertain"
        return result

    google.refresh = refresh
    await service.run_due(db, google)
    row = await repository.account(db, setup.student_id)
    assert row.last_error == "calendarCreationUncertain" and row.next_sync_at is None
    await due(db, setup)
    await service.run_due(db, google)
    assert len(google.calendars) == 1


async def test_lost_delete_ack_then_readd_restores_missing_series(
    client, setup, google, db
):
    await connect(client, setup, google)
    await service.run_due(db, google)
    calendar = next(iter(google.calendars.values()))
    await db.execute(
        update(StudentCourseEnrollment)
        .where(StudentCourseEnrollment.id == setup.registration_ids[0])
        .values(status="dropped")
    )
    await db.commit()
    google.fail_delete_once = True
    await due(db, setup)
    await service.run_due(db, google)
    assert len(calendar) == 3
    await db.execute(
        update(StudentCourseEnrollment)
        .where(StudentCourseEnrollment.id == setup.registration_ids[0])
        .values(status="enrolled")
    )
    await db.commit()
    await due(db, setup)
    await service.run_due(db, google)
    assert len(calendar) == 4
    assert (await repository.account(db, setup.student_id)).last_error is None


@pytest.mark.parametrize("error", ["quotaLimited", "reconnectRequired"])
async def test_safe_failure_status_and_backoff(client, setup, google, db, error):
    await connect(client, setup, google)
    google.fail = error
    await service.run_due(db, google)
    row = await repository.account(db, setup.student_id)
    assert row.last_error == error and row.attempts == 1
    assert (row.next_sync_at is None) == (error == "reconnectRequired")


async def test_worker_lease_is_exclusive_and_stale_writes_are_rejected(
    client, setup, google, db
):
    await connect(client, setup, google)
    claim = await repository.claim_due(db, service.now())
    assert claim and await repository.claim_due(db, service.now()) is None
    await repository.save_account(db, setup.student_id, {"lease_id": uuid.uuid4()})
    await db.commit()
    with pytest.raises(LeaseLost):
        await service.checkpoint(db, *claim, last_error="stale")


@pytest.mark.parametrize("withdrawal", ["disconnect", "permission"])
async def test_ac_stu_005_13_same_account_reuses_calendar_and_repairs_edits(
    client, setup, google, db, monkeypatch, withdrawal
):
    await connect(client, setup, google)
    await service.run_due(db, google)
    row = await repository.account(db, setup.student_id)
    targets = copy.deepcopy(row.targets)
    calendar = next(iter(google.calendars.values()))
    original = copy.deepcopy(calendar)
    calendar[next(iter(calendar))]["location"] = "EDITED-WHILE-DISCONNECTED"
    if withdrawal == "disconnect":
        response = await client.delete(URL, headers=setup.headers)
        assert response.status_code == 202
        assert response.json()["targets"] == []
    else:

        async def no_permissions(*args):
            return []

        with monkeypatch.context() as patch:
            patch.setattr(service.iam, "get_effective_permissions", no_permissions)
            await due(db, setup)
            await service.run_due(db, google)
    row = await repository.account(db, setup.student_id)
    assert row.targets == targets and row.refresh_cipher is None
    assert row.revoke_cipher
    assert (await client.get(URL, headers=setup.headers)).json()["targets"] == []
    assert (
        await client.post(f"{URL}/authorization", json={}, headers=setup.headers)
    ).status_code == 409
    assert (
        await client.post(f"{URL}/sync", json={}, headers=setup.headers)
    ).status_code == 409
    await service.run_due(db, google)
    row = await repository.account(db, setup.student_id)
    assert row.targets == targets and row.revoke_cipher is None
    assert row.next_sync_at is None
    assert not await service.run_due(db, google)
    await connect(client, setup, google)
    row = await repository.account(db, setup.student_id)
    assert all(target["force"] for target in row.targets.values())
    await service.run_due(db, google)
    assert len(google.calendars) == 1 and calendar == original
    assert google.revocations == 1
    assert (await repository.account(db, setup.student_id)).last_error is None


async def test_ac_stu_005_7_different_account_gets_a_separate_calendar(
    client, setup, google, db
):
    await connect(client, setup, google)
    await service.run_due(db, google)
    old_calendar_id = next(iter(google.calendars))
    old_calendar = copy.deepcopy(google.calendars[old_calendar_id])
    await client.delete(URL, headers=setup.headers)
    await service.run_due(db, google)
    google.subject = "different-verified-subject"
    await connect(client, setup, google)
    row = await repository.account(db, setup.student_id)
    assert row.last_sync_at is None
    assert all(target["calendarId"] is None for target in row.targets.values())
    await service.run_due(db, google)
    assert len(google.calendars) == 2
    assert google.calendars[old_calendar_id] == old_calendar
    row = await repository.account(db, setup.student_id)
    assert all(
        target["calendarId"] != old_calendar_id for target in row.targets.values()
    )


@pytest.mark.parametrize(
    "error", ["providerUnavailable", "permissionDenied", "reconnectRequired"]
)
async def test_revocation_failure_retains_only_encrypted_pending_credential(
    client, setup, google, db, monkeypatch, error
):
    await connect(client, setup, google)
    await service.run_due(db, google)
    before = copy.deepcopy(google.calendars)
    await client.delete(URL, headers=setup.headers)

    async def unavailable(refresh):
        raise ProviderError(error)

    with monkeypatch.context() as patch:
        patch.setattr(google, "revoke", unavailable)
        await service.run_due(db, google)
    row = await repository.account(db, setup.student_id)
    assert row.refresh_cipher is None and row.revoke_cipher
    assert "synthetic-refresh-secret" not in row.revoke_cipher
    assert row.next_sync_at and row.last_error == error
    assert google.calendars == before
    assert (
        await client.post(f"{URL}/authorization", json={}, headers=setup.headers)
    ).status_code == 409
    await due(db, setup)
    await service.run_due(db, google)
    row = await repository.account(db, setup.student_id)
    assert row.revoke_cipher is None and row.next_sync_at is None
    assert google.calendars == before


@pytest.mark.parametrize("during", ["GET", "INSERT"])
async def test_disconnect_during_provider_request_fences_next_write(
    client, setup, google, db, during
):
    await connect(client, setup, google)
    original_event, original_insert = google.event, google.insert_event
    writes = []
    withdrew = False

    async def event(token, calendar_id, event_id, *, method="GET", body=None):
        nonlocal withdrew
        result = await original_event(
            token, calendar_id, event_id, method=method, body=body
        )
        if during == "GET" and not withdrew:
            withdrew = True
            await service.disconnect(db, setup.student_id)
        if method != "GET":
            writes.append(method)
        return result

    async def insert(token, calendar_id, event_id, body):
        nonlocal withdrew
        result = await original_insert(token, calendar_id, event_id, body)
        writes.append("INSERT")
        if during == "INSERT" and not withdrew:
            withdrew = True
            await service.disconnect(db, setup.student_id)
        return result

    google.event, google.insert_event = event, insert
    await service.run_due(db, google)
    assert withdrew
    assert writes == ([] if during == "GET" else ["INSERT"])
    row = await repository.account(db, setup.student_id)
    assert row.refresh_cipher is None and row.revoke_cipher
    assert row.last_error is None


async def test_reconnect_keeps_uncertain_calendar_creation_fail_closed(
    client, setup, google, db
):
    await connect(client, setup, google)
    row = await repository.account(db, setup.student_id)
    targets = copy.deepcopy(row.targets)
    next(iter(targets.values()))["provisioning"] = True
    await repository.save_account(db, setup.student_id, {"targets": targets})
    await db.commit()
    await client.delete(URL, headers=setup.headers)
    await service.run_due(db, google)
    await connect(client, setup, google)
    await service.run_due(db, google)
    row = await repository.account(db, setup.student_id)
    assert row.last_error == "calendarCreationUncertain"
    assert row.next_sync_at is None and not google.calendars


def test_tokens_are_bound_to_student_and_purpose():
    student = uuid.uuid4()
    encrypted = security.crypt("private", student, "refresh")
    assert encrypted != security.crypt("private", student, "refresh")
    assert security.crypt(encrypted, student, "refresh", decrypt=True) == "private"
    for user, purpose in ((uuid.uuid4(), "refresh"), (student, "state")):
        with pytest.raises(CalendarUnavailable):
            security.crypt(encrypted, user, purpose, decrypt=True)
