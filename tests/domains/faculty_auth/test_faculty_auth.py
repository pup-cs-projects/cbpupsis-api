"""US-102 acceptance checks: faculty sign-in, MFA, and session.

Several tests here are regressions for defects the auth audit found: a password
alone reaching student data, MFA re-enrollment with only a password, writes that
never committed, an unauthenticated ledger read, and failure responses that
differed for known and unknown ids.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_database.models.audit import AuthAuditLog
from cbpupsis_database.models.auth import (
    AuthenticationFailure,
    MfaChallenge,
    RefreshToken,
    UserActiveSession,
    UserMfaCredential,
    UserMfaRecoveryCode,
)
from cbpupsis_database.models.users import FacultyProfile, User
from cbpupsis_shared.domains.auth.security import decode_token, hash_password, totp_at

AUTH = "/api/v1/auth/faculty"
EMPLOYEE_ID = "2021-00456-MN-0"
PASSWORD = "Faculty-Password-123!"


@pytest.fixture
async def faculty(db: AsyncSession) -> dict[str, object]:
    user = User(
        email="faculty@example.com",
        password_hash=hash_password(PASSWORD),
        is_active=True,
        email_verified_at=datetime.now(UTC),
    )
    db.add(user)
    await db.flush()
    db.add(FacultyProfile(user_id=user.id, employee_id=EMPLOYEE_ID, department="CS"))
    await db.commit()
    return {"id": user.id, "email": user.email}


def _now_counter() -> int:
    return int(datetime.now(UTC).timestamp()) // 30


async def _login(client: AsyncClient, password: str = PASSWORD, **extra) -> dict:
    response = await client.post(
        f"{AUTH}/login",
        json={"employee_id": EMPLOYEE_ID, "password": password, **extra},
    )
    return response


async def _enrolled_session(client: AsyncClient) -> tuple[dict, str, int]:
    """Sign in for the first time: password, enroll, confirm."""
    challenge = (await _login(client)).json()["challenge_token"]
    enrolled = await client.post(
        f"{AUTH}/mfa/totp/enroll", json={"challenge_token": challenge}
    )
    assert enrolled.status_code == 200, enrolled.text
    secret = enrolled.json()["secret"]
    counter = _now_counter()
    confirmed = await client.post(
        f"{AUTH}/mfa/totp/confirm",
        json={"challenge_token": challenge, "code": totp_at(secret, counter)},
    )
    assert confirmed.status_code == 200, confirmed.text
    return confirmed.json(), secret, counter


class TestPasswordStep:
    async def test_ac_102_1_password_yields_only_a_challenge(
        self, client: AsyncClient, faculty
    ) -> None:
        """AC-102.1: a password alone never yields a faculty session."""
        response = await _login(client)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["mfa_required"] is True
        assert body["enrollment_required"] is True
        assert "access_token" not in body and "refresh_token" not in body

    async def test_ac_102_1_challenge_token_opens_no_protected_route(
        self, client: AsyncClient, faculty, grant
    ) -> None:
        """Regression: the challenge token used to read sections and rosters."""
        await grant(faculty["id"], "ReadCourseSection")
        challenge = (await _login(client)).json()["challenge_token"]
        headers = {"Authorization": f"Bearer {challenge}"}
        for path in (
            f"{AUTH}/me",
            "/api/v1/sections",
            f"/api/v1/sections/{uuid.uuid4()}/roster",
            "/api/v1/users/me",
        ):
            assert (await client.get(path, headers=headers)).status_code == 403

    async def test_ac_102_1_email_password_cannot_skip_mfa(
        self, client: AsyncClient, faculty
    ) -> None:
        """Regression: an email fallback and the generic login both skipped MFA."""
        generic = await client.post(
            "/api/v1/auth/login",
            json={"email": faculty["email"], "password": PASSWORD},
        )
        assert generic.status_code == 401
        fallback = await client.post(
            f"{AUTH}/login", json={"email": faculty["email"], "password": PASSWORD}
        )
        assert fallback.status_code == 422

    async def test_ac_102_1_malformed_employee_id_is_422(
        self, client: AsyncClient
    ) -> None:
        response = await client.post(
            f"{AUTH}/login", json={"employee_id": "2021-45-MN", "password": "x"}
        )
        assert response.status_code == 422
        assert response.json()["code"] == "AUTH_ID_FORMAT_INVALID"


class TestEnumerationParity:
    async def test_ac_102_5_unknown_and_known_ids_fail_identically(
        self, client: AsyncClient, faculty
    ) -> None:
        """Regression: the 5th failure was 423 for an unknown id but 401 with
        "0 attempts remaining" for a known one."""
        unknown = {"employee_id": "2021-99999-MN-0", "password": "wrong"}
        known = {"employee_id": EMPLOYEE_ID, "password": "wrong"}
        for _ in range(4):
            a = await client.post(f"{AUTH}/login", json=unknown)
            b = await client.post(f"{AUTH}/login", json=known)
            assert a.status_code == b.status_code == 401
            body_a, body_b = a.json(), b.json()
            body_a.pop("request_id")
            body_b.pop("request_id")
            assert body_a == body_b
            assert body_a["code"] == "AUTH_INVALID_CREDENTIALS"
        fifth_unknown = await client.post(f"{AUTH}/login", json=unknown)
        fifth_known = await client.post(f"{AUTH}/login", json=known)
        assert fifth_unknown.status_code == fifth_known.status_code == 423
        assert fifth_known.json()["code"] == "AUTH_ACCOUNT_LOCKED"

    async def test_ac_102_5_correct_password_inside_the_lock_is_refused(
        self, client: AsyncClient, faculty
    ) -> None:
        for _ in range(5):
            await _login(client, password="wrong")
        assert (await _login(client)).status_code == 423


class TestPersistence:
    async def test_failures_and_ledger_rows_are_committed(
        self, client: AsyncClient, db: AsyncSession, faculty
    ) -> None:
        """Regression: faculty sign-in never committed, so lockout counters,
        ledger rows, and spent challenges vanished at the end of the request."""
        await _login(client, password="wrong")
        await db.rollback()  # discards anything the request left uncommitted
        failures = await db.scalar(
            select(func.count())
            .select_from(AuthenticationFailure)
            .where(AuthenticationFailure.user_id == faculty["id"])
        )
        ledger = await db.scalar(
            select(func.count())
            .select_from(AuthAuditLog)
            .where(AuthAuditLog.attempted_id == EMPLOYEE_ID)
        )
        assert failures == 1 and ledger == 1

    async def test_enrollment_and_session_are_committed(
        self, client: AsyncClient, db: AsyncSession, faculty
    ) -> None:
        await _enrolled_session(client)
        await db.rollback()
        factor = await db.scalar(
            select(UserMfaCredential).where(UserMfaCredential.user_id == faculty["id"])
        )
        assert factor is not None and factor.is_enrolled
        assert await db.scalar(select(func.count()).select_from(RefreshToken)) == 1
        assert await db.scalar(select(func.count()).select_from(UserActiveSession)) == 1
        assert await db.get(MfaChallenge, faculty["id"]) is None


class TestMfa:
    async def test_ac_102_2_enrollment_issues_a_session_and_recovery_codes(
        self, client: AsyncClient, db: AsyncSession, faculty
    ) -> None:
        """AC-102.2: confirming the seed opens the first session."""
        session, _secret, _counter = await _enrolled_session(client)
        assert session["role"] == "faculty"
        claims = decode_token(session["access_token"], "access")
        assert claims["role"] == "faculty" and claims["mfa"] is True
        assert len(session["recovery_codes"]) == 10
        stored = (await db.scalars(select(UserMfaRecoveryCode.code_hash))).all()
        assert all(code not in stored for code in session["recovery_codes"])
        me = await client.get(
            f"{AUTH}/me",
            headers={"Authorization": f"Bearer {session['access_token']}"},
        )
        assert me.status_code == 200 and me.json()["employee_id"] == EMPLOYEE_ID

    async def test_ac_102_6_password_alone_cannot_re_enroll(
        self, client: AsyncClient, faculty
    ) -> None:
        """Regression: re-enrolling replaced an enrolled authenticator."""
        await _enrolled_session(client)
        challenge = (await _login(client)).json()
        assert challenge["enrollment_required"] is False
        response = await client.post(
            f"{AUTH}/mfa/totp/enroll",
            json={"challenge_token": challenge["challenge_token"]},
        )
        assert response.status_code == 403
        assert response.json()["code"] == "AUTH_MFA_ENROLLMENT_REQUIRED"

    async def test_ac_102_3_4_verify_and_replay(
        self, client: AsyncClient, faculty
    ) -> None:
        """AC-102.3/102.4: a current code signs in; a used code is refused."""
        _session, secret, counter = await _enrolled_session(client)
        challenge = (await _login(client)).json()["challenge_token"]
        replay = await client.post(
            f"{AUTH}/mfa/verify",
            json={"challenge_token": challenge, "code": totp_at(secret, counter)},
        )
        assert replay.status_code == 401
        assert replay.json()["code"] == "AUTH_MFA_CODE_REUSED"
        fresh = await client.post(
            f"{AUTH}/mfa/verify",
            json={"challenge_token": challenge, "code": totp_at(secret, counter + 1)},
        )
        assert fresh.status_code == 200, fresh.text
        assert fresh.json()["role"] == "faculty"

    async def test_recovery_code_works_once(self, client: AsyncClient, faculty) -> None:
        session, _secret, _counter = await _enrolled_session(client)
        code = session["recovery_codes"][0]
        first = (await _login(client)).json()["challenge_token"]
        used = await client.post(
            f"{AUTH}/mfa/verify", json={"challenge_token": first, "recovery_code": code}
        )
        assert used.status_code == 200, used.text
        second = (await _login(client)).json()["challenge_token"]
        again = await client.post(
            f"{AUTH}/mfa/verify",
            json={"challenge_token": second, "recovery_code": code},
        )
        assert again.status_code == 401
        assert again.json()["code"] == "AUTH_MFA_INVALID"

    async def test_wrong_codes_lock_on_the_fifth(
        self, client: AsyncClient, faculty
    ) -> None:
        await _enrolled_session(client)
        challenge = (await _login(client)).json()["challenge_token"]
        for _ in range(4):
            wrong = await client.post(
                f"{AUTH}/mfa/verify",
                json={"challenge_token": challenge, "code": "000000"},
            )
            assert wrong.status_code == 401
        fifth = await client.post(
            f"{AUTH}/mfa/verify",
            json={"challenge_token": challenge, "code": "000000"},
        )
        assert fifth.status_code == 423


class TestRefreshAndLogout:
    async def test_refresh_rotates_and_logout_ends_the_session(
        self, client: AsyncClient, faculty
    ) -> None:
        """Regression: faculty refresh tokens were never stored, so refresh
        always failed and logout did nothing."""
        session, _secret, _counter = await _enrolled_session(client)
        refreshed = await client.post(
            f"{AUTH}/refresh", json={"refresh_token": session["refresh_token"]}
        )
        assert refreshed.status_code == 200, refreshed.text
        assert refreshed.json()["expires_in"] == 900
        assert (
            await client.post(
                "/api/v1/auth/refresh",
                json={"refresh_token": refreshed.json()["refresh_token"]},
            )
        ).status_code == 401
        logout = await client.post(
            f"{AUTH}/logout", json={"refresh_token": refreshed.json()["refresh_token"]}
        )
        assert logout.status_code == 204
        me = await client.get(
            f"{AUTH}/me",
            headers={"Authorization": f"Bearer {refreshed.json()['access_token']}"},
        )
        assert me.status_code == 401


class TestLedgerAccess:
    async def test_ac_102_8_ledger_needs_read_all_audit_entry(
        self, client: AsyncClient, faculty
    ) -> None:
        """Regression: the sign-in ledger was readable with no credentials."""
        assert (await client.get("/api/v1/faculty-auth/audit")).status_code == 404
        assert (await client.get("/api/v1/audit/sign-in-attempts")).status_code == 401
        session, _secret, _counter = await _enrolled_session(client)
        response = await client.get(
            "/api/v1/audit/sign-in-attempts",
            headers={"Authorization": f"Bearer {session['access_token']}"},
        )
        assert response.status_code == 403

    async def test_ac_102_8_ledger_records_mfa_completion(
        self, client: AsyncClient, db: AsyncSession, faculty
    ) -> None:
        await _enrolled_session(client)
        rows = (
            await db.scalars(
                select(AuthAuditLog).order_by(AuthAuditLog.created_at.asc())
            )
        ).all()
        assert [row.success for row in rows] == [True, True]
        assert rows[-1].mfa_used is True


@pytest.fixture
def secret_from_uri():
    def _secret(uri: str) -> str:
        return parse_qs(urlsplit(uri).query)["secret"][0]

    return _secret


async def test_provisioning_uri_carries_the_shared_issuer(
    client: AsyncClient, faculty, secret_from_uri
) -> None:
    challenge = (await _login(client)).json()["challenge_token"]
    enrolled = (
        await client.post(
            f"{AUTH}/mfa/totp/enroll", json={"challenge_token": challenge}
        )
    ).json()
    assert "issuer=CBPUPSIS" in enrolled["provisioning_uri"]
    assert secret_from_uri(enrolled["provisioning_uri"]) == enrolled["secret"]


async def test_unknown_client_address_still_signs_in_with_a_null_address(
    addressless_client: AsyncClient, db: AsyncSession, faculty
) -> None:
    """No usable caller address never blocks sign-in; NULL is stored."""
    await _enrolled_session(addressless_client)

    sessions = (
        await db.scalars(
            select(UserActiveSession.ip_address).where(
                UserActiveSession.user_id == faculty["id"]
            )
        )
    ).all()
    ledger = (
        await db.scalars(
            select(AuthAuditLog.ip_address).where(AuthAuditLog.user_id == faculty["id"])
        )
    ).all()
    assert sessions == [None]
    assert ledger == [None, None]


async def _expired_temporary_password(db, user_id, password: str) -> None:
    """Give the account a temporary password that expired a minute ago."""
    from datetime import UTC, datetime, timedelta

    from cbpupsis_shared.domains.auth.security import hash_password
    from cbpupsis_shared.domains.users import service as users_service

    await users_service.stage_temporary_password(
        db,
        user_id,
        hash_password(password),
        datetime.now(UTC) - timedelta(minutes=1),
    )
    await db.commit()


async def test_an_expired_temporary_password_is_refused_at_the_password_step(
    client: AsyncClient, db: AsyncSession, faculty
) -> None:
    await _expired_temporary_password(db, faculty["id"], PASSWORD)
    response = await _login(client)
    assert response.status_code == 401
    assert response.json()["code"] == "AUTH_TEMPORARY_PASSWORD_EXPIRED"
    wrong = await _login(client, "Wrong-Password-123!")
    assert wrong.json()["code"] == "AUTH_INVALID_CREDENTIALS"
