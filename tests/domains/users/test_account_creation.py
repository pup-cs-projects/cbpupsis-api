"""Superadmin account creation, temporary passwords, and the forced change.

One test (or more) per product-owner decision:

1. Only a Superadmin creates accounts; public registration is gone.
2. One at a time (``POST /users``) or in bulk (see ``tests/domains/user_import``).
3. Created accounts are verified on creation.
4. The system mails a temporary password; the user signs in with it.
5. Until it is changed, every route but change-password (and logout) answers
   403 ``PASSWORD_CHANGE_REQUIRED``; after ``TEMPORARY_PASSWORD_TTL_DAYS`` it
   stops working, and a Superadmin must reissue.
6. ``finance`` is an Admin position with institution-wide scope.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_core.config import settings
from cbpupsis_database.models.audit import AuditEntry
from cbpupsis_database.models.iam import Group, UserGroup
from cbpupsis_database.models.outbox import OutboxMessage
from cbpupsis_database.models.users import (
    AdminProfile,
    FacultyProfile,
    StudentProfile,
    User,
    UserProfile,
)
from cbpupsis_shared.domains.auth.security import totp_at
from tests.conftest import temporary_password_from

USERS = "/api/v1/users"
STUDENT_NUMBER = "2024-00001-MN-0"
EMPLOYEE_ID = "2015-00042-MN-1"

ROWS: dict[str, dict[str, object]] = {
    "student": {
        "first_name": "Ana",
        "surname": "Reyes",
        "email": "ana.reyes@example.com",
        "role": "student",
        "institutional_id": STUDENT_NUMBER,
        "birthdate": "2005-02-14",
        "program": "bscs",
        "year_level": 1,
    },
    "faculty": {
        "first_name": "Ben",
        "surname": "Cruz",
        "email": "ben.cruz@example.com",
        "role": "faculty",
        "institutional_id": EMPLOYEE_ID,
        "department": "Computer Science",
        "college": "CCIS",
    },
    "admin_registrar": {
        "first_name": "Cora",
        "surname": "Diaz",
        "email": "cora.diaz@example.com",
        "role": "admin_registrar",
    },
    "admin_finance": {
        "first_name": "Dan",
        "surname": "Lim",
        "email": "dan.lim@example.com",
        "role": "admin_finance",
        # Institution-wide: a stray department or college is not kept.
        "department": "Accounting",
        "college": "CBA",
    },
    "admin_chairperson": {
        "first_name": "Eva",
        "surname": "Tan",
        "email": "eva.tan@example.com",
        "role": "admin_chairperson",
        "department": "Computer Science",
    },
    "admin_dean": {
        "first_name": "Fe",
        "surname": "Go",
        "email": "fe.go@example.com",
        "role": "admin_dean",
        "college": "CCIS",
        "department": "Ignored",
    },
}

EXPECTED_GROUP = {
    "student": "Students",
    "faculty": "Faculty",
    "admin_registrar": "Admins",
    "admin_finance": "Admins",
    "admin_chairperson": "Admins",
    "admin_dean": "Admins",
}


@pytest.fixture
async def creating(role_groups, active_program, superadmin_headers):
    """A Superadmin session with the role groups and one program in place."""
    return superadmin_headers


async def _create(client: AsyncClient, headers, role: str, **changes) -> dict:
    response = await client.post(USERS, headers=headers, json={**ROWS[role], **changes})
    assert response.status_code == 201, response.text
    return response.json()


async def _mailed_password(drain_outbox, sent_emails, email: str) -> str:
    await drain_outbox()
    [message] = [m for m in sent_emails if m["to"] == email]
    return temporary_password_from(message)


async def _student_login(client: AsyncClient, password: str) -> dict:
    response = await client.post(
        "/api/v1/auth/student/login",
        json={
            "student_number": STUDENT_NUMBER,
            "birthdate": ROWS["student"]["birthdate"],
            "password": password,
        },
    )
    return response


async def _expire(db: AsyncSession, email: str) -> None:
    user = await db.scalar(select(User).where(User.email == email))
    user.temporary_password_expires_at = datetime.now(UTC) - timedelta(minutes=1)
    await db.commit()


class TestOnlyASuperadminCreatesAccounts:
    """Decision 1."""

    async def test_unauthenticated_is_401_and_another_session_is_403(
        self, client: AsyncClient, auth_headers: dict[str, str], role_groups
    ) -> None:
        anonymous = await client.post(USERS, json=ROWS["admin_registrar"])
        assert anonymous.status_code == 401
        ordinary = await client.post(
            USERS, headers=auth_headers, json=ROWS["admin_registrar"]
        )
        assert ordinary.status_code == 403
        assert ordinary.json()["code"] == "SUPERADMIN_ROLE_REQUIRED"

    async def test_reissue_is_superadmin_only(
        self, client: AsyncClient, auth_headers: dict[str, str], registered_user
    ) -> None:
        path = f"{USERS}/{registered_user['id']}/reissue-temporary-password"
        assert (await client.post(path)).status_code == 401
        assert (await client.post(path, headers=auth_headers)).status_code == 403


class TestEachRoleRow:
    """Decisions 2, 3, and 6: the right profile, group, and scope per role."""

    @pytest.mark.parametrize("role", list(ROWS))
    async def test_creates_a_verified_account_with_its_profile_and_group(
        self, client: AsyncClient, db: AsyncSession, creating, role: str
    ) -> None:
        body = await _create(client, creating, role)
        assert body["email"] == ROWS[role]["email"]
        assert body["email_verified_at"] is not None
        assert "password" not in json.dumps(body)

        user_id = uuid.UUID(body["id"])
        user = await db.get(User, user_id)
        assert user.password_change_required is True
        expected_expiry = datetime.now(UTC) + timedelta(
            days=settings.temporary_password_ttl_days
        )
        expires_at = user.temporary_password_expires_at.replace(tzinfo=UTC)
        assert abs((expires_at - expected_expiry).total_seconds()) < 60

        profile = await db.get(UserProfile, user_id)
        assert (profile.first_name, profile.last_name) == (
            ROWS[role]["first_name"],
            ROWS[role]["surname"],
        )
        groups = (
            await db.scalars(
                select(Group.name)
                .join(UserGroup, UserGroup.group_id == Group.id)
                .where(UserGroup.user_id == user_id)
            )
        ).all()
        assert groups == [EXPECTED_GROUP[role]]

    async def test_student_profile(
        self, client: AsyncClient, db: AsyncSession, creating, active_program
    ) -> None:
        body = await _create(client, creating, "student")
        profile = await db.get(StudentProfile, uuid.UUID(body["id"]))
        assert profile.student_number == STUDENT_NUMBER
        assert profile.program_id == active_program.id
        assert profile.year_level == 1
        user_profile = await db.get(UserProfile, uuid.UUID(body["id"]))
        assert user_profile.birthdate.isoformat() == ROWS["student"]["birthdate"]

    async def test_faculty_profile(
        self, client: AsyncClient, db: AsyncSession, creating
    ) -> None:
        body = await _create(client, creating, "faculty")
        profile = await db.get(FacultyProfile, uuid.UUID(body["id"]))
        assert (profile.employee_id, profile.department, profile.college) == (
            EMPLOYEE_ID,
            "Computer Science",
            "CCIS",
        )

    @pytest.mark.parametrize(
        ("role", "position", "department", "college"),
        [
            ("admin_registrar", "registrar", None, None),
            ("admin_finance", "finance", None, None),
            ("admin_chairperson", "chairperson", "Computer Science", None),
            ("admin_dean", "dean", None, "CCIS"),
        ],
    )
    async def test_admin_position_and_scope(
        self,
        client: AsyncClient,
        db: AsyncSession,
        creating,
        role: str,
        position: str,
        department: str | None,
        college: str | None,
    ) -> None:
        body = await _create(client, creating, role)
        profile = await db.get(AdminProfile, uuid.UUID(body["id"]))
        assert (profile.position, profile.department, profile.college) == (
            position,
            department,
            college,
        )


class TestValidation:
    async def test_missing_role_fields_are_422(
        self, client: AsyncClient, db: AsyncSession, creating
    ) -> None:
        row = {k: v for k, v in ROWS["student"].items() if k != "program"}
        response = await client.post(USERS, headers=creating, json=row)
        assert response.status_code == 422
        assert "required for this role: program" in response.text
        assert await db.scalar(select(User).where(User.email == row["email"])) is None

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("institutional_id", "2024-1-MN-0"),
            ("birthdate", "14/02/2005"),
            ("year_level", 9),
            ("role", "superadmin"),
        ],
    )
    async def test_invalid_fields_are_422(
        self, client: AsyncClient, creating, field: str, value: object
    ) -> None:
        response = await client.post(
            USERS, headers=creating, json={**ROWS["student"], field: value}
        )
        assert response.status_code == 422

    async def test_unknown_program_is_422(self, client: AsyncClient, creating) -> None:
        response = await client.post(
            USERS, headers=creating, json={**ROWS["student"], "program": "BSXX"}
        )
        assert response.status_code == 422
        assert response.json()["code"] == "ACCOUNT_PROGRAM_UNKNOWN"

    @pytest.mark.parametrize(
        ("change", "field"),
        [
            ({"institutional_id": "2024-00002-MN-0"}, "email"),
            ({"email": "other@example.com"}, "institutional_id"),
        ],
    )
    async def test_duplicates_are_409(
        self, client: AsyncClient, creating, change: dict, field: str
    ) -> None:
        await _create(client, creating, "student")
        response = await client.post(
            USERS, headers=creating, json={**ROWS["student"], **change}
        )
        assert response.status_code == 409
        assert response.json()["code"] == "USER_ALREADY_EXISTS"
        assert response.json()["field"] == field

    async def test_email_conflict_ignores_case(
        self, client: AsyncClient, creating
    ) -> None:
        await _create(client, creating, "admin_registrar")
        response = await client.post(
            USERS,
            headers=creating,
            json={**ROWS["admin_registrar"], "email": "Cora.Diaz@Example.com"},
        )
        assert response.status_code == 409


class TestTemporaryPassword:
    """Decisions 4 and 5."""

    async def test_signs_in_with_the_mailed_password_and_must_change_it(
        self,
        client: AsyncClient,
        db: AsyncSession,
        creating,
        drain_outbox,
        sent_emails,
    ) -> None:
        body = await _create(client, creating, "student")
        password = await _mailed_password(drain_outbox, sent_emails, body["email"])
        login = await _student_login(client, password)
        assert login.status_code == 200, login.text
        pair = login.json()
        headers = {"Authorization": f"Bearer {pair['access_token']}"}

        for path in (
            "/api/v1/auth/student/me",
            "/api/v1/users/me",
            "/api/v1/auth/whoami",
        ):
            blocked = await client.get(path, headers=headers)
            assert blocked.status_code == 403, path
            assert blocked.json()["code"] == "PASSWORD_CHANGE_REQUIRED"

        kept = await client.post(
            "/api/v1/auth/change-password",
            headers=headers,
            json={"current_password": password, "new_password": password},
        )
        assert kept.status_code == 422

        changed = await client.post(
            "/api/v1/auth/change-password",
            headers=headers,
            json={"current_password": password, "new_password": "My-own-password-42!"},
        )
        assert changed.status_code == 200, changed.text
        new_headers = {"Authorization": f"Bearer {changed.json()['access_token']}"}
        assert (
            await client.get("/api/v1/auth/student/me", headers=new_headers)
        ).status_code == 200

        user = await db.get(User, uuid.UUID(body["id"]), populate_existing=True)
        assert user.password_change_required is False
        assert user.temporary_password_expires_at is None

    async def test_logout_stays_open_during_the_forced_change(
        self, client: AsyncClient, creating, drain_outbox, sent_emails
    ) -> None:
        body = await _create(client, creating, "student")
        password = await _mailed_password(drain_outbox, sent_emails, body["email"])
        pair = (await _student_login(client, password)).json()
        response = await client.post(
            "/api/v1/auth/student/logout",
            json={"refresh_token": pair["refresh_token"]},
        )
        assert response.status_code == 204

    async def test_faculty_and_admin_password_steps_accept_the_mailed_password(
        self, client: AsyncClient, creating, drain_outbox, sent_emails
    ) -> None:
        faculty = await _create(client, creating, "faculty")
        admin = await _create(client, creating, "admin_finance")
        await drain_outbox()
        by_address = {m["to"]: temporary_password_from(m) for m in sent_emails}

        faculty_login = await client.post(
            "/api/v1/auth/faculty/login",
            json={"employee_id": EMPLOYEE_ID, "password": by_address[faculty["email"]]},
        )
        assert faculty_login.status_code == 200, faculty_login.text
        assert "challenge_token" in faculty_login.json()
        admin_login = await client.post(
            "/api/v1/auth/admin/login",
            json={"email": admin["email"], "password": by_address[admin["email"]]},
        )
        assert admin_login.status_code == 200, admin_login.text
        assert "challenge_token" in admin_login.json()

    async def test_the_admin_guard_blocks_until_the_change(
        self, client: AsyncClient, creating, drain_outbox, sent_emails
    ) -> None:
        """The Admin guard checks sessions itself; it must honor the rule too."""
        admin = await _create(client, creating, "admin_finance")
        password = await _mailed_password(drain_outbox, sent_emails, admin["email"])
        challenge = (
            await client.post(
                "/api/v1/auth/admin/login",
                json={"email": admin["email"], "password": password},
            )
        ).json()["challenge_token"]
        enrolled = await client.post(
            "/api/v1/auth/admin/mfa/totp/enroll", json={"challenge_token": challenge}
        )
        secret = enrolled.json()["secret"]
        pair = (
            await client.post(
                "/api/v1/auth/admin/mfa/totp/confirm",
                json={
                    "challenge_token": challenge,
                    "code": totp_at(secret, int(datetime.now(UTC).timestamp()) // 30),
                },
            )
        ).json()
        headers = {"Authorization": f"Bearer {pair['access_token']}"}
        blocked = await client.get("/api/v1/admin/me", headers=headers)
        assert blocked.status_code == 403
        assert blocked.json()["code"] == "PASSWORD_CHANGE_REQUIRED"

        changed = await client.post(
            "/api/v1/auth/change-password",
            headers=headers,
            json={"current_password": password, "new_password": "My-own-password-42!"},
        )
        assert changed.status_code == 200, changed.text
        me = await client.get(
            "/api/v1/admin/me",
            headers={"Authorization": f"Bearer {changed.json()['access_token']}"},
        )
        assert me.status_code == 200, me.text
        assert me.json()["position"] == "finance"

    async def test_an_expired_temporary_password_is_refused(
        self,
        client: AsyncClient,
        db: AsyncSession,
        creating,
        drain_outbox,
        sent_emails,
    ) -> None:
        body = await _create(client, creating, "student")
        password = await _mailed_password(drain_outbox, sent_emails, body["email"])
        await _expire(db, body["email"])

        expired = await _student_login(client, password)
        assert expired.status_code == 401
        assert expired.json()["code"] == "AUTH_TEMPORARY_PASSWORD_EXPIRED"
        # A wrong password still gets the one identical failure: the expiry is
        # only told to a caller who holds the password.
        wrong = await _student_login(client, "Not-the-password-1!")
        assert wrong.status_code == 401
        assert wrong.json()["code"] == "AUTH_FAILED"

    async def test_an_expired_temporary_password_cannot_be_changed(
        self,
        client: AsyncClient,
        db: AsyncSession,
        creating,
        drain_outbox,
        sent_emails,
    ) -> None:
        body = await _create(client, creating, "student")
        password = await _mailed_password(drain_outbox, sent_emails, body["email"])
        pair = (await _student_login(client, password)).json()
        await _expire(db, body["email"])
        response = await client.post(
            "/api/v1/auth/change-password",
            headers={"Authorization": f"Bearer {pair['access_token']}"},
            json={"current_password": password, "new_password": "My-own-password-42!"},
        )
        assert response.status_code == 401
        assert response.json()["code"] == "AUTH_TEMPORARY_PASSWORD_EXPIRED"


class TestReissue:
    async def test_reissue_mails_a_new_password_and_revokes_sessions(
        self,
        client: AsyncClient,
        db: AsyncSession,
        creating,
        drain_outbox,
        sent_emails,
    ) -> None:
        body = await _create(client, creating, "student")
        first = await _mailed_password(drain_outbox, sent_emails, body["email"])
        pair = (await _student_login(client, first)).json()
        await _expire(db, body["email"])

        response = await client.post(
            f"{USERS}/{body['id']}/reissue-temporary-password", headers=creating
        )
        assert response.status_code == 204, response.text
        await drain_outbox()
        second = temporary_password_from(sent_emails[-1])
        assert second != first

        old_session = await client.post(
            "/api/v1/auth/change-password",
            headers={"Authorization": f"Bearer {pair['access_token']}"},
            json={"current_password": first, "new_password": "My-own-password-42!"},
        )
        assert old_session.status_code == 401
        assert (await _student_login(client, first)).status_code == 401
        renewed = await _student_login(client, second)
        assert renewed.status_code == 200, renewed.text

        user = await db.get(User, uuid.UUID(body["id"]), populate_existing=True)
        expires_at = user.temporary_password_expires_at.replace(tzinfo=UTC)
        assert user.password_change_required and expires_at > datetime.now(UTC)

    async def test_a_reissue_silences_the_unsent_older_email(
        self, client: AsyncClient, creating, drain_outbox, sent_emails
    ) -> None:
        body = await _create(client, creating, "student")
        await client.post(
            f"{USERS}/{body['id']}/reissue-temporary-password", headers=creating
        )
        await drain_outbox()
        mailed = [m for m in sent_emails if m["to"] == body["email"]]
        assert len(mailed) == 1
        assert (
            await _student_login(client, temporary_password_from(mailed[0]))
        ).status_code == 200

    async def test_reissue_for_an_unknown_user_is_404(
        self, client: AsyncClient, creating
    ) -> None:
        response = await client.post(
            f"{USERS}/{uuid.uuid4()}/reissue-temporary-password", headers=creating
        )
        assert response.status_code == 404


class TestThePasswordStaysSecret:
    async def test_raw_password_is_in_no_log_audit_row_or_outbox_payload(
        self,
        client: AsyncClient,
        db: AsyncSession,
        creating,
        drain_outbox,
        sent_emails,
        caplog,
    ) -> None:
        with caplog.at_level(logging.DEBUG):
            body = await _create(client, creating, "faculty")
            await client.post(
                f"{USERS}/{body['id']}/reissue-temporary-password", headers=creating
            )
            # Captured before delivery: the payload must already be opaque.
            payloads = [
                json.dumps(message.payload)
                for message in (await db.scalars(select(OutboxMessage))).all()
            ]
            await drain_outbox()
        [password] = [temporary_password_from(m) for m in sent_emails]

        assert password not in caplog.text
        assert all(password not in payload for payload in payloads)
        delivered = (await db.scalars(select(OutboxMessage))).all()
        assert all(password not in (message.last_error or "") for message in delivered)
        entries = (await db.scalars(select(AuditEntry))).all()
        assert entries
        for entry in entries:
            stored = json.dumps([entry.payload, entry.prior_state, entry.new_state])
            assert password not in stored
            assert body["email"] not in stored
        user = await db.get(User, uuid.UUID(body["id"]))
        assert (
            user.password_hash.startswith("$2b$") and password not in user.password_hash
        )
