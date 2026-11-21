"""US-105: every emailed link opens the portal of the account's own role.

Each role has its own frontend (student, faculty, Admin, Superadmin), so a
reset link, a verification link, or a temporary-password email that pointed at
one shared URL sent three of the four roles to an app that cannot sign them in.
No acceptance criterion of US-105 names the portal yet; these tests encode the
fix and the configuration contract (per-role keys, ``FRONTEND_BASE_URL`` as the
fallback, the dev ports as defaults).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_core.config import DEV_FRONTEND_URLS, Settings, settings
from cbpupsis_database.models.iam import Group
from cbpupsis_shared.domains.auth import service as auth_service
from cbpupsis_shared.domains.auth.security import hash_password
from cbpupsis_shared.domains.auth.service.accounts import (
    stage_temporary_password_email,
)
from cbpupsis_shared.domains.iam import service as iam_service
from cbpupsis_shared.domains.iam.constants import (
    ADMIN_GROUP,
    FACULTY_GROUP,
    STUDENT_GROUP,
    SUPERADMIN_GROUP,
)
from cbpupsis_shared.domains.users import service as users_service

SECRET = "x" * 48
ROLES = ("student", "faculty", "admin", "superadmin")
ROLE_GROUP = {
    "student": STUDENT_GROUP,
    "faculty": FACULTY_GROUP,
    "admin": ADMIN_GROUP,
    "superadmin": SUPERADMIN_GROUP,
}
#: Distinct hosts, so a link that opened the wrong portal cannot pass by
#: sharing a port with the right one.
PORTALS = {role: f"https://{role}.portal.test" for role in ROLES}
FALLBACK = "https://fallback.portal.test"
FRONTEND_KEYS = (
    "FRONTEND_BASE_URL",
    "FRONTEND_BASE_URL_STUDENT",
    "FRONTEND_BASE_URL_FACULTY",
    "FRONTEND_BASE_URL_ADMIN",
    "FRONTEND_BASE_URL_SUPERADMIN",
)


@pytest.fixture
def clean_env(monkeypatch) -> None:
    """No frontend key from the shell, so each test states its own config."""
    for key in FRONTEND_KEYS:
        monkeypatch.delenv(key, raising=False)


def _settings(**values: object) -> Settings:
    """Build settings from ``values`` alone, ignoring any local env file."""
    return Settings(_env_file=None, jwt_secret=SECRET, **values)


@pytest.mark.usefixtures("clean_env")
class TestPortalSettings:
    """How a role's portal URL is chosen from configuration."""

    def test_defaults_are_the_dev_ports(self) -> None:
        """Breaks if a fresh clone mails faculty, Admins, or Superadmins to 3000."""
        configured = _settings()
        assert {role: configured.frontend_url_for(role) for role in ROLES} == {
            "student": "http://localhost:3000",
            "faculty": "http://localhost:3001",
            "admin": "http://localhost:3002",
            "superadmin": "http://localhost:3003",
        }
        assert {
            role: configured.frontend_url_for(role) for role in ROLES
        } == DEV_FRONTEND_URLS

    def test_fallback_default_is_the_student_portal(self) -> None:
        assert _settings().frontend_base_url == "http://localhost:3000"

    @pytest.mark.parametrize("role", ROLES)
    def test_role_key_wins(self, role: str) -> None:
        configured = _settings(
            frontend_base_url=FALLBACK, **{f"frontend_base_url_{role}": PORTALS[role]}
        )
        assert configured.frontend_url_for(role) == PORTALS[role]

    @pytest.mark.parametrize("role", ROLES)
    def test_unset_role_falls_back_to_frontend_base_url(self, role: str) -> None:
        """An env file from before the per-role keys keeps its one URL.

        Breaks if a deployment that set only FRONTEND_BASE_URL starts mailing
        localhost dev ports.
        """
        configured = _settings(frontend_base_url=FALLBACK)
        assert configured.frontend_url_for(role) == FALLBACK

    def test_fallback_is_read_from_the_environment(self, monkeypatch) -> None:
        monkeypatch.setenv("FRONTEND_BASE_URL", FALLBACK)
        monkeypatch.setenv("FRONTEND_BASE_URL_FACULTY", PORTALS["faculty"])
        configured = _settings()
        assert configured.frontend_url_for("faculty") == PORTALS["faculty"]
        assert configured.frontend_url_for("admin") == FALLBACK

    def test_empty_role_key_counts_as_unset(self) -> None:
        configured = _settings(frontend_base_url=FALLBACK, frontend_base_url_admin="")
        assert configured.frontend_url_for("admin") == FALLBACK

    @pytest.mark.parametrize("role", [None, "", "members"])
    def test_no_role_uses_frontend_base_url(self, role: str | None) -> None:
        """An account in no role group has no portal of its own."""
        assert _settings().frontend_url_for(role) == "http://localhost:3000"
        assert _settings(frontend_base_url=FALLBACK).frontend_url_for(role) == FALLBACK


@pytest.fixture
def portals(monkeypatch) -> dict[str, str]:
    """Point every role at its own host on the live settings object."""
    for role in ROLES:
        monkeypatch.setattr(settings, f"frontend_base_url_{role}", PORTALS[role])
    monkeypatch.setattr(settings, "frontend_base_url", FALLBACK)
    return PORTALS


async def _join(db: AsyncSession, user_id: uuid.UUID, group_name: str) -> None:
    group_id = await db.scalar(select(Group.id).where(Group.name == group_name))
    if group_id is None:
        group_id = (await iam_service.create_group(db, name=group_name)).id
    await iam_service.add_user_to_group(db, user_id, group_id)


async def _account(db: AsyncSession, make_user, role: str | None, email: str):
    """An active account in ``role``'s group (none for ``None``)."""
    user = await make_user(email)
    if role is not None:
        await _join(db, user.id, ROLE_GROUP[role])
    return user


def _only_mail_to(sent_emails: list[dict[str, str]], email: str) -> str:
    [message] = [m for m in sent_emails if m["to"] == email]
    return message["body"]


@pytest.mark.usefixtures("clean_env", "portals")
class TestEmailedLinks:
    """Each email type, delivered by the outbox, for each role."""

    @pytest.mark.parametrize("role", ROLES)
    async def test_reset_link_opens_the_role_portal(
        self, db, make_user, sent_emails, drain_outbox, role
    ) -> None:
        """Breaks if a reset link sends this role to another role's portal."""
        email = f"reset-{role}@example.com"
        await _account(db, make_user, role, email)
        await drain_outbox()
        sent_emails.clear()

        await auth_service.forgot_password(db, email)
        await drain_outbox()

        body = _only_mail_to(sent_emails, email)
        assert f"{PORTALS[role]}/reset-password?token=" in body

    @pytest.mark.parametrize("role", ROLES)
    async def test_verification_link_opens_the_role_portal(
        self, db, make_user, sent_emails, drain_outbox, role
    ) -> None:
        """The role is read at delivery, so a group joined after the link was
        queued still decides the portal."""
        email = f"verify-{role}@example.com"
        await _account(db, make_user, role, email)

        await drain_outbox()

        body = _only_mail_to(sent_emails, email)
        assert f"{PORTALS[role]}/verify-email?token=" in body

    @pytest.mark.parametrize("role", ROLES)
    async def test_temporary_password_email_opens_the_role_portal(
        self, db, make_user, sent_emails, drain_outbox, role
    ) -> None:
        email = f"welcome-{role}@example.com"
        user = await _account(db, make_user, role, email)
        await drain_outbox()
        sent_emails.clear()
        raw = "Temp-Password-123!"
        expires_at = datetime.now(UTC) + timedelta(days=1)
        await users_service.stage_temporary_password(
            db, user.id, hash_password(raw), expires_at
        )
        stage_temporary_password_email(db, user.id, raw, expires_at)
        await db.commit()

        await drain_outbox()

        body = _only_mail_to(sent_emails, email)
        assert f"Sign in at {PORTALS[role]} with this temporary" in body

    async def test_account_in_no_role_group_gets_the_fallback(
        self, db, make_user, sent_emails, drain_outbox
    ) -> None:
        email = "no-role@example.com"
        await _account(db, make_user, None, email)

        await drain_outbox()

        body = _only_mail_to(sent_emails, email)
        assert f"{FALLBACK}/verify-email?token=" in body

    async def test_role_keys_unset_fall_back_to_frontend_base_url(
        self, monkeypatch, db, make_user, sent_emails, drain_outbox
    ) -> None:
        """Breaks if an unset role key mails a link with no host, or None."""
        monkeypatch.setattr(
            "cbpupsis_core.emails.templates.settings",
            _settings(frontend_base_url=FALLBACK),
        )
        email = "fallback-faculty@example.com"
        await _account(db, make_user, "faculty", email)
        await drain_outbox()
        sent_emails.clear()

        await auth_service.forgot_password(db, email)
        await drain_outbox()

        body = _only_mail_to(sent_emails, email)
        assert f"{FALLBACK}/reset-password?token=" in body


class TestPortalRole:
    """Which role decides the portal for an account in several groups."""

    async def test_most_privileged_group_wins(self, db, make_user) -> None:
        user = await make_user("several@example.com")
        await _join(db, user.id, STUDENT_GROUP)
        await _join(db, user.id, FACULTY_GROUP)

        assert await auth_service.portal_role(db, user.id) == "faculty"

    async def test_superadmin_outranks_other_role_groups(self, db, make_user) -> None:
        user = await make_user("top@example.com")
        await _join(db, user.id, STUDENT_GROUP)
        await _join(db, user.id, SUPERADMIN_GROUP)

        assert await auth_service.portal_role(db, user.id) == "superadmin"

    async def test_no_role_group_is_none(self, db, make_user) -> None:
        user = await make_user("plain@example.com")
        await _join(db, user.id, "Members")

        assert await auth_service.portal_role(db, user.id) is None


#: One Superadmin-created account per role that POST /users can create.
CREATED_ROWS: dict[str, dict[str, object]] = {
    "student": {
        "first_name": "Ana",
        "surname": "Reyes",
        "email": "ana.portal@example.com",
        "role": "student",
        "institutional_id": "2024-00001-MN-0",
        "birthdate": "2005-02-14",
        "program": "bscs",
        "year_level": 1,
    },
    "faculty": {
        "first_name": "Ben",
        "surname": "Cruz",
        "email": "ben.portal@example.com",
        "role": "faculty",
        "institutional_id": "2015-00042-MN-1",
        "department": "Computer Science",
    },
    "admin": {
        "first_name": "Cora",
        "surname": "Diaz",
        "email": "cora.portal@example.com",
        "role": "admin_registrar",
    },
}


@pytest.mark.usefixtures("portals")
class TestCreatedAccounts:
    """The Superadmin-created account path, end to end over HTTP."""

    @pytest.mark.parametrize("role", ["student", "faculty", "admin"])
    async def test_welcome_email_opens_the_created_role_portal(
        self,
        client: AsyncClient,
        role_groups,
        active_program,
        superadmin_headers,
        sent_emails,
        drain_outbox,
        role: str,
    ) -> None:
        row = CREATED_ROWS[role]
        response = await client.post(
            "/api/v1/users", headers=superadmin_headers, json=row
        )
        assert response.status_code == 201, response.text

        await drain_outbox()

        body = _only_mail_to(sent_emails, str(row["email"]))
        assert f"Sign in at {PORTALS[role]} with this temporary" in body
