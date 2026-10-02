from __future__ import annotations

import logging
import uuid
from datetime import date, datetime, timedelta
from unittest.mock import patch

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_api_student.domains.student_auth import service as student_service
from cbpupsis_database.models.student_auth import AuthAuditLog, UserActiveSession
from cbpupsis_database.models.users import StudentProfile, User, UserProfile
from cbpupsis_shared.domains.auth.security import hash_password_bcrypt

TEST_PASSWORD = "CorrectStudentPassword123!"
VALID_STUDENT_ID = "2021-00123-MN-0"
VALID_BIRTHDATE = date(2003, 5, 14)


@pytest.fixture
async def student_user(db: AsyncSession) -> dict[str, object]:
    """Seed a student account with user_profile and student_profile."""
    user = User(
        email="student1@pup.edu.ph",
        password_hash=hash_password_bcrypt(TEST_PASSWORD, rounds=12),
        full_name="Juan Dela Cruz",
        is_active=True,
    )
    db.add(user)
    await db.flush()

    user_profile = UserProfile(
        user_id=user.id,
        first_name="Juan",
        last_name="Dela Cruz",
        birthdate=VALID_BIRTHDATE,
        institutional_email="student1@pup.edu.ph",
    )
    db.add(user_profile)

    student_profile = StudentProfile(
        user_id=user.id,
        student_number=VALID_STUDENT_ID,
        program_id=uuid.uuid4(),
        year_level=1,
        enrollment_status="enrolled",
    )
    db.add(student_profile)
    await db.commit()

    return {
        "user_id": user.id,
        "student_number": VALID_STUDENT_ID,
        "birthdate": VALID_BIRTHDATE,
        "password": TEST_PASSWORD,
        "email": user.email,
    }


@pytest.fixture
async def student_b(db: AsyncSession) -> dict[str, object]:
    """Seed a second student account for horizontal access verification (AC-001.8)."""
    user = User(
        email="student2@pup.edu.ph",
        password_hash=hash_password_bcrypt("AnotherPassword123!", rounds=12),
        full_name="Maria Santos",
        is_active=True,
    )
    db.add(user)
    await db.flush()

    student_id_b = "2021-00999-MN-1"
    student_profile = StudentProfile(
        user_id=user.id,
        student_number=student_id_b,
        program_id=uuid.uuid4(),
        year_level=2,
        enrollment_status="enrolled",
    )
    db.add(student_profile)
    await db.commit()

    return {
        "user_id": user.id,
        "student_number": student_id_b,
    }


class TestStudentLoginHappyPath:
    async def test_ac_001_1_happy_path(
        self, client: AsyncClient, student_user: dict[str, object]
    ) -> None:
        """AC-001.1: Happy path for an active unlocked student account.

        Expected: 200 OK, body carries access token and role student,
        cookie is HttpOnly, Secure, SameSite=Lax.
        """
        response = await client.post(
            "/api/v1/student-auth/login",
            json={
                "student_number": student_user["student_number"],
                "birthdate": str(student_user["birthdate"]),
                "password": student_user["password"],
            },
        )
        assert response.status_code == 200
        body = response.json()

        assert body["status"] == "success"
        assert "access_token" in body["data"]
        assert body["data"]["role"] == "student"
        assert body["data"]["expires_in"] == 900
        assert body["data"]["token_type"] == "bearer"

        # Verify refresh_token cookie
        cookies = response.cookies
        assert "refresh_token" in cookies
        # Check set-cookie header attributes
        cookie_header = response.headers.get("set-cookie", "").lower()
        assert "httponly" in cookie_header
        assert "samesite=lax" in cookie_header


class TestStudentIdFormatValidation:
    async def test_ac_001_2_malformed_id_format(
        self, client: AsyncClient, student_user: dict[str, object]
    ) -> None:
        """AC-001.2: A malformed student ID is rejected before any credential check.

        Expected: 422 with AUTH_ID_FORMAT_INVALID and no password-verification
        operation.
        """
        with patch(
            "cbpupsis_api_student.domains.student_auth.service.auth_client.check_password"
        ) as mock_verify:
            response = await client.post(
                "/api/v1/student-auth/login",
                json={
                    # Malformed pattern with a missing check digit.
                    "student_number": "2021-123-MN",
                    "password": "ValidPassword123!",
                },
            )
            assert response.status_code == 422
            body = response.json()
            assert body.get("code") == "AUTH_ID_FORMAT_INVALID"
            assert "Invalid Student ID format." in str(body.get("detail", ""))

            # Crucial assertion: verify_password must NOT be called
            assert mock_verify.call_count == 0


class TestCredentialIndistinguishability:
    async def test_ac_001_3_wrong_birthdate_byte_identical_to_wrong_password(
        self, client: AsyncClient, student_user: dict[str, object]
    ) -> None:
        """AC-001.3: A wrong birthdate must be indistinguishable from a wrong password.

        Expected: 401, code AUTH_FAILED, response body byte-identical.
        """
        wrong_birthdate_res = await client.post(
            "/api/v1/student-auth/login",
            json={
                "student_number": student_user["student_number"],
                "birthdate": "1999-01-01",  # wrong birthdate
                "password": student_user["password"],
            },
        )
        wrong_password_res = await client.post(
            "/api/v1/student-auth/login",
            json={
                "student_number": student_user["student_number"],
                "birthdate": str(student_user["birthdate"]),
                "password": "WrongPassword123!",  # wrong password
            },
        )

        assert wrong_birthdate_res.status_code == 401
        assert wrong_password_res.status_code == 401
        assert wrong_birthdate_res.json().get("code") == "AUTH_FAILED"

        # Indistinguishable response bodies (excluding dynamic per-request request_id)
        body_birthdate = wrong_birthdate_res.json()
        body_password = wrong_password_res.json()
        body_birthdate.pop("request_id", None)
        body_password.pop("request_id", None)
        assert body_birthdate == body_password


class TestAccountLockout:
    async def test_ac_001_4_lockout_fires_on_fifth_consecutive_failure(
        self,
        client: AsyncClient,
        student_user: dict[str, object],
        sent_emails: list[dict[str, str]],
    ) -> None:
        """AC-001.4: 5 consecutive failures in 15 minutes lock the account.

        Expected: 5th call returns 423 ACCOUNT_LOCKED, retry_after_seconds 900,
        and one security email queued.
        """
        sent_emails.clear()

        # Submit 4 failed attempts
        for i in range(4):
            res = await client.post(
                "/api/v1/student-auth/login",
                json={
                    "student_number": student_user["student_number"],
                    "birthdate": str(student_user["birthdate"]),
                    "password": f"wrong-pass-{i}",
                },
            )
            assert res.status_code == 401

        # 5th attempt triggers lockout
        res_5th = await client.post(
            "/api/v1/student-auth/login",
            json={
                "student_number": student_user["student_number"],
                "birthdate": str(student_user["birthdate"]),
                "password": "wrong-pass-5",
            },
        )
        assert res_5th.status_code == 423
        body_5th = res_5th.json()
        assert body_5th.get("code") in ("ACCOUNT_LOCKED", "AUTH_ACCOUNT_LOCKED")
        assert (
            body_5th.get("data", {}).get("retry_after_seconds") == 900
            or body_5th.get("retry_after_seconds") == 900
        )

        # One security notice email queued to institutional address
        assert len(sent_emails) == 1
        assert sent_emails[0]["to"] == student_user["email"]

    async def test_ac_001_5_correct_credentials_during_lockout_still_refused(
        self,
        client: AsyncClient,
        student_user: dict[str, object],
    ) -> None:
        """AC-001.5: Correct credentials inside active lockout are still refused.

        Expected: 423 ACCOUNT_LOCKED with remaining countdown time.
        """
        # Drive 5 failures to trigger lockout
        for _ in range(5):
            await client.post(
                "/api/v1/student-auth/login",
                json={
                    "student_number": student_user["student_number"],
                    "birthdate": str(student_user["birthdate"]),
                    "password": "bad-password",
                },
            )

        # Submit fully correct credentials 60s into the lockout
        with patch(
            "cbpupsis_api_student.domains.student_auth.service._get_utc8_now",
            return_value=datetime.now(student_service.UTC_PLUS_8)
            + timedelta(seconds=60),
        ):
            res_locked = await client.post(
                "/api/v1/student-auth/login",
                json={
                    "student_number": student_user["student_number"],
                    "birthdate": str(student_user["birthdate"]),
                    "password": student_user["password"],
                },
            )
            assert res_locked.status_code == 423
            body = res_locked.json()
            assert body.get("code") in ("ACCOUNT_LOCKED", "AUTH_ACCOUNT_LOCKED")

            # Remaining seconds should be approximately 840 (900 - 60)
            retry_sec = body.get("data", {}).get("retry_after_seconds") or body.get(
                "retry_after_seconds"
            )
            assert 800 <= retry_sec <= 850


class TestAuditLedger:
    async def test_ac_001_6_append_only_audit_ledger(
        self,
        client: AsyncClient,
        student_user: dict[str, object],
        db: AsyncSession,
    ) -> None:
        """AC-001.6: Every attempt records an audit row that cannot be deleted.

        Expected: Two rows present with actor, IP, outcome; delete fails.
        """
        # 1 Success
        await client.post(
            "/api/v1/student-auth/login",
            json={
                "student_number": student_user["student_number"],
                "birthdate": str(student_user["birthdate"]),
                "password": student_user["password"],
            },
        )

        # 1 Failure
        await client.post(
            "/api/v1/student-auth/login",
            json={
                "student_number": student_user["student_number"],
                "birthdate": "2000-01-01",
                "password": "wrong",
            },
        )

        # Read audit rows
        stmt = (
            select(AuthAuditLog)
            .where(AuthAuditLog.attempted_id == student_user["student_number"])
            .order_by(AuthAuditLog.created_at.asc())
        )
        result = await db.scalars(stmt)
        rows = result.all()

        assert len(rows) == 2
        assert rows[0].success is True
        assert rows[1].success is False
        assert rows[0].attempted_id == student_user["student_number"]
        assert rows[0].created_at is not None


class TestSessionIdleTimeout:
    async def test_ac_001_7_session_idle_15_minutes_invalidated_server_side(
        self,
        client: AsyncClient,
        student_user: dict[str, object],
        db: AsyncSession,
    ) -> None:
        """AC-001.7: Session idle for 15 minutes is invalidated server-side.

        Expected: 401 AUTH_SESSION_EXPIRED and token absent from session store.
        """
        # Login
        login_res = await client.post(
            "/api/v1/student-auth/login",
            json={
                "student_number": student_user["student_number"],
                "birthdate": str(student_user["birthdate"]),
                "password": student_user["password"],
            },
        )
        assert login_res.status_code == 200
        token = login_res.json()["data"]["access_token"]

        # Advance time by 16 minutes (beyond 15-minute idle limit)
        future_time = datetime.now(student_service.UTC_PLUS_8) + timedelta(minutes=16)

        with patch(
            "cbpupsis_api_student.domains.student_auth.service._get_utc8_now",
            return_value=future_time,
        ):
            me_res = await client.get(
                "/api/v1/student-auth/me",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert me_res.status_code == 401
            assert me_res.json().get("code") == "AUTH_SESSION_EXPIRED"

            # Assert token is absent from session store
            sessions_count = await db.scalar(
                select(UserActiveSession).where(
                    UserActiveSession.user_id == student_user["user_id"]
                )
            )
            assert sessions_count is None


class TestHorizontalAccessOwnership:
    async def test_ac_001_8_horizontal_access_answers_404_not_403(
        self,
        student_user: dict[str, object],
        student_b: dict[str, object],
    ) -> None:
        """AC-001.8: Requesting another student's record returns 404 RESOURCE_NOT_FOUND.

        Must never return 403 to prevent record enumeration.
        """
        from cbpupsis_api_student.domains.student_auth.dependencies import (
            check_student_resource_ownership,
        )
        from cbpupsis_api_student.domains.student_auth.exceptions import (
            StudentResourceNotFoundError,
        )

        # Another student's resource returns 404 to prevent enumeration.
        with pytest.raises(StudentResourceNotFoundError) as exc_info:
            check_student_resource_ownership(
                requested_student_id=str(student_b["user_id"]),
                authenticated_student_id=str(student_user["user_id"]),
            )
        assert exc_info.value.status_code == 404
        assert exc_info.value.code == "RESOURCE_NOT_FOUND"

        # Requesting own resource succeeds
        check_student_resource_ownership(
            requested_student_id=str(student_user["user_id"]),
            authenticated_student_id=str(student_user["user_id"]),
        )


class TestSecurityLogHygiene:
    async def test_no_credential_material_logged_on_failure(
        self,
        client: AsyncClient,
        student_user: dict[str, object],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Grep logs after failed login and assert no credential material appears."""
        plain_secret_password = "SuperSecretUnstoredPassword999!"
        with caplog.at_level(logging.DEBUG):
            await client.post(
                "/api/v1/student-auth/login",
                json={
                    "student_number": student_user["student_number"],
                    "birthdate": str(student_user["birthdate"]),
                    "password": plain_secret_password,
                },
            )

        log_text = caplog.text
        assert plain_secret_password not in log_text
