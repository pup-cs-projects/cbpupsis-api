"""Tests for the SQLAdmin panel's authentication gate and its data exposure.

The panel edits rows directly, bypassing every service-layer rule, so its login
is the only thing standing between a session cookie and unrestricted write
access to the database. Two properties carry almost all of that weight and are
both invisible when broken: that ``ManageIAM`` is genuinely required, and that
``authenticate`` re-reads permissions from the database rather than trusting the
cookie it was handed.

The backend opens its own ``AsyncSessionLocal`` because SQLAdmin mounts a
separate Starlette app and the ``get_db`` request dependency does not reach it.
Tests therefore patch that name **in ``cbpupsis_api_admin.admin``**, where it was
imported — patching ``cbpupsis_database.session`` would leave the module-level
binding untouched.
"""

from __future__ import annotations

import importlib
import os
import uuid
from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqladmin import Admin
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import RedirectResponse

from cbpupsis_api_admin.admin import SESSION_USER_ID, AdminAuth, UserAdmin
from cbpupsis_database.models.iam import Group
from cbpupsis_database.session import engine
from cbpupsis_shared.domains.auth import service as auth_service
from cbpupsis_shared.domains.iam import service as iam_service
from cbpupsis_shared.domains.iam.constants import MANAGE_IAM
from cbpupsis_shared.domains.users import service as users_service

PASSWORD = "correct-horse-battery-staple"

ADMIN_ROUTES = ["/admin", "/admin/login"]


class _Session:
    """Wrap the test session so the backend's `async with` does not close it."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def __aenter__(self) -> AsyncSession:
        return self._session

    async def __aexit__(self, *exc_info: object) -> None:
        return None


class _Request:
    """Minimal stand-in for a Starlette request the backend can drive.

    Carries the three things the backend touches: the submitted form, a mutable
    session mapping, and ``url_for`` — which must exist because ``authenticate``
    builds a redirect when it refuses, and a missing attribute would fail the
    test for the wrong reason.
    """

    def __init__(self, **form: str) -> None:
        self._form = form
        self.session: dict[str, str] = {}

    async def form(self) -> dict[str, str]:
        return self._form

    def url_for(self, name: str, **kwargs: object) -> str:
        return f"http://test/{name}"


@pytest.fixture
def backend(db: AsyncSession, monkeypatch) -> AdminAuth:
    """Return an ``AdminAuth`` whose own sessions hit the test database."""
    monkeypatch.setattr(
        "cbpupsis_api_admin.admin.AsyncSessionLocal", lambda: _Session(db)
    )
    return AdminAuth(secret_key="x" * 32)


async def _make_admin(db: AsyncSession, grant, email: str = "admin@example.com"):
    """Register a verified user holding ``ManageIAM``."""
    user = await auth_service.register(db, email=email, password=PASSWORD)
    await users_service.mark_email_verified(db, user.id)
    await grant(user.id, MANAGE_IAM)
    return user


async def _group_named(db: AsyncSession, name: str) -> Group:
    """Return the group the ``grant`` fixture created, by name."""
    result = await db.execute(select(Group).where(Group.name == name))
    return result.scalar_one()


def _reload_app_modules() -> None:
    """Rebuild the app from a fresh settings object, in dependency order.

    ``create_app`` reads the gate from the settings its module bound at import,
    so the factory module is reloaded after the config and before ``main``.
    """
    import cbpupsis_core.config
    import cbpupsis_shared.application
    import main

    importlib.reload(cbpupsis_core.config)
    importlib.reload(cbpupsis_shared.application)
    importlib.reload(main)


@contextmanager
def app_for(environment: str) -> Iterator[FastAPI]:
    """Yield a freshly imported app built for ``environment``.

    Mirrors ``tests/core/test_docs.py``: the panel is mounted at import time
    inside the ``docs_enabled`` gate, so both modules are reloaded under the
    patched environment and restored afterwards, leaving the shared app
    untouched for other tests.
    """
    import main

    previous = os.environ.get("ENVIRONMENT")
    os.environ["ENVIRONMENT"] = environment
    try:
        _reload_app_modules()
        yield main.app
    finally:
        if previous is None:
            os.environ.pop("ENVIRONMENT", None)
        else:
            os.environ["ENVIRONMENT"] = previous
        _reload_app_modules()


class TestAdminLogin:
    async def test_admin_with_manage_iam_logs_in_and_gets_a_session(
        self, db: AsyncSession, backend: AdminAuth, grant
    ) -> None:
        """If this fails nobody can reach the panel at all."""
        user = await _make_admin(db, grant)
        request = _Request(username=user.email, password=PASSWORD)

        assert await backend.login(request) is True
        assert request.session[SESSION_USER_ID] == str(user.id)

    async def test_user_without_manage_iam_is_refused_with_a_correct_password(
        self, db: AsyncSession, backend: AdminAuth
    ) -> None:
        """The single most important test here: valid credentials alone must not
        open the panel. If this fails, every registered account can edit any row
        in the database directly, bypassing every service-layer rule."""
        user = await auth_service.register(
            db, email="plain@example.com", password=PASSWORD
        )
        await users_service.mark_email_verified(db, user.id)
        request = _Request(username=user.email, password=PASSWORD)

        assert await backend.login(request) is False
        assert SESSION_USER_ID not in request.session
        assert request.session == {}

    async def test_wrong_password_is_refused(
        self, db: AsyncSession, backend: AdminAuth, grant
    ) -> None:
        """Holding ManageIAM must not substitute for proving identity."""
        user = await _make_admin(db, grant)
        request = _Request(username=user.email, password="not-the-password")

        assert await backend.login(request) is False
        assert request.session == {}

    async def test_unknown_account_is_refused(self, backend: AdminAuth) -> None:
        """A missing account must not fall through to a session."""
        request = _Request(username="nobody@example.com", password=PASSWORD)

        assert await backend.login(request) is False
        assert request.session == {}

    @pytest.mark.parametrize("field", ["username", "password"])
    async def test_blank_credentials_are_refused(
        self, db: AsyncSession, backend: AdminAuth, grant, field: str
    ) -> None:
        """An empty submission must be rejected outright, not verified against
        a stored hash."""
        user = await _make_admin(db, grant)
        form = {"username": user.email, "password": PASSWORD, field: ""}
        request = _Request(**form)

        assert await backend.login(request) is False
        assert request.session == {}

    async def test_deactivated_admin_is_refused(
        self, db: AsyncSession, backend: AdminAuth, grant
    ) -> None:
        """Deactivation must lock the panel even while the grant survives —
        suspending an account is otherwise only half done."""
        user = await _make_admin(db, grant)
        await users_service.deactivate(db, user.id)
        request = _Request(username=user.email, password=PASSWORD)

        assert await backend.login(request) is False
        assert request.session == {}

    async def test_soft_deleted_admin_is_refused(
        self, db: AsyncSession, backend: AdminAuth, grant
    ) -> None:
        """A deleted account keeps its row (items and grants reference the id),
        so the panel has to refuse it on its own rather than assume it is gone."""
        user = await _make_admin(db, grant)
        email = user.email
        await users_service.soft_delete(db, user.id)
        request = _Request(username=email, password=PASSWORD)

        assert await backend.login(request) is False
        assert request.session == {}

    async def test_logout_clears_the_session(self, backend: AdminAuth) -> None:
        """A shared machine must not leave the next person logged in."""
        request = _Request()
        request.session[SESSION_USER_ID] = str(uuid.uuid4())

        assert await backend.logout(request) is True
        assert request.session == {}


class TestAdminAuthenticate:
    async def test_live_session_of_an_admin_is_allowed(
        self, db: AsyncSession, backend: AdminAuth, grant
    ) -> None:
        """The per-request check must accept a session login just issued."""
        user = await _make_admin(db, grant)
        request = _Request(username=user.email, password=PASSWORD)
        assert await backend.login(request) is True

        assert await backend.authenticate(request) is True

    async def test_revoking_the_group_refuses_an_already_live_session(
        self, db: AsyncSession, backend: AdminAuth, grant
    ) -> None:
        """Revocation must be immediate. If ``authenticate`` trusted the cookie
        instead of re-reading permissions, a removed administrator would keep
        full write access until the cookie expired."""
        user = await _make_admin(db, grant)
        request = _Request(username=user.email, password=PASSWORD)
        assert await backend.login(request) is True
        assert await backend.authenticate(request) is True

        group = await _group_named(db, f"group-{MANAGE_IAM}")
        await iam_service.remove_user_from_group(db, user.id, group.id)

        assert MANAGE_IAM not in await iam_service.get_effective_permissions(
            db, user.id
        )
        # `is not True` on purpose: a RedirectResponse is truthy, so a loose
        # assertion would pass while the panel stood wide open.
        assert await backend.authenticate(request) is not True

    async def test_deactivating_the_user_refuses_an_already_live_session(
        self, db: AsyncSession, backend: AdminAuth, grant
    ) -> None:
        """Deactivation must take effect on the next request, not at expiry."""
        user = await _make_admin(db, grant)
        request = _Request(username=user.email, password=PASSWORD)
        assert await backend.login(request) is True

        await users_service.deactivate(db, user.id)

        assert await backend.authenticate(request) is not True

    async def test_empty_session_is_refused(self, backend: AdminAuth) -> None:
        """An unauthenticated visitor must be sent to the login page."""
        request = _Request()

        result = await backend.authenticate(request)
        assert result is not True
        assert isinstance(result, RedirectResponse)

    async def test_unparseable_session_id_is_refused_and_cleared(
        self, backend: AdminAuth
    ) -> None:
        """A tampered cookie must not raise a 500 that leaks a traceback."""
        request = _Request()
        request.session[SESSION_USER_ID] = "not-a-uuid"

        assert await backend.authenticate(request) is not True
        assert request.session == {}

    async def test_session_naming_an_unknown_user_is_refused(
        self, backend: AdminAuth
    ) -> None:
        """A well-formed id for a nonexistent user must not authenticate."""
        request = _Request()
        request.session[SESSION_USER_ID] = str(uuid.uuid4())

        assert await backend.authenticate(request) is not True
        assert request.session == {}


class TestAdminPanelIsDevelopmentOnly:
    @pytest.mark.parametrize("environment", ["production", "staging"])
    def test_admin_routes_are_not_registered_outside_development(
        self, environment: str
    ) -> None:
        """The panel must not exist at all outside development. A mounted panel
        that merely refuses logins is one misconfigured grant away from direct
        write access to production data."""
        with app_for(environment) as built:
            paths = [getattr(route, "path", "") for route in built.routes]
            assert not any(path.startswith("/admin") for path in paths)

    @pytest.mark.parametrize("environment", ["production", "staging"])
    @pytest.mark.parametrize("route", ADMIN_ROUTES)
    def test_admin_does_not_respond_outside_development(
        self, environment: str, route: str
    ) -> None:
        """Nothing to probe: no login page, no fingerprint of the panel."""
        with app_for(environment) as built, TestClient(built) as client:
            assert client.get(route).status_code == 404

    def test_admin_is_mounted_in_development(self) -> None:
        """The gate must not be so tight that local development loses the
        panel entirely."""
        with app_for("development") as built:
            paths = [getattr(route, "path", "") for route in built.routes]
            assert any(path.startswith("/admin") for path in paths)


class TestAdminDoesNotExposeCredentials:
    """Regression cover for a real vulnerability.

    ``column_list`` constrains the index page only. Without explicit excludes
    the detail view and the *editable form* both rendered the argon2 digest, so
    the hash was one click from the user list and one POST from being rewritten.
    """

    @pytest.fixture
    def user_view(self) -> UserAdmin:
        from starlette.applications import Starlette

        admin = Admin(Starlette(), engine)
        admin.add_view(UserAdmin)
        return admin.views[0]

    @pytest.mark.parametrize(
        "names_attr",
        ["_details_prop_names", "_form_prop_names", "_export_prop_names"],
    )
    def test_password_hash_is_absent_from_every_rendered_surface(
        self, user_view: UserAdmin, names_attr: str
    ) -> None:
        """If any of these regains ``password_hash``, the panel serves or
        rewrites the stored credential digest."""
        names = getattr(user_view, names_attr)
        assert "password_hash" not in names, (
            f"{names_attr} exposes password_hash: {names}"
        )

    def test_the_index_listing_also_omits_password_hash(
        self, user_view: UserAdmin
    ) -> None:
        """The narrowest surface, and the only one ``column_list`` covers."""
        assert "password_hash" not in user_view._list_prop_names

    def test_the_view_still_shows_the_columns_it_is_meant_to(
        self, user_view: UserAdmin
    ) -> None:
        """Guards the exclusions above from being satisfied by a view that
        renders nothing at all."""
        assert "email" in user_view._details_prop_names
        assert "email" in user_view._form_prop_names
