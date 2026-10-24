"""OAuth ownership, secret handling, durable reconciliation and withdrawal."""

import base64
import copy
import hashlib
import json
import uuid
from datetime import time, timedelta
from urllib.parse import parse_qs, urlsplit

import pytest
from pydantic import SecretStr
from sqlalchemy import delete, select, update

from cbpupsis_api_student.domains.google_calendar import repository, security, service
from cbpupsis_api_student.domains.google_calendar.exceptions import (
    CalendarUnavailable,
    LeaseLost,
    ProviderError,
)
from cbpupsis_api_student.domains.google_calendar.provider import SCOPE
from cbpupsis_api_student.domains.google_calendar.router import PERMISSION, get_provider
from cbpupsis_core.config import settings
from cbpupsis_database.models.enrollment import StudentCourseEnrollment
from cbpupsis_database.models.google_calendar import GoogleCalendarOAuthState
from cbpupsis_database.models.iam import PolicyPermission
from cbpupsis_database.models.sections import SectionSchedule
from cbpupsis_shared.domains.auth.dependencies import CurrentUser, get_current_user
from main import app
from tests.domains.schedule.test_schedule import password_hash as password_hash
from tests.domains.schedule.test_schedule import setup as setup

URL = "/api/v1/students/me/schedule/google-calendar"


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


async def test_oauth_state_pkce_encryption_and_single_use(client, setup, google, db):
    state, query = await begin(client, setup)
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
    "case", ["foreignStudent", "otherSession", "expired", "disconnect"]
)
async def test_state_binding_expiry_and_withdrawal(client, setup, google, db, case):
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
    else:
        assert (await client.delete(URL, headers=setup.headers)).status_code == 202
    response = await client.post(
        f"{URL}/callback",
        headers=setup.headers,
        json={"state": state, "code": "synthetic-code"},
    )
    assert response.status_code == 400
    assert google.exchanges == 0


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
async def test_live_student_guards_and_private_errors(
    client, setup, google, method, suffix, body
):
    response = await client.request(method, URL + suffix, json=body)
    assert response.status_code == 401
    assert response.headers["cache-control"] == "private, no-store"
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        id=setup.student_id, email="synthetic@example.com", role="faculty"
    )
    response = await client.request(method, URL + suffix, json=body)
    assert response.status_code == 403


async def test_permission_revocation_does_not_prevent_disconnect(
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


async def test_disabled_integration_does_not_block_schedule(
    client, setup, google, monkeypatch
):
    monkeypatch.setattr(settings, "google_calendar_enabled", False)
    assert (await client.get(URL, headers=setup.headers)).json()["enabled"] is False
    assert (
        await client.post(f"{URL}/authorization", json={}, headers=setup.headers)
    ).status_code == 503
    assert (
        await client.get("/api/v1/students/me/schedule", headers=setup.headers)
    ).status_code == 200


async def test_committed_updates_drop_readd_and_unchanged_reconciliation(
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
async def test_same_account_reconnect_reuses_calendar_and_reconciles_edits(
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


async def test_reconnect_to_different_account_never_reuses_old_calendar(
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
