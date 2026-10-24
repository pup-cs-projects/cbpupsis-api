"""US-101 acceptance checks: student sign-in with student number and birthdate."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, date, datetime, timedelta
from unittest.mock import patch

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_database.models.audit import AuthAuditLog
from cbpupsis_database.models.auth import AuthenticationLockout, UserActiveSession
from cbpupsis_database.models.users import StudentProfile, User, UserProfile
from cbpupsis_shared.domains.auth.security import decode_token, hash_password

LOGIN = "/api/v1/auth/student/login"
TEST_PASSWORD = "CorrectStudentPassword123!"
VALID_STUDENT_ID = "2021-00123-MN-0"
VALID_BIRTHDATE = date(2003, 5, 14)


async def _student(
    db: AsyncSession, *, email: str, number: str, password: str = TEST_PASSWORD
) -> User:
    user = User(email=email, password_hash=hash_password(password), is_active=True)
    db.add(user)
    await db.flush()
    db.add(
        UserProfile(
            user_id=user.id,
            first_name="Test",
            last_name="Student",
            birthdate=VALID_BIRTHDATE,
        )
    )
    db.add(
        StudentProfile(
            user_id=user.id,
            student_number=number,
            program_id=uuid.uuid4(),
            year_level=1,
            enrollment_status="enrolled",
        )
    )
    await db.commit()
    return user


@pytest.fixture
async def student_user(db: AsyncSession) -> dict[str, object]:
    user = await _student(db, email="student1@example.com", number=VALID_STUDENT_ID)
    return {
        "user_id": user.id,
        "student_number": VALID_STUDENT_ID,
        "birthdate": VALID_BIRTHDATE,
        "password": TEST_PASSWORD,
        "email": user.email,
    }


def _body(student: dict[str, object], **overrides: object) -> dict[str, object]:
    return {
        "student_number": student["student_number"],
        "birthdate": str(student["birthdate"]),
        "password": student["password"],
        **overrides,
    }


class TestStudentLoginHappyPath:
    async def test_ac_101_1_happy_path(
        self, client: AsyncClient, student_user: dict[str, object]
    ) -> None:
        """AC-101.1: an active, unlocked student gets a student session."""
        response = await client.post(LOGIN, json=_body(student_user))
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["role"] == "student"
        assert body["token_type"] == "bearer"
        assert body["expires_in"] == 900
        claims = decode_token(body["access_token"], "access")
        assert claims["role"] == "student" and claims["sid"]

        cookie_header = response.headers.get("set-cookie", "").lower()
        assert "refresh_token=" in cookie_header
        assert "httponly" in cookie_header
        assert "samesite=lax" in cookie_header
        assert "path=/api/v1/auth/student" in cookie_header


class TestStudentIdFormatValidation:
    async def test_ac_101_2_malformed_id_is_rejected_before_any_hash(
        self, client: AsyncClient, student_user: dict[str, object]
    ) -> None:
        """AC-101.2: a malformed student number never reaches a password check."""
        with patch(
            "cbpupsis_shared.domains.auth.service.passwords.verify_password"
        ) as mock_verify:
            response = await client.post(
                LOGIN,
                json={"student_number": "2021-123-MN", "password": "Whatever123!"},
            )
        assert response.status_code == 422
        assert response.json()["code"] == "AUTH_ID_FORMAT_INVALID"
        assert response.json()["detail"] == "Invalid Student ID format."
        assert mock_verify.call_count == 0

    @pytest.mark.parametrize(
        "invalid_id",
        [
            "2021-123-MN",
            "2025-00001-mn-0",
            "2025-00001-M1-0",
            "25-00001-MN-0",
            "2025-000001-MN-0",
            "2025-00001-MN-",
            "2025-00001-MN-00",
            "202500001MN0",
            "",
        ],
    )
    async def test_ac_101_2_invalid_formats(
        self, client: AsyncClient, invalid_id: str
    ) -> None:
        response = await client.post(
            LOGIN, json={"student_number": invalid_id, "password": "x"}
        )
        assert response.status_code == 422
        assert response.json()["code"] == "AUTH_ID_FORMAT_INVALID"


class TestCredentialIndistinguishability:
    async def test_ac_101_3_every_failure_is_byte_identical(
        self, client: AsyncClient, student_user: dict[str, object]
    ) -> None:
        """AC-101.3: wrong birthdate, wrong password, and an unknown student
        number produce one identical 401."""
        responses = [
            await client.post(LOGIN, json=_body(student_user, birthdate="1999-01-01")),
            await client.post(LOGIN, json=_body(student_user, password="Wrong123!")),
            await client.post(
                LOGIN, json=_body(student_user, student_number="2021-99999-MN-0")
            ),
        ]
        bodies = []
        for response in responses:
            assert response.status_code == 401
            assert response.json()["code"] == "AUTH_INVALID_CREDENTIALS"
            body = response.json()
            body.pop("request_id", None)
            bodies.append(body)
        assert bodies[0] == bodies[1] == bodies[2]

    async def test_ac_101_3_generic_email_login_is_refused_for_students(
        self, client: AsyncClient, student_user: dict[str, object]
    ) -> None:
        """Email and password alone cannot skip the birthdate check."""
        response = await client.post(
            "/api/v1/auth/login",
            json={"email": student_user["email"], "password": student_user["password"]},
        )
        assert response.status_code == 401


class TestAccountLockout:
    async def test_ac_101_4_fifth_failure_locks_and_notifies(
        self,
        client: AsyncClient,
        student_user: dict[str, object],
        sent_emails: list[dict[str, str]],
        drain_outbox,
    ) -> None:
        """AC-101.4: the 5th failure in 15 minutes answers 423 and one security
        notice is delivered to the account."""
        sent_emails.clear()
        for i in range(4):
            res = await client.post(
                LOGIN, json=_body(student_user, password=f"wrong-pass-{i}")
            )
            assert res.status_code == 401
        fifth = await client.post(
            LOGIN, json=_body(student_user, password="wrong-pass-5")
        )
        assert fifth.status_code == 423
        assert fifth.json()["code"] == "AUTH_ACCOUNT_LOCKED"
        assert fifth.json()["retry_after_seconds"] == 900
        assert fifth.headers["Retry-After"] == "900"

        await drain_outbox()
        locked_notices = [m for m in sent_emails if "locked" in m["subject"].lower()]
        assert len(locked_notices) == 1
        assert locked_notices[0]["to"] == student_user["email"]

    async def test_ac_101_4_unknown_student_number_locks_identically(
        self, client: AsyncClient
    ) -> None:
        """No enumeration through the lock: an unknown number locks on the 5th
        failure exactly like a real one."""
        body = {
            "student_number": "2021-55555-MN-0",
            "birthdate": "2000-01-01",
            "password": "x",
        }
        for _ in range(4):
            assert (await client.post(LOGIN, json=body)).status_code == 401
        assert (await client.post(LOGIN, json=body)).status_code == 423

    async def test_ac_101_5_correct_credentials_during_lockout_still_refused(
        self, client: AsyncClient, db: AsyncSession, student_user: dict[str, object]
    ) -> None:
        """AC-101.5: inside the lock, correct credentials answer 423 with the
        remaining time, and the refused attempts do not extend the lock."""
        for _ in range(5):
            await client.post(LOGIN, json=_body(student_user, password="bad"))
        lockout = await db.get(AuthenticationLockout, student_user["user_id"])
        lockout.locked_until = datetime.now(UTC) + timedelta(seconds=840)
        await db.commit()

        for _ in range(3):
            refused = await client.post(LOGIN, json=_body(student_user))
            assert refused.status_code == 423
            assert 800 <= refused.json()["retry_after_seconds"] <= 840
        await db.refresh(lockout)
        locked_until = lockout.locked_until
        if locked_until.tzinfo is None:  # SQLite drops the offset
            locked_until = locked_until.replace(tzinfo=UTC)
        assert locked_until <= datetime.now(UTC) + timedelta(seconds=840)


class TestAuditLedger:
    async def test_ac_101_6_every_attempt_is_on_the_ledger_without_secrets(
        self, client: AsyncClient, student_user: dict[str, object], db: AsyncSession
    ) -> None:
        """AC-101.6: every attempt lands on the append-only ledger, and the
        ledger has no write endpoint."""
        await client.post(LOGIN, json=_body(student_user))
        await client.post(LOGIN, json=_body(student_user, birthdate="2000-01-01"))

        rows = (
            await db.scalars(
                select(AuthAuditLog)
                .where(AuthAuditLog.attempted_id == student_user["student_number"])
                .order_by(AuthAuditLog.created_at.asc())
            )
        ).all()
        assert [row.success for row in rows] == [True, False]
        assert rows[1].failure_reason == "wrong_birthdate"
        for method in ("delete", "patch", "put"):
            response = await client.request(
                method.upper(), "/api/v1/audit/sign-in-attempts"
            )
            assert response.status_code == 405

    async def test_no_credential_material_logged_on_failure(
        self,
        client: AsyncClient,
        student_user: dict[str, object],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        secret = "SuperSecretUnstoredPassword999!"
        with caplog.at_level(logging.DEBUG):
            await client.post(LOGIN, json=_body(student_user, password=secret))
        assert secret not in caplog.text


class TestSessionIdleTimeout:
    async def test_ac_101_7_idle_session_is_invalidated_server_side(
        self, client: AsyncClient, student_user: dict[str, object], db: AsyncSession
    ) -> None:
        """AC-101.7: 15 idle minutes end the session on every endpoint."""
        token = (await client.post(LOGIN, json=_body(student_user))).json()[
            "access_token"
        ]
        headers = {"Authorization": f"Bearer {token}"}
        assert (
            await client.get("/api/v1/auth/student/me", headers=headers)
        ).status_code == 200

        session = await db.scalar(
            select(UserActiveSession).where(
                UserActiveSession.user_id == student_user["user_id"]
            )
        )
        session.last_activity_at = datetime.now(UTC) - timedelta(minutes=16)
        await db.commit()

        response = await client.get("/api/v1/users/me", headers=headers)
        assert response.status_code == 401
        assert response.json()["code"] == "AUTH_SESSION_EXPIRED"
        remaining = await db.scalar(
            select(UserActiveSession).where(
                UserActiveSession.user_id == student_user["user_id"]
            )
        )
        assert remaining is None

    async def test_ac_101_7_refresh_keeps_the_session_and_logout_ends_it(
        self, client: AsyncClient, student_user: dict[str, object]
    ) -> None:
        pair = (await client.post(LOGIN, json=_body(student_user))).json()
        refreshed = await client.post(
            "/api/v1/auth/student/refresh",
            json={"refresh_token": pair["refresh_token"]},
        )
        assert refreshed.status_code == 200, refreshed.text
        headers = {"Authorization": f"Bearer {refreshed.json()['access_token']}"}
        assert (
            await client.get("/api/v1/auth/student/me", headers=headers)
        ).status_code == 200
        assert (
            await client.post(
                "/api/v1/auth/refresh",
                json={"refresh_token": refreshed.json()["refresh_token"]},
            )
        ).status_code == 401

        assert (
            await client.post(
                "/api/v1/auth/student/logout",
                json={"refresh_token": refreshed.json()["refresh_token"]},
            )
        ).status_code == 204
        assert (
            await client.get("/api/v1/auth/student/me", headers=headers)
        ).status_code == 401

    async def test_refresh_restores_the_session_from_the_cookie_alone(
        self, client: AsyncClient, student_user: dict[str, object]
    ) -> None:
        # A reloaded web client holds no token; only the HttpOnly cookie survives.
        pair = (await client.post(LOGIN, json=_body(student_user))).json()
        restored = await client.post(
            "/api/v1/auth/student/refresh",
            headers={"Cookie": f"refresh_token={pair['refresh_token']}"},
        )
        assert restored.status_code == 200, restored.text
        assert "refresh_token=" in restored.headers["set-cookie"]
        headers = {"Authorization": f"Bearer {restored.json()['access_token']}"}
        assert (
            await client.get("/api/v1/auth/student/me", headers=headers)
        ).status_code == 200

    async def test_refresh_with_neither_body_nor_cookie_is_refused(
        self, client: AsyncClient
    ) -> None:
        response = await client.post("/api/v1/auth/student/refresh")
        assert response.status_code == 401
        assert response.json()["detail"] == "Not authenticated"


class TestUnknownClientAddress:
    async def test_sign_in_succeeds_and_records_a_null_address(
        self,
        addressless_client: AsyncClient,
        student_user: dict[str, object],
        db: AsyncSession,
    ) -> None:
        """No usable caller address never blocks sign-in; NULL is stored."""
        response = await addressless_client.post(LOGIN, json=_body(student_user))
        assert response.status_code == 200, response.text

        uid = student_user["user_id"]
        sessions = (
            await db.scalars(
                select(UserActiveSession.ip_address).where(
                    UserActiveSession.user_id == uid
                )
            )
        ).all()
        ledger = (
            await db.scalars(
                select(AuthAuditLog.ip_address).where(AuthAuditLog.user_id == uid)
            )
        ).all()
        assert sessions == [None]
        assert ledger == [None]
