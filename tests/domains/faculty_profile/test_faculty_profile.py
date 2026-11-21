"""US-FAC-005 acceptance checks: faculty profile, settings, and password change."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_core.events import Event
from cbpupsis_database.models.audit import AuditEntry
from cbpupsis_database.models.notifications import Notification
from cbpupsis_database.models.users import FacultyProfile, User, UserProfile
from cbpupsis_shared.domains.audit.schemas import AuditEntryRead
from cbpupsis_shared.domains.auth.security import hash_password, totp_at
from cbpupsis_shared.domains.iam import service as iam_service
from cbpupsis_shared.domains.iam.constants import FACULTY_GROUP
from cbpupsis_shared.outbox import publish_transactional
from scripts import seed_iam

AUTH = "/api/v1/auth/faculty"
PROFILE_URL = "/api/v1/faculty/profile"
PASSWORD = "Faculty-Password-123!"
NEW_PASSWORD = "New-Faculty-Password-456!"


async def _create_faculty(
    db: AsyncSession,
    *,
    email: str = "faculty@example.com",
    employee_id: str = "2021-00456-MN-0",
    full_name: str = "Dr. Eleanor Vance",
    department: str = "Computer Science",
    academic_rank: str | None = "Associate Professor",
    office_location: str | None = "Building C, Room 402",
    office_hours: str | None = "Mon/Wed 10:00 - 12:00",
    qualifications: str | None = "Ph.D. in Computer Science",
    with_group: bool = True,
) -> dict[str, object]:
    await seed_iam.seed(db)
    user = User(
        email=email,
        password_hash=hash_password(PASSWORD),
        full_name=full_name,
        is_active=True,
        email_verified_at=datetime.now(UTC),
    )
    db.add(user)
    await db.flush()
    db.add(
        FacultyProfile(
            user_id=user.id,
            employee_id=employee_id,
            department=department,
            academic_rank=academic_rank,
            office_location=office_location,
            office_hours=office_hours,
            qualifications=qualifications,
        )
    )
    if with_group:
        group_id = await iam_service.get_group_id_by_name(db, FACULTY_GROUP)
        assert group_id is not None
        await iam_service.add_user_to_group(db, user.id, group_id)
    await db.commit()
    return {
        "id": user.id,
        "email": email,
        "employee_id": employee_id,
        "full_name": full_name,
    }


async def _enroll_and_login_faculty(
    client: AsyncClient, *, employee_id: str, password: str = PASSWORD
) -> tuple[dict[str, str], str]:
    login = await client.post(
        f"{AUTH}/login",
        json={"employee_id": employee_id, "password": password},
    )
    assert login.status_code == 200, login.text
    challenge = login.json()["challenge_token"]
    enroll = await client.post(
        f"{AUTH}/mfa/totp/enroll", json={"challenge_token": challenge}
    )
    assert enroll.status_code == 200, enroll.text
    secret = enroll.json()["secret"]
    counter = int(datetime.now(UTC).timestamp()) // 30
    confirmed = await client.post(
        f"{AUTH}/mfa/totp/confirm",
        json={"challenge_token": challenge, "code": totp_at(secret, counter)},
    )
    assert confirmed.status_code == 200, confirmed.text
    pair = confirmed.json()
    return {"Authorization": f"Bearer {pair['access_token']}"}, pair["recovery_codes"][
        0
    ]


async def _login_second_faculty_session(
    client: AsyncClient,
    *,
    employee_id: str,
    recovery_code: str,
    password: str = PASSWORD,
) -> dict[str, str]:
    login = await client.post(
        f"{AUTH}/login",
        json={"employee_id": employee_id, "password": password},
    )
    assert login.status_code == 200, login.text
    challenge = login.json()["challenge_token"]
    verified = await client.post(
        f"{AUTH}/mfa/verify",
        json={"challenge_token": challenge, "recovery_code": recovery_code},
    )
    assert verified.status_code == 200, verified.text
    return {"Authorization": f"Bearer {verified.json()['access_token']}"}


async def test_ac_fac_005_1_get_profile_returns_fields_and_editable_flags(
    client: AsyncClient, db: AsyncSession
) -> None:
    faculty = await _create_faculty(db)
    headers, _ = await _enroll_and_login_faculty(
        client, employee_id=str(faculty["employee_id"])
    )

    response = await client.get(PROFILE_URL, headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    fields = body["fields"]
    assert fields == {
        "fullName": {"value": "Dr. Eleanor Vance", "editable": False},
        "employeeId": {"value": "2021-00456-MN-0", "editable": False},
        "department": {"value": "Computer Science", "editable": False},
        "title": {"value": "Associate Professor", "editable": True},
        "officeLocation": {"value": "Building C, Room 402", "editable": True},
        "officeHours": {"value": "Mon/Wed 10:00 - 12:00", "editable": True},
        "qualifications": {"value": "Ph.D. in Computer Science", "editable": True},
    }


async def test_ac_fac_005_1_profile_requires_mfa_and_permission(
    client: AsyncClient, db: AsyncSession
) -> None:
    faculty = await _create_faculty(db, with_group=False)
    headers, _ = await _enroll_and_login_faculty(
        client, employee_id=str(faculty["employee_id"])
    )
    perm_refused = await client.get(PROFILE_URL, headers=headers)
    assert perm_refused.status_code == 403

    login = await client.post(
        f"{AUTH}/login",
        json={"employee_id": faculty["employee_id"], "password": PASSWORD},
    )
    assert login.status_code == 200
    challenge_headers = {"Authorization": f"Bearer {login.json()['challenge_token']}"}
    mfa_refused = await client.get(PROFILE_URL, headers=challenge_headers)
    assert mfa_refused.status_code == 401
    assert mfa_refused.json()["code"] == "AUTH_MFA_REQUIRED"


async def test_ac_fac_005_2_patch_editable_fields_and_reject_bad_values(
    client: AsyncClient, db: AsyncSession
) -> None:
    faculty = await _create_faculty(db)
    headers, _ = await _enroll_and_login_faculty(
        client, employee_id=str(faculty["employee_id"])
    )

    updated = await client.patch(
        PROFILE_URL,
        headers=headers,
        json={
            "title": "Professor",
            "officeLocation": "Main Building, Room 210",
            "officeHours": "Tue/Thu 13:00 - 15:00",
            "qualifications": "Ph.D. in Distributed Systems, M.S. CS",
        },
    )
    assert updated.status_code == 200, updated.text

    reread = await client.get(PROFILE_URL, headers=headers)
    assert reread.status_code == 200
    fields = reread.json()["fields"]
    assert fields["title"]["value"] == "Professor"
    assert fields["officeLocation"]["value"] == "Main Building, Room 210"
    assert fields["officeHours"]["value"] == "Tue/Thu 13:00 - 15:00"
    assert fields["qualifications"]["value"] == "Ph.D. in Distributed Systems, M.S. CS"

    bad = await client.patch(
        PROFILE_URL,
        headers=headers,
        json={"officeHours": "   "},
    )
    assert bad.status_code == 422
    assert bad.json()["code"] == "VALIDATION_ERROR"
    assert "officeHours" in bad.json()["fields"]


@pytest.mark.parametrize("locked_field", ["fullName", "employeeId", "department"])
async def test_ac_fac_005_3_patch_locked_field_returns_403_and_writes_nothing(
    client: AsyncClient, db: AsyncSession, locked_field: str
) -> None:
    faculty = await _create_faculty(db)
    headers, _ = await _enroll_and_login_faculty(
        client, employee_id=str(faculty["employee_id"])
    )

    response = await client.patch(
        PROFILE_URL,
        headers=headers,
        json={locked_field: "Tampered Value", "officeLocation": "Should Not Save"},
    )
    assert response.status_code == 403
    body = response.json()
    assert body["code"] == "PROFILE_FIELD_LOCKED"
    assert locked_field in body["lockedFields"]
    assert body["petitionRoute"]

    profile = await db.get(FacultyProfile, faculty["id"], populate_existing=True)
    assert profile is not None
    assert profile.office_location == "Building C, Room 402"
    assert profile.department == "Computer Science"


async def test_ac_fac_005_4_notification_preferences_and_opt_out_stops_delivery(
    client: AsyncClient,
    db: AsyncSession,
    sent_emails: list[dict[str, str]],
    drain_outbox,
) -> None:
    faculty = await _create_faculty(db)
    headers, _ = await _enroll_and_login_faculty(
        client, employee_id=str(faculty["employee_id"])
    )

    for channel in ("email", "in_app"):
        put_res = await client.put(
            "/api/v1/notifications/preferences",
            headers=headers,
            json={
                "notification_type": "faculty.roster_updated",
                "channel": channel,
                "enabled": False,
            },
        )
        assert put_res.status_code == 204, put_res.text

    get_res = await client.get("/api/v1/notifications/preferences", headers=headers)
    assert get_res.status_code == 200
    roster_prefs = [
        p for p in get_res.json() if p["notification_type"] == "faculty.roster_updated"
    ]
    assert len(roster_prefs) == 2
    assert all(p["enabled"] is False for p in roster_prefs)

    sent_emails.clear()
    publish_transactional(
        db,
        Event(
            name="faculty.roster_updated",
            payload={"user_id": str(faculty["id"])},
        ),
    )
    await db.commit()
    assert await drain_outbox() >= 1

    assert sent_emails == []
    in_app_rows = (
        await db.scalars(
            select(Notification).where(
                Notification.user_id == faculty["id"],
                Notification.notification_type == "faculty.roster_updated",
            )
        )
    ).all()
    assert in_app_rows == []


async def test_ac_fac_005_5_wrong_current_password_400_and_lockout(
    client: AsyncClient, db: AsyncSession
) -> None:
    faculty = await _create_faculty(db)
    headers, _ = await _enroll_and_login_faculty(
        client, employee_id=str(faculty["employee_id"])
    )

    for attempt in range(4):
        res = await client.post(
            "/api/v1/auth/change-password",
            headers=headers,
            json={
                "current_password": f"WrongPass-{attempt}!",
                "new_password": NEW_PASSWORD,
            },
        )
        assert res.status_code == 400
        assert res.json()["code"] == "AUTH_CURRENT_PASSWORD_INCORRECT"

    locked = await client.post(
        "/api/v1/auth/change-password",
        headers=headers,
        json={
            "current_password": "WrongPass-4!",
            "new_password": NEW_PASSWORD,
        },
    )
    assert locked.status_code == 423
    assert locked.json()["code"] == "AUTH_ACCOUNT_LOCKED"

    # Lockout also blocks subsequent password changes even with the right password.
    blocked_even_valid = await client.post(
        "/api/v1/auth/change-password",
        headers=headers,
        json={
            "current_password": PASSWORD,
            "new_password": NEW_PASSWORD,
        },
    )
    assert blocked_even_valid.status_code == 423
    assert blocked_even_valid.json()["code"] == "AUTH_ACCOUNT_LOCKED"


async def test_ac_fac_005_6_weak_new_password_returns_422_with_unmet_rules(
    client: AsyncClient, db: AsyncSession
) -> None:
    faculty = await _create_faculty(db)
    headers, _ = await _enroll_and_login_faculty(
        client, employee_id=str(faculty["employee_id"])
    )

    weak = await client.post(
        "/api/v1/auth/change-password",
        headers=headers,
        json={"current_password": PASSWORD, "new_password": "abc"},
    )
    assert weak.status_code == 422
    body = weak.json()
    assert body["code"] == "AUTH_PASSWORD_TOO_WEAK"
    assert set(body["unmet_rules"]) == {
        "at least 8 characters",
        "uppercase",
        "digit",
        "symbol",
    }


async def test_ac_fac_005_7_password_change_ends_other_session_and_logs_no_password(
    client: AsyncClient, db: AsyncSession, caplog
) -> None:
    faculty = await _create_faculty(db)
    session_one, recovery_code = await _enroll_and_login_faculty(
        client, employee_id=str(faculty["employee_id"])
    )
    session_two = await _login_second_faculty_session(
        client, employee_id=str(faculty["employee_id"]), recovery_code=recovery_code
    )

    assert (await client.get(PROFILE_URL, headers=session_two)).status_code == 200

    with caplog.at_level(logging.DEBUG):
        changed = await client.post(
            "/api/v1/auth/change-password",
            headers=session_one,
            json={"current_password": PASSWORD, "new_password": NEW_PASSWORD},
        )
    assert changed.status_code == 200, changed.text
    assert PASSWORD not in caplog.text
    assert NEW_PASSWORD not in caplog.text

    other_session_res = await client.get(PROFILE_URL, headers=session_two)
    assert other_session_res.status_code == 401
    assert other_session_res.json()["code"] == "AUTH_SESSION_EXPIRED"

    new_headers = {"Authorization": f"Bearer {changed.json()['access_token']}"}
    assert (await client.get(PROFILE_URL, headers=new_headers)).status_code == 200


async def test_ac_fac_005_8_reading_another_faculty_via_admin_users_returns_404(
    client: AsyncClient, db: AsyncSession
) -> None:
    faculty_a = await _create_faculty(
        db, email="faculty-a@example.com", employee_id="2021-00456-MN-0"
    )
    faculty_b = await _create_faculty(
        db, email="faculty-b@example.com", employee_id="2021-00457-MN-0"
    )
    headers_a, _ = await _enroll_and_login_faculty(
        client, employee_id=str(faculty_a["employee_id"])
    )

    response = await client.get(
        f"/api/v1/admin/users/{faculty_b['id']}", headers=headers_a
    )
    assert response.status_code == 404


async def test_ac_fac_005_9_profile_and_setting_changes_write_utc8_audit_rows(
    client: AsyncClient, db: AsyncSession
) -> None:
    faculty = await _create_faculty(db)
    db.add(UserProfile(user_id=faculty["id"], first_name="Eleanor", last_name="Vance"))
    await db.commit()
    headers, _ = await _enroll_and_login_faculty(
        client, employee_id=str(faculty["employee_id"])
    )

    patch_res = await client.patch(
        PROFILE_URL,
        headers=headers,
        json={"officeLocation": "North Wing, Room 101"},
    )
    assert patch_res.status_code == 200

    pref_res = await client.put(
        "/api/v1/notifications/preferences",
        headers=headers,
        json={
            "notification_type": "faculty.deadline_reminder",
            "channel": "email",
            "enabled": False,
        },
    )
    assert pref_res.status_code == 204

    profile_entry = await db.scalar(
        select(AuditEntry).where(
            AuditEntry.action == "faculty_profile.updated",
            AuditEntry.actor_id == faculty["id"],
        )
    )
    assert profile_entry is not None
    assert profile_entry.payload["field"] == "officeLocation"
    assert profile_entry.payload["actor"] == str(faculty["id"])
    assert profile_entry.payload["timestamp"].endswith("+08:00")
    assert "Building C, Room 402" not in json.dumps(profile_entry.payload)
    assert "North Wing, Room 101" not in json.dumps(profile_entry.payload)
    assert (
        AuditEntryRead.model_validate(profile_entry)
        .model_dump(mode="json")["occurred_at"]
        .endswith("+08:00")
    )

    pref_entry = await db.scalar(
        select(AuditEntry).where(
            AuditEntry.action == "notification.preference_updated",
            AuditEntry.actor_id == faculty["id"],
        )
    )
    assert pref_entry is not None
    assert pref_entry.payload["field"] == "faculty.deadline_reminder:email"
    assert pref_entry.payload["prior_value"] is True
    assert pref_entry.payload["new_value"] is False
    assert pref_entry.payload["actor"] == str(faculty["id"])
    assert pref_entry.payload["timestamp"].endswith("+08:00")
    assert (
        AuditEntryRead.model_validate(pref_entry)
        .model_dump(mode="json")["occurred_at"]
        .endswith("+08:00")
    )
