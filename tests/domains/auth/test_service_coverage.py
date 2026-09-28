"""Tests for the security-critical branches of ``auth/service.py``.

This module was the lowest-covered in the application (67%) and the one where a
gap matters most: the uncovered lines were the compromise-response paths —
refresh-token reuse revoking a whole family, reset and change consuming their
tokens and ending every session. Those only run when something has already gone
wrong, so nothing else exercises them, and a regression in one is invisible
until the day it matters.

The happy paths and the token lifecycle live in ``test_auth.py`` and
``test_email_flows.py``; this file deliberately holds the failure branches.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_core.exceptions import UnauthorizedError
from cbpupsis_shared.domains.auth import service as auth_service
from cbpupsis_shared.domains.auth.exceptions import (
    EmailAlreadyRegisteredError,
    IncorrectCurrentPasswordError,
    InvalidAuthTokenError,
    RefreshTokenReusedError,
)
from cbpupsis_shared.domains.users import service as users_service

PASSWORD = "correct-horse-battery-staple"


async def _verified_user(db: AsyncSession, email: str = "coverage@example.com"):
    user = await auth_service.register(db, email=email, password=PASSWORD)
    await users_service.mark_email_verified(db, user.id)
    return user


class TestRefreshFamilyRevocation:
    """Reuse of a rotated refresh token means the token leaked.

    Rotation alone does not protect anything if a replayed token merely fails:
    the attacker still holds whatever they stole. Cutting off the whole family
    is what turns theft into a dead end, and it is the branch nothing else runs.
    """

    async def test_reusing_a_rotated_token_is_refused(self, db: AsyncSession) -> None:
        user = await _verified_user(db)
        first = await auth_service.login(db, email=user.email, password=PASSWORD)
        await auth_service.refresh(db, first.refresh_token)

        with pytest.raises(RefreshTokenReusedError):
            await auth_service.refresh(db, first.refresh_token)

    async def test_reuse_revokes_the_whole_family(self, db: AsyncSession) -> None:
        """The token minted by the legitimate rotation must die too.

        This is the assertion that matters: refusing the replayed token while
        leaving the newest one alive would let whichever party holds it — quite
        possibly the attacker — carry on.
        """
        user = await _verified_user(db)
        first = await auth_service.login(db, email=user.email, password=PASSWORD)
        second = await auth_service.refresh(db, first.refresh_token)

        with pytest.raises(RefreshTokenReusedError):
            await auth_service.refresh(db, first.refresh_token)

        with pytest.raises(UnauthorizedError):
            await auth_service.refresh(db, second.refresh_token)

    async def test_an_unknown_jti_is_refused(self, db: AsyncSession) -> None:
        """A well-formed, correctly signed token whose jti was never recorded.

        Distinct from the reuse case below: there is no row at all, so the
        service cannot tell a forgery from a token whose record was purged, and
        refuses both. Minted directly rather than through login, since every
        issued token has a row by construction.
        """
        from cbpupsis_shared.domains.auth.security import create_refresh_token

        user = await _verified_user(db)
        token, _jti, _expires = create_refresh_token(user.id)

        with pytest.raises(InvalidAuthTokenError):
            await auth_service.refresh(db, token)

    async def test_refresh_is_refused_for_a_deactivated_account(
        self, db: AsyncSession
    ) -> None:
        """The account is re-read on every rotation, so deactivation takes
        effect immediately rather than when the access token expires."""
        user = await _verified_user(db)
        pair = await auth_service.login(db, email=user.email, password=PASSWORD)
        await users_service.deactivate(db, user.id)

        with pytest.raises(UnauthorizedError):
            await auth_service.refresh(db, pair.refresh_token)

    async def test_the_presented_token_stays_revoked_after_a_refusal(
        self, db: AsyncSession
    ) -> None:
        """The commit-before-raise the service documents.

        If the refusal rolled back, a blocked account would keep an endlessly
        retryable credential.
        """
        user = await _verified_user(db)
        pair = await auth_service.login(db, email=user.email, password=PASSWORD)
        await users_service.deactivate(db, user.id)

        with pytest.raises(UnauthorizedError):
            await auth_service.refresh(db, pair.refresh_token)

        await users_service.reactivate(db, user.id)
        with pytest.raises(UnauthorizedError):
            await auth_service.refresh(db, pair.refresh_token)


class TestResetTokenConsumption:
    async def test_reset_sets_the_new_password_and_ends_sessions(
        self, db: AsyncSession, sent_emails: list[dict[str, str]]
    ) -> None:
        from tests.conftest import token_from_email

        user = await _verified_user(db)
        pair = await auth_service.login(db, email=user.email, password=PASSWORD)

        await auth_service.forgot_password(db, user.email)
        token = token_from_email(sent_emails[-1])
        await auth_service.reset_password(db, token, "a-brand-new-password")

        # The new credential works.
        await auth_service.login(db, email=user.email, password="a-brand-new-password")

        # The old one does not, and neither does the session that predates it —
        # the point of the reset when recovering from a compromise.
        with pytest.raises(UnauthorizedError):
            await auth_service.login(db, email=user.email, password=PASSWORD)
        with pytest.raises(UnauthorizedError):
            await auth_service.refresh(db, pair.refresh_token)

    async def test_a_reset_token_cannot_be_used_twice(
        self, db: AsyncSession, sent_emails: list[dict[str, str]]
    ) -> None:
        from tests.conftest import token_from_email

        user = await _verified_user(db)
        await auth_service.forgot_password(db, user.email)
        token = token_from_email(sent_emails[-1])
        await auth_service.reset_password(db, token, "a-brand-new-password")

        with pytest.raises(UnauthorizedError):
            await auth_service.reset_password(db, token, "another-password")

    async def test_a_garbage_reset_token_is_refused(self, db: AsyncSession) -> None:
        with pytest.raises(UnauthorizedError):
            await auth_service.reset_password(db, "not-a-real-token", "whatever-pass")

    async def test_forgot_password_is_silent_for_an_unknown_address(
        self, db: AsyncSession, sent_emails: list[dict[str, str]]
    ) -> None:
        """Enumeration safety: no error, and no mail to a stranger."""
        await auth_service.forgot_password(db, "nobody@example.com")
        assert sent_emails == []

    async def test_forgot_password_is_silent_for_an_inactive_account(
        self, db: AsyncSession, sent_emails: list[dict[str, str]]
    ) -> None:
        user = await _verified_user(db)
        await users_service.deactivate(db, user.id)
        sent_emails.clear()

        await auth_service.forgot_password(db, user.email)
        assert sent_emails == []


class TestChangePassword:
    async def test_requires_the_current_password(self, db: AsyncSession) -> None:
        """An access token alone must not be enough to take over an account."""
        user = await _verified_user(db)

        with pytest.raises(IncorrectCurrentPasswordError):
            await auth_service.change_password(
                db, user.id, current_password="wrong", new_password="new-password-here"
            )

    async def test_changing_ends_every_other_session(
        self, db: AsyncSession, sent_emails: list[dict[str, str]]
    ) -> None:
        user = await _verified_user(db)
        old_session = await auth_service.login(db, email=user.email, password=PASSWORD)

        fresh = await auth_service.change_password(
            db,
            user.id,
            current_password=PASSWORD,
            new_password="a-brand-new-password",
        )

        # The fresh pair first, deliberately: presenting the revoked one trips
        # family revocation, which would then kill the fresh pair too. That is
        # correct behaviour, but asserting in the other order would prove only
        # that reuse detection works, not that the caller kept a live session.
        await auth_service.refresh(db, fresh.refresh_token)

        with pytest.raises(UnauthorizedError):
            await auth_service.refresh(db, old_session.refresh_token)

    async def test_changing_notifies_the_owner(
        self, db: AsyncSession, sent_emails: list[dict[str, str]], drain_outbox
    ) -> None:
        """The signal that surfaces an account takeover to the victim.

        Durably staged rather than sent inline, so it cannot be lost in the
        window between writing the new password and telling the owner — the one
        moment where losing it matters most.
        """
        user = await _verified_user(db)
        sent_emails.clear()

        await auth_service.change_password(
            db,
            user.id,
            current_password=PASSWORD,
            new_password="a-brand-new-password",
        )
        await drain_outbox()

        assert len(sent_emails) == 1
        assert sent_emails[0]["to"] == user.email

    async def test_the_notice_survives_a_process_that_never_sent_it(
        self, db: AsyncSession, sent_emails: list[dict[str, str]], drain_outbox
    ) -> None:
        """What moving it to the outbox actually bought.

        Change the password, then do nothing — the stand-in for a crash or a
        deploy immediately afterwards. Sent inline, the notice would be gone and
        the victim would never learn their credential changed. Staged, it is
        still pending and the next worker delivers it.
        """
        user = await _verified_user(db)
        sent_emails.clear()

        await auth_service.change_password(
            db,
            user.id,
            current_password=PASSWORD,
            new_password="a-brand-new-password",
        )
        assert sent_emails == []  # the "crash" happens here

        await drain_outbox()  # a later process picks it up

        assert [e["to"] for e in sent_emails] == [user.email]


class TestRegistrationConflict:
    async def test_a_duplicate_email_is_rejected(self, db: AsyncSession) -> None:
        """Enforced by the unique constraint, not a prior SELECT — the branch
        that closes the check-then-insert race."""
        await auth_service.register(db, email="dupe@example.com", password=PASSWORD)

        with pytest.raises(EmailAlreadyRegisteredError):
            await auth_service.register(db, email="dupe@example.com", password=PASSWORD)

    async def test_the_conflict_is_case_insensitive(self, db: AsyncSession) -> None:
        """Stored lower-cased, so Foo@x.com and foo@x.com are one account."""
        await auth_service.register(db, email="Mixed@Example.com", password=PASSWORD)

        with pytest.raises(EmailAlreadyRegisteredError):
            await auth_service.register(
                db, email="mixed@example.com", password=PASSWORD
            )


class TestLogoutIsIdempotent:
    async def test_logging_out_twice_is_not_an_error(self, db: AsyncSession) -> None:
        user = await _verified_user(db)
        pair = await auth_service.login(db, email=user.email, password=PASSWORD)

        await auth_service.logout(db, pair.refresh_token)
        await auth_service.logout(db, pair.refresh_token)

    async def test_logging_out_a_garbage_token_is_silent(
        self, db: AsyncSession
    ) -> None:
        """Reporting failure would leak whether a token was genuine."""
        await auth_service.logout(db, "not-a-real-token")

    async def test_a_logged_out_token_cannot_refresh(self, db: AsyncSession) -> None:
        user = await _verified_user(db)
        pair = await auth_service.login(db, email=user.email, password=PASSWORD)
        await auth_service.logout(db, pair.refresh_token)

        with pytest.raises(UnauthorizedError):
            await auth_service.refresh(db, pair.refresh_token)


class TestVerificationResend:
    async def test_silent_for_an_unknown_address(
        self, db: AsyncSession, sent_emails: list[dict[str, str]]
    ) -> None:
        await auth_service.resend_verification(db, "nobody@example.com")
        assert sent_emails == []

    async def test_silent_for_an_already_verified_account(
        self, db: AsyncSession, sent_emails: list[dict[str, str]]
    ) -> None:
        """Re-sending to a verified address is not meaningful, and saying so
        would confirm the address is registered."""
        user = await _verified_user(db)
        sent_emails.clear()

        await auth_service.resend_verification(db, user.email)
        assert sent_emails == []

    async def test_sends_for_an_unverified_account(
        self, db: AsyncSession, sent_emails: list[dict[str, str]]
    ) -> None:
        user = await auth_service.register(
            db, email="pending@example.com", password=PASSWORD
        )
        sent_emails.clear()

        await auth_service.resend_verification(db, user.email)
        assert len(sent_emails) == 1

    async def test_verifying_an_unknown_user_is_refused(
        self, db: AsyncSession, sent_emails: list[dict[str, str]]
    ) -> None:
        """A token whose user has since been deleted fails like any other bad
        token, so deletion cannot be detected by watching this response."""
        from tests.conftest import token_from_email

        user = await auth_service.register(
            db, email="ghost@example.com", password=PASSWORD
        )
        token = token_from_email(sent_emails[-1])
        await users_service.soft_delete(db, user.id)

        with pytest.raises(UnauthorizedError):
            await auth_service.verify_email(db, token)


class TestRevokeAll:
    async def test_revoking_ends_every_session(self, db: AsyncSession) -> None:
        user = await _verified_user(db)
        first = await auth_service.login(db, email=user.email, password=PASSWORD)
        second = await auth_service.login(db, email=user.email, password=PASSWORD)

        await auth_service.revoke_all_for_user(db, user.id)

        for pair in (first, second):
            with pytest.raises(UnauthorizedError):
                await auth_service.refresh(db, pair.refresh_token)

    async def test_revoking_for_a_user_with_no_sessions_is_a_no_op(
        self, db: AsyncSession
    ) -> None:
        await auth_service.revoke_all_for_user(db, uuid.uuid4())


class TestDefensiveBranches:
    """The two remaining branches: both are second checks that a first check
    usually reaches first, which is exactly why neither gets exercised."""

    async def test_login_is_refused_for_a_deactivated_account(
        self, db: AsyncSession
    ) -> None:
        """Same error as a wrong password, deliberately: distinguishing them
        would tell an attacker the address is registered."""
        user = await _verified_user(db)
        await users_service.deactivate(db, user.id)

        with pytest.raises(UnauthorizedError):
            await auth_service.login(db, email=user.email, password=PASSWORD)

    async def test_reset_is_refused_when_the_account_went_away(
        self, db: AsyncSession, sent_emails: list[dict[str, str]]
    ) -> None:
        """The token was valid when mailed and the account was deleted before it
        was redeemed. Refused like any bad token, so the response cannot be used
        to detect the deletion.
        """
        from tests.conftest import token_from_email

        user = await _verified_user(db)
        await auth_service.forgot_password(db, user.email)
        token = token_from_email(sent_emails[-1])

        await users_service.soft_delete(db, user.id)

        with pytest.raises(UnauthorizedError):
            await auth_service.reset_password(db, token, "a-brand-new-password")
