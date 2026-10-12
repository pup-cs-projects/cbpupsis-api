"""Tests for the mailed-token flows: verification, reset, and change.

The properties under test here are the ones a naive implementation still passes
a happy-path test without having: single use, expiry, purpose separation, and
enumeration resistance. Each of those is a silent vulnerability when it breaks,
so each gets an explicit test rather than being implied by a successful flow.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_core.exceptions import UnauthorizedError
from cbpupsis_database.models.auth import OneTimeToken, TokenPurpose
from cbpupsis_database.models.users import User
from cbpupsis_shared.domains.auth import service as auth_service
from cbpupsis_shared.domains.auth.security import (
    generate_one_time_token,
    hash_one_time_token,
)
from cbpupsis_shared.domains.users import service as users_service
from tests.conftest import token_from_email


async def _expire_tokens(db: AsyncSession, user_id: uuid.UUID) -> None:
    """Backdate every live token for a user so it is past its expiry."""
    await db.execute(
        update(OneTimeToken)
        .where(OneTimeToken.user_id == user_id)
        .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
    )
    await db.commit()


async def _unverify(db: AsyncSession, user_id: uuid.UUID) -> None:
    """Clear a user's verification stamp.

    Written as a direct UPDATE rather than a service call on purpose: nothing in
    the product un-verifies an address, so exposing an API for it would be
    adding production surface that exists only for a test. This reproduces the
    state (an account that holds a live session but is no longer verified) that
    the refresh gate has to handle.
    """
    await db.execute(
        update(User).where(User.id == user_id).values(email_verified_at=None)
    )
    await db.commit()


class TestOneTimeTokenPrimitives:
    def test_raw_token_is_not_the_stored_value(self) -> None:
        """A database leak must not yield anything replayable."""
        raw, hashed = generate_one_time_token()
        assert raw != hashed
        assert hashed == hash_one_time_token(raw)

    def test_tokens_are_unique(self) -> None:
        assert len({generate_one_time_token()[0] for _ in range(100)}) == 100

    def test_token_has_meaningful_entropy(self) -> None:
        """token_urlsafe(32) is 256 bits; a short token would be guessable."""
        raw, _ = generate_one_time_token()
        assert len(raw) >= 40


class TestEmailVerification:
    async def test_registration_sends_a_verification_email(
        self, client: AsyncClient, sent_emails: list[dict[str, str]]
    ) -> None:
        response = await client.post(
            "/api/v1/auth/register",
            json={"email": "verify@example.com", "password": "a-long-enough-password"},
        )
        assert response.status_code == 201
        assert response.json()["email_verified_at"] is None
        assert len(sent_emails) == 1
        assert sent_emails[0]["to"] == "verify@example.com"

    async def test_verifying_marks_the_user_verified(
        self, client: AsyncClient, sent_emails: list[dict[str, str]]
    ) -> None:
        await client.post(
            "/api/v1/auth/register",
            json={"email": "verify2@example.com", "password": "a-long-enough-password"},
        )
        token = token_from_email(sent_emails[0])

        assert (
            await client.post("/api/v1/auth/verify-email", json={"token": token})
        ).status_code == 204

        login = await client.post(
            "/api/v1/auth/login",
            json={"email": "verify2@example.com", "password": "a-long-enough-password"},
        )
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
        me = await client.get("/api/v1/users/me", headers=headers)
        assert me.json()["email_verified_at"] is not None

    async def test_token_cannot_be_used_twice(
        self, client: AsyncClient, sent_emails: list[dict[str, str]]
    ) -> None:
        """Single use is the core property: a link in a browser history or a
        forwarded mail must be inert after the first redemption."""
        await client.post(
            "/api/v1/auth/register",
            json={"email": "once@example.com", "password": "a-long-enough-password"},
        )
        token = token_from_email(sent_emails[0])

        first = await client.post("/api/v1/auth/verify-email", json={"token": token})
        second = await client.post("/api/v1/auth/verify-email", json={"token": token})
        assert first.status_code == 204
        assert second.status_code == 401

    async def test_expired_token_is_rejected(
        self,
        client: AsyncClient,
        db: AsyncSession,
        sent_emails: list[dict[str, str]],
    ) -> None:
        register = await client.post(
            "/api/v1/auth/register",
            json={"email": "stale@example.com", "password": "a-long-enough-password"},
        )
        token = token_from_email(sent_emails[0])
        await _expire_tokens(db, uuid.UUID(register.json()["id"]))

        response = await client.post("/api/v1/auth/verify-email", json={"token": token})
        assert response.status_code == 401

    async def test_unknown_and_used_tokens_are_indistinguishable(
        self, client: AsyncClient, sent_emails: list[dict[str, str]]
    ) -> None:
        """A differing message would tell an attacker whether a guessed value
        was ever a real token."""
        await client.post(
            "/api/v1/auth/register",
            json={"email": "same@example.com", "password": "a-long-enough-password"},
        )
        token = token_from_email(sent_emails[0])
        await client.post("/api/v1/auth/verify-email", json={"token": token})

        used = await client.post("/api/v1/auth/verify-email", json={"token": token})
        unknown = await client.post(
            "/api/v1/auth/verify-email", json={"token": "not-a-real-token"}
        )
        assert used.status_code == unknown.status_code == 401
        assert used.json()["detail"] == unknown.json()["detail"]

    async def test_a_reset_token_cannot_verify_an_email(
        self,
        client: AsyncClient,
        registered_user: dict[str, str],
        sent_emails: list[dict[str, str]],
        drain_outbox,
    ) -> None:
        """The purpose column is what keeps one table from crossing two flows."""
        sent_emails.clear()
        await client.post(
            "/api/v1/auth/forgot-password", json={"email": registered_user["email"]}
        )
        await drain_outbox()
        reset_token = token_from_email(sent_emails[-1])

        response = await client.post(
            "/api/v1/auth/verify-email", json={"token": reset_token}
        )
        assert response.status_code == 401


class TestResendVerification:
    async def test_resend_issues_a_working_token(
        self, client: AsyncClient, sent_emails: list[dict[str, str]]
    ) -> None:
        await client.post(
            "/api/v1/auth/register",
            json={"email": "resend@example.com", "password": "a-long-enough-password"},
        )
        await client.post(
            "/api/v1/auth/resend-verification", json={"email": "resend@example.com"}
        )
        assert len(sent_emails) == 2

        response = await client.post(
            "/api/v1/auth/verify-email",
            json={"token": token_from_email(sent_emails[-1])},
        )
        assert response.status_code == 204

    async def test_resend_invalidates_the_previous_token(
        self, client: AsyncClient, sent_emails: list[dict[str, str]]
    ) -> None:
        """Otherwise every resend leaves another live credential in an inbox."""
        await client.post(
            "/api/v1/auth/register",
            json={"email": "super@example.com", "password": "a-long-enough-password"},
        )
        first_token = token_from_email(sent_emails[0])
        await client.post(
            "/api/v1/auth/resend-verification", json={"email": "super@example.com"}
        )

        response = await client.post(
            "/api/v1/auth/verify-email", json={"token": first_token}
        )
        assert response.status_code == 401

    async def test_unknown_address_still_returns_204(
        self, client: AsyncClient, sent_emails: list[dict[str, str]]
    ) -> None:
        response = await client.post(
            "/api/v1/auth/resend-verification", json={"email": "nobody@example.com"}
        )
        assert response.status_code == 204
        assert sent_emails == []

    async def test_already_verified_address_sends_nothing(
        self, client: AsyncClient, sent_emails: list[dict[str, str]]
    ) -> None:
        await client.post(
            "/api/v1/auth/register",
            json={"email": "done@example.com", "password": "a-long-enough-password"},
        )
        await client.post(
            "/api/v1/auth/verify-email",
            json={"token": token_from_email(sent_emails[0])},
        )
        sent_emails.clear()

        response = await client.post(
            "/api/v1/auth/resend-verification", json={"email": "done@example.com"}
        )
        assert response.status_code == 204
        assert sent_emails == []


class TestVerificationIsRequiredToLogIn:
    """Login is gated on a verified address, and the gate sits *after* the
    password check so it cannot be used to enumerate accounts."""

    async def test_unverified_user_is_refused_with_a_machine_readable_code(
        self, client: AsyncClient, sent_emails: list[dict[str, str]]
    ) -> None:
        await client.post(
            "/api/v1/auth/register",
            json={"email": "unver@example.com", "password": "a-long-enough-password"},
        )
        response = await client.post(
            "/api/v1/auth/login",
            json={"email": "unver@example.com", "password": "a-long-enough-password"},
        )
        assert response.status_code == 403
        body = response.json()
        # The frontend branches on the code, never on the prose.
        assert body["code"] == "email_not_verified"
        assert "access_token" not in body
        assert "request_id" in body

    async def test_wrong_password_on_an_unverified_account_is_still_401(
        self, client: AsyncClient, sent_emails: list[dict[str, str]]
    ) -> None:
        """**The enumeration guarantee.** The 403 must be unreachable without
        the correct password, or it becomes an oracle: an attacker could probe
        an email list with any password and read registration status off the
        status code. Wrong password must look identical to unknown address."""
        await client.post(
            "/api/v1/auth/register",
            json={"email": "probe@example.com", "password": "a-long-enough-password"},
        )

        wrong_password = await client.post(
            "/api/v1/auth/login",
            json={"email": "probe@example.com", "password": "not-the-password"},
        )
        unknown_email = await client.post(
            "/api/v1/auth/login",
            json={"email": "nobody@example.com", "password": "not-the-password"},
        )

        assert wrong_password.status_code == unknown_email.status_code == 401
        assert wrong_password.json()["detail"] == unknown_email.json()["detail"]
        # No code on either: a code here would itself distinguish them.
        assert "code" not in wrong_password.json()
        assert "code" not in unknown_email.json()

    async def test_verifying_then_logging_in_succeeds(
        self, client: AsyncClient, sent_emails: list[dict[str, str]]
    ) -> None:
        """The gate opens once the mailed token is redeemed."""
        await client.post(
            "/api/v1/auth/register",
            json={"email": "opens@example.com", "password": "a-long-enough-password"},
        )
        blocked = await client.post(
            "/api/v1/auth/login",
            json={"email": "opens@example.com", "password": "a-long-enough-password"},
        )
        assert blocked.status_code == 403

        await client.post(
            "/api/v1/auth/verify-email",
            json={"token": token_from_email(sent_emails[0])},
        )

        allowed = await client.post(
            "/api/v1/auth/login",
            json={"email": "opens@example.com", "password": "a-long-enough-password"},
        )
        assert allowed.status_code == 200
        assert allowed.json()["access_token"]

    async def test_inactive_unverified_account_gets_401_not_403(
        self,
        client: AsyncClient,
        db: AsyncSession,
        sent_emails: list[dict[str, str]],
    ) -> None:
        """Ordering: the active check runs before the verification check, so a
        disabled account is indistinguishable from a wrong password even when
        the caller has the right one."""
        registered = await client.post(
            "/api/v1/auth/register",
            json={"email": "off@example.com", "password": "a-long-enough-password"},
        )
        await users_service.deactivate(db, uuid.UUID(registered.json()["id"]))

        response = await client.post(
            "/api/v1/auth/login",
            json={"email": "off@example.com", "password": "a-long-enough-password"},
        )
        assert response.status_code == 401
        assert "code" not in response.json()

    async def test_refresh_stops_working_once_the_address_is_unverified(
        self,
        client: AsyncClient,
        db: AsyncSession,
        registered_user: dict[str, str],
        sent_emails: list[dict[str, str]],
    ) -> None:
        """Refresh mints access tokens, so it enforces the same gate — otherwise
        a session opened earlier renews itself around the block forever."""
        login = await client.post(
            "/api/v1/auth/login",
            json={
                "email": registered_user["email"],
                "password": registered_user["password"],
            },
        )
        assert login.status_code == 200
        refresh_token = login.json()["refresh_token"]

        # Un-verify the address behind the live session.
        await _unverify(db, uuid.UUID(registered_user["id"]))

        response = await client.post(
            "/api/v1/auth/refresh", json={"refresh_token": refresh_token}
        )
        assert response.status_code == 403
        assert response.json()["code"] == "email_not_verified"

    async def test_a_refused_refresh_does_not_leave_the_token_usable(
        self,
        client: AsyncClient,
        db: AsyncSession,
        registered_user: dict[str, str],
        sent_emails: list[dict[str, str]],
    ) -> None:
        """The presented token is consumed even when the gate refuses, so a
        blocked account cannot retry with it indefinitely."""
        login = await client.post(
            "/api/v1/auth/login",
            json={
                "email": registered_user["email"],
                "password": registered_user["password"],
            },
        )
        refresh_token = login.json()["refresh_token"]
        await _unverify(db, uuid.UUID(registered_user["id"]))
        assert (
            await client.post(
                "/api/v1/auth/refresh", json={"refresh_token": refresh_token}
            )
        ).status_code == 403

        # Re-verify, then replay the token that was refused: it is dead.
        await users_service.mark_email_verified(db, uuid.UUID(registered_user["id"]))
        replay = await client.post(
            "/api/v1/auth/refresh", json={"refresh_token": refresh_token}
        )
        assert replay.status_code == 401

    async def test_require_verified_email_rejects_then_allows(
        self,
        db: AsyncSession,
        registered_user: dict[str, str],
        sent_emails: list[dict[str, str]],
    ) -> None:
        """The dependency is exercised directly — no endpoint in the template
        uses it yet, and a guard nothing calls is a guard nothing tests."""
        from cbpupsis_core.exceptions import ForbiddenError
        from cbpupsis_shared.domains.auth.dependencies import (
            CurrentUser,
            require_verified_email,
        )

        user_id = uuid.UUID(registered_user["id"])
        principal = CurrentUser(id=user_id, email=registered_user["email"])
        # The fixture arrives verified (login requires it); this test is about
        # the guard's two branches, so start it from the unverified side.
        await _unverify(db, user_id)

        with pytest.raises(ForbiddenError):
            await require_verified_email(user=principal, db=db)

        await users_service.mark_email_verified(db, user_id)
        assert await require_verified_email(user=principal, db=db) is principal


class TestForgotPassword:
    async def test_known_address_returns_202_and_queues(
        self,
        client: AsyncClient,
        registered_user: dict[str, str],
        sent_emails: list[dict[str, str]],
        drain_outbox,
    ) -> None:
        sent_emails.clear()
        response = await client.post(
            "/api/v1/auth/forgot-password", json={"email": registered_user["email"]}
        )
        assert response.status_code == 202
        assert (
            response.json()["message"]
            == "If that address is registered, a reset link has been sent."
        )
        assert sent_emails == []
        await drain_outbox()
        assert len(sent_emails) == 1

    async def test_unknown_address_is_indistinguishable(
        self,
        client: AsyncClient,
        registered_user: dict[str, str],
        sent_emails: list[dict[str, str]],
    ) -> None:
        """Same status and same body, or the endpoint tests an email list
        against the user base for free."""
        known = await client.post(
            "/api/v1/auth/forgot-password", json={"email": registered_user["email"]}
        )
        unknown = await client.post(
            "/api/v1/auth/forgot-password", json={"email": "ghost@example.com"}
        )
        assert known.status_code == unknown.status_code == 202
        assert known.content == unknown.content
        assert (
            known.json()["message"]
            == "If that address is registered, a reset link has been sent."
        )

    async def test_deactivated_account_gets_no_reset_mail(
        self,
        client: AsyncClient,
        db: AsyncSession,
        registered_user: dict[str, str],
        sent_emails: list[dict[str, str]],
    ) -> None:
        await users_service.deactivate(db, uuid.UUID(registered_user["id"]))
        sent_emails.clear()

        response = await client.post(
            "/api/v1/auth/forgot-password", json={"email": registered_user["email"]}
        )
        assert response.status_code == 202
        assert (
            response.json()["message"]
            == "If that address is registered, a reset link has been sent."
        )
        assert sent_emails == []


COMPLEX_NEW_PASSWORD = "A-freshly-chosen-password-1!"


class TestResetPassword:
    async def _request_reset(
        self,
        client: AsyncClient,
        email: str,
        sent_emails: list[dict[str, str]],
        drain_outbox,
    ) -> str:
        sent_emails.clear()
        await client.post("/api/v1/auth/forgot-password", json={"email": email})
        await drain_outbox()
        return token_from_email(sent_emails[0])

    async def test_reset_sets_the_new_password(
        self,
        client: AsyncClient,
        registered_user: dict[str, str],
        sent_emails: list[dict[str, str]],
        drain_outbox,
    ) -> None:
        token = await self._request_reset(
            client, registered_user["email"], sent_emails, drain_outbox
        )
        response = await client.post(
            "/api/v1/auth/reset-password",
            json={"token": token, "new_password": COMPLEX_NEW_PASSWORD},
        )
        assert response.status_code == 200
        assert "sessionsRevoked" in response.json()

        new_login = await client.post(
            "/api/v1/auth/login",
            json={
                "email": registered_user["email"],
                "password": COMPLEX_NEW_PASSWORD,
            },
        )
        assert new_login.status_code == 200

    async def test_old_password_stops_working(
        self,
        client: AsyncClient,
        registered_user: dict[str, str],
        sent_emails: list[dict[str, str]],
        drain_outbox,
    ) -> None:
        token = await self._request_reset(
            client, registered_user["email"], sent_emails, drain_outbox
        )
        await client.post(
            "/api/v1/auth/reset-password",
            json={"token": token, "new_password": COMPLEX_NEW_PASSWORD},
        )
        response = await client.post(
            "/api/v1/auth/login",
            json={
                "email": registered_user["email"],
                "password": registered_user["password"],
            },
        )
        assert response.status_code == 401

    async def test_reset_revokes_every_refresh_token(
        self,
        client: AsyncClient,
        registered_user: dict[str, str],
        sent_emails: list[dict[str, str]],
        drain_outbox,
    ) -> None:
        """A reset is often recovery from compromise; a session the attacker
        already holds must not survive it."""
        login = await client.post(
            "/api/v1/auth/login",
            json={
                "email": registered_user["email"],
                "password": registered_user["password"],
            },
        )
        attacker_refresh = login.json()["refresh_token"]

        token = await self._request_reset(
            client, registered_user["email"], sent_emails, drain_outbox
        )
        response = await client.post(
            "/api/v1/auth/reset-password",
            json={"token": token, "new_password": COMPLEX_NEW_PASSWORD},
        )
        assert response.status_code == 200
        assert response.json()["sessionsRevoked"] >= 1

        revoked_response = await client.post(
            "/api/v1/auth/refresh", json={"refresh_token": attacker_refresh}
        )
        assert revoked_response.status_code == 401

    async def test_token_cannot_be_reused(
        self,
        client: AsyncClient,
        registered_user: dict[str, str],
        sent_emails: list[dict[str, str]],
        drain_outbox,
    ) -> None:
        token = await self._request_reset(
            client, registered_user["email"], sent_emails, drain_outbox
        )
        first = await client.post(
            "/api/v1/auth/reset-password",
            json={"token": token, "new_password": COMPLEX_NEW_PASSWORD},
        )
        second = await client.post(
            "/api/v1/auth/reset-password",
            json={"token": token, "new_password": "Another-complex-pass-2@"},
        )
        assert first.status_code == 200
        assert second.status_code == 410
        assert second.json()["code"] == "AUTH_RESET_TOKEN_USED"

    async def test_expired_reset_token_is_rejected(
        self,
        client: AsyncClient,
        db: AsyncSession,
        registered_user: dict[str, str],
        sent_emails: list[dict[str, str]],
        drain_outbox,
    ) -> None:
        token = await self._request_reset(
            client, registered_user["email"], sent_emails, drain_outbox
        )
        await _expire_tokens(db, uuid.UUID(registered_user["id"]))

        response = await client.post(
            "/api/v1/auth/reset-password",
            json={"token": token, "new_password": COMPLEX_NEW_PASSWORD},
        )
        assert response.status_code == 410
        assert response.json()["code"] == "AUTH_RESET_TOKEN_EXPIRED"

    async def test_a_verification_token_cannot_reset_a_password(
        self, client: AsyncClient, sent_emails: list[dict[str, str]]
    ) -> None:
        await client.post(
            "/api/v1/auth/register",
            json={"email": "cross@example.com", "password": "a-long-enough-password"},
        )
        verification_token = token_from_email(sent_emails[0])

        response = await client.post(
            "/api/v1/auth/reset-password",
            json={
                "token": verification_token,
                "new_password": COMPLEX_NEW_PASSWORD,
            },
        )
        assert response.status_code == 401

    async def test_short_new_password_rejected(
        self,
        client: AsyncClient,
        registered_user: dict[str, str],
        sent_emails: list[dict[str, str]],
        drain_outbox,
    ) -> None:
        token = await self._request_reset(
            client, registered_user["email"], sent_emails, drain_outbox
        )
        response = await client.post(
            "/api/v1/auth/reset-password",
            json={"token": token, "new_password": "short"},
        )
        assert response.status_code == 422

    async def test_ac_005_3_weak_password_rejected_with_unmet_rules(
        self,
        client: AsyncClient,
        registered_user: dict[str, str],
        sent_emails: list[dict[str, str]],
        drain_outbox,
    ) -> None:
        """AC-005.3: Password complexity rejects missing sets (uppercase,
        lowercase, digits, symbols) with 422 AUTH_PASSWORD_TOO_WEAK.
        """
        token = await self._request_reset(
            client, registered_user["email"], sent_emails, drain_outbox
        )
        response = await client.post(
            "/api/v1/auth/reset-password",
            json={"token": token, "new_password": "alllowercasepassword"},
        )
        assert response.status_code == 422
        body = response.json()
        assert body["code"] == "AUTH_PASSWORD_TOO_WEAK"
        assert "uppercase" in body["unmet_rules"]
        assert "digit" in body["unmet_rules"]
        assert "symbol" in body["unmet_rules"]

    async def test_ac_005_6_sessions_revoked_count_accurate(
        self,
        client: AsyncClient,
        registered_user: dict[str, str],
        sent_emails: list[dict[str, str]],
        drain_outbox,
    ) -> None:
        """AC-005.6: Successful password reset returns 200 with sessionsRevoked."""
        # Open 2 sessions
        s1 = await client.post(
            "/api/v1/auth/login",
            json={
                "email": registered_user["email"],
                "password": registered_user["password"],
            },
        )
        s2 = await client.post(
            "/api/v1/auth/login",
            json={
                "email": registered_user["email"],
                "password": registered_user["password"],
            },
        )
        assert s1.status_code == 200
        assert s2.status_code == 200

        token = await self._request_reset(
            client, registered_user["email"], sent_emails, drain_outbox
        )
        response = await client.post(
            "/api/v1/auth/reset-password",
            json={"token": token, "new_password": COMPLEX_NEW_PASSWORD},
        )
        assert response.status_code == 200
        assert response.json()["sessionsRevoked"] == 2

    async def test_ac_005_8_audit_trail_recorded(
        self,
        client: AsyncClient,
        db: AsyncSession,
        registered_user: dict[str, str],
        sent_emails: list[dict[str, str]],
        drain_outbox,
    ) -> None:
        """AC-005.8: Audit trail records password reset requested and completed
        without leaking passwords or tokens into payloads.
        """
        from cbpupsis_shared.domains.audit import service as audit_service

        token = await self._request_reset(
            client, registered_user["email"], sent_emails, drain_outbox
        )
        reset_res = await client.post(
            "/api/v1/auth/reset-password",
            json={"token": token, "new_password": COMPLEX_NEW_PASSWORD},
        )
        assert reset_res.status_code == 200

        user_id = uuid.UUID(registered_user["id"])
        page = await audit_service.list_entries(db, actor_id=user_id)
        actions = [entry.action for entry in page.items]
        assert "auth.password_reset_requested" in actions
        assert "auth.password_reset_completed" in actions

        # Verify no token or password leaked into audit payloads
        for entry in page.items:
            payload_str = str(entry.payload)
            assert token not in payload_str
            assert COMPLEX_NEW_PASSWORD not in payload_str
            assert registered_user["password"] not in payload_str


class TestChangePassword:
    async def test_change_requires_authentication(self, client: AsyncClient) -> None:
        response = await client.post(
            "/api/v1/auth/change-password",
            json={
                "current_password": "whatever-goes-here",
                "new_password": "a-freshly-chosen-password",
            },
        )
        assert response.status_code == 401

    async def test_wrong_current_password_is_rejected(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        sent_emails: list[dict[str, str]],
    ) -> None:
        """Without this check a stolen access token could seize the account."""
        response = await client.post(
            "/api/v1/auth/change-password",
            headers=auth_headers,
            json={
                "current_password": "not-the-real-password",
                "new_password": "a-freshly-chosen-password",
            },
        )
        assert response.status_code == 401

    async def test_change_returns_a_working_new_pair(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        registered_user: dict[str, str],
        sent_emails: list[dict[str, str]],
    ) -> None:
        response = await client.post(
            "/api/v1/auth/change-password",
            headers=auth_headers,
            json={
                "current_password": registered_user["password"],
                "new_password": "a-freshly-chosen-password",
            },
        )
        assert response.status_code == 200
        pair = response.json()

        # The returned refresh token is live...
        rotated = await client.post(
            "/api/v1/auth/refresh", json={"refresh_token": pair["refresh_token"]}
        )
        assert rotated.status_code == 200
        # ...and the returned access token authenticates.
        whoami = await client.get(
            "/api/v1/auth/whoami",
            headers={"Authorization": f"Bearer {pair['access_token']}"},
        )
        assert whoami.status_code == 200

    async def test_change_revokes_the_previous_sessions(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        registered_user: dict[str, str],
        sent_emails: list[dict[str, str]],
    ) -> None:
        """The documented choice: revoke everything and hand back a fresh pair,
        so a password change really does end every other session."""
        login = await client.post(
            "/api/v1/auth/login",
            json={
                "email": registered_user["email"],
                "password": registered_user["password"],
            },
        )
        old_refresh = login.json()["refresh_token"]

        await client.post(
            "/api/v1/auth/change-password",
            headers=auth_headers,
            json={
                "current_password": registered_user["password"],
                "new_password": "a-freshly-chosen-password",
            },
        )

        response = await client.post(
            "/api/v1/auth/refresh", json={"refresh_token": old_refresh}
        )
        assert response.status_code == 401

    async def test_change_kills_other_sessions_not_just_the_callers(
        self,
        client: AsyncClient,
        registered_user: dict[str, str],
        sent_emails: list[dict[str, str]],
    ) -> None:
        """Two independent sessions, and the one that did *not* change the
        password must die.

        Distinct from the test above, which reuses the same login the caller
        holds: an implementation that revoked only the calling session — or only
        the token it was handed — would still pass that one. This is the
        property that matters when a user changes their password *because* they
        believe someone else is signed in as them.
        """
        # Session A: the caller. Session B: a second, independent login.
        session_a = await client.post(
            "/api/v1/auth/login",
            json={
                "email": registered_user["email"],
                "password": registered_user["password"],
            },
        )
        session_b = await client.post(
            "/api/v1/auth/login",
            json={
                "email": registered_user["email"],
                "password": registered_user["password"],
            },
        )
        a_access = session_a.json()["access_token"]
        b_refresh = session_b.json()["refresh_token"]
        assert session_a.json()["refresh_token"] != b_refresh, (
            "the two logins must be independent sessions for this test to mean anything"
        )

        # B's token is deliberately left untouched until after the change:
        # spending it here would rotate it, and the replay-detection path would
        # then be what kills it, not the password change.
        changed = await client.post(
            "/api/v1/auth/change-password",
            headers={"Authorization": f"Bearer {a_access}"},
            json={
                "current_password": registered_user["password"],
                "new_password": "a-freshly-chosen-password",
            },
        )
        assert changed.status_code == 200

        # A's replacement pair is live immediately after the change...
        a_refresh = changed.json()["refresh_token"]
        assert (
            await client.post("/api/v1/auth/refresh", json={"refresh_token": a_refresh})
        ).status_code == 200

        # ...and B is dead, without B having done anything at all. This is the
        # assertion the whole test exists for.
        response = await client.post(
            "/api/v1/auth/refresh", json={"refresh_token": b_refresh}
        )
        assert response.status_code == 401

        # Checked in this order deliberately. Presenting B's revoked token is
        # indistinguishable from replaying a rotated one, so it trips the
        # reuse-detection path and revokes the whole family — A included. That
        # is the intended conservative response to a suspected leak, but it
        # means asserting A still works *after* touching B would fail for a
        # reason that has nothing to do with the password change.

    async def test_new_password_works_at_login(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        registered_user: dict[str, str],
        sent_emails: list[dict[str, str]],
    ) -> None:
        await client.post(
            "/api/v1/auth/change-password",
            headers=auth_headers,
            json={
                "current_password": registered_user["password"],
                "new_password": "a-freshly-chosen-password",
            },
        )
        response = await client.post(
            "/api/v1/auth/login",
            json={
                "email": registered_user["email"],
                "password": "a-freshly-chosen-password",
            },
        )
        assert response.status_code == 200

    async def test_change_notifies_the_account_owner(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        registered_user: dict[str, str],
        sent_emails: list[dict[str, str]],
        drain_outbox,
    ) -> None:
        """How a victim of a takeover finds out it happened.

        The notice is staged on the outbox rather than sent inline, so that it
        survives a crash between the password write and the send. Draining is
        what the worker does in production; the boundary is real, so the test
        makes it explicit.
        """
        sent_emails.clear()
        await client.post(
            "/api/v1/auth/change-password",
            headers=auth_headers,
            json={
                "current_password": registered_user["password"],
                "new_password": "a-freshly-chosen-password",
            },
        )
        assert sent_emails == [], "the notice must not block the response"

        await drain_outbox()

        assert len(sent_emails) == 1
        assert sent_emails[0]["to"] == registered_user["email"]


class TestNothingSensitiveIsPersistedOrLogged:
    async def test_only_a_digest_reaches_the_database(
        self,
        client: AsyncClient,
        db: AsyncSession,
        sent_emails: list[dict[str, str]],
    ) -> None:
        """The stored value must not be the value in the email."""
        await client.post(
            "/api/v1/auth/register",
            json={"email": "digest@example.com", "password": "a-long-enough-password"},
        )
        raw_token = token_from_email(sent_emails[0])

        stored = (await db.execute(select(OneTimeToken.token_hash))).scalars().all()
        assert stored
        assert raw_token not in stored
        assert hash_one_time_token(raw_token) in stored

    async def test_service_errors_do_not_echo_the_token(self, db: AsyncSession) -> None:
        """An error message carrying the token would land it in logs."""
        with pytest.raises(UnauthorizedError) as excinfo:
            await auth_service.verify_email(db, "a-secret-token-value")
        assert "a-secret-token-value" not in str(excinfo.value)


class TestServiceLayer:
    """Framework-agnostic: these flows run with no HTTP involved."""

    async def test_forgot_password_is_silent_for_unknown_email(
        self, db: AsyncSession, sent_emails: list[dict[str, str]]
    ) -> None:
        """Returns None rather than raising, which is what lets the endpoint
        answer 204 without branching on existence."""
        assert await auth_service.forgot_password(db, "nobody@example.com") is None

    async def test_tokens_are_scoped_to_one_user(
        self, db: AsyncSession, sent_emails: list[dict[str, str]]
    ) -> None:
        """A token must verify the user it was issued to, and only them."""
        first = await auth_service.register(
            db, email="first@example.com", password="a-long-enough-password"
        )
        second = await auth_service.register(
            db, email="second@example.com", password="a-long-enough-password"
        )
        first_token = token_from_email(sent_emails[0])

        await auth_service.verify_email(db, first_token)

        assert (await users_service.get_by_id(db, first.id)).email_verified_at
        assert (await users_service.get_by_id(db, second.id)).email_verified_at is None

    async def test_consumed_token_is_stamped(
        self, db: AsyncSession, sent_emails: list[dict[str, str]]
    ) -> None:
        user = await auth_service.register(
            db, email="stamp@example.com", password="a-long-enough-password"
        )
        await auth_service.verify_email(db, token_from_email(sent_emails[0]))

        record = await db.scalar(
            select(OneTimeToken).where(
                OneTimeToken.user_id == user.id,
                OneTimeToken.purpose == TokenPurpose.email_verification,
            )
        )
        assert record is not None
        assert record.consumed_at is not None
