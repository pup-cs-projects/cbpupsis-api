"""Tests that each app serves its own routers and the composed process serves all.

The composed ``main.py`` is what compose, Bruno, and this suite run, so its URL
paths are the published contract. Each app must serve a subset of it at the same
paths, or a client moving from the composed process to the separate apps would
see different URLs for the same endpoint.
"""

from __future__ import annotations

import inspect
from collections import Counter
from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute
from starlette.routing import Mount

import main
from cbpupsis_api_admin.main import app as admin_app
from cbpupsis_api_admin.routes import MOUNTS as ADMIN_MOUNTS
from cbpupsis_api_faculty.main import app as faculty_app
from cbpupsis_api_faculty.routes import MOUNTS as FACULTY_MOUNTS
from cbpupsis_api_student.main import app as student_app
from cbpupsis_api_student.routes import MOUNTS as STUDENT_MOUNTS
from cbpupsis_shared.application import RouterMount
from cbpupsis_shared.domains.auth.dependencies import require_superadmin_session
from cbpupsis_shared.domains.iam.constants import SUPERADMIN_ACTIONS

APPS: dict[str, FastAPI] = {
    "student": student_app,
    "faculty": faculty_app,
    "admin": admin_app,
}

#: The router prefixes under /api/v1 each app serves.
EXPECTED_PREFIXES: dict[str, set[str]] = {
    # Role sign-ins live under /auth/<role>, so they share the "auth" prefix.
    "student": {"auth", "users", "notifications", "items", "student", "students"},
    "faculty": {"auth", "users", "notifications", "sections"},
    "admin": {"auth", "users", "notifications", "admin", "superadmin", "iam", "audit"},
}


def operations(app: FastAPI) -> list[tuple[str, str]]:
    """Every ``(method, path)`` in the app's schema, duplicates included."""
    return [
        (method, path)
        for path, item in app.openapi()["paths"].items()
        for method in item
    ]


def api_prefixes(app: FastAPI) -> set[str]:
    return {
        path.split("/")[3] for _, path in operations(app) if path.startswith("/api/v1/")
    }


@pytest.mark.parametrize("name", sorted(APPS))
def test_each_app_serves_exactly_its_routers(name: str) -> None:
    assert api_prefixes(APPS[name]) == EXPECTED_PREFIXES[name]


@pytest.mark.parametrize("name", sorted(APPS))
def test_each_app_answers_the_probes(name: str) -> None:
    paths = {path for _, path in operations(APPS[name])}
    assert {"/health", "/ready"} <= paths


def test_the_composed_app_serves_the_union_of_the_apps() -> None:
    """Same operations at the same paths: nothing lost, nothing added."""
    union = {op for app in APPS.values() for op in operations(app)}
    assert set(operations(main.app)) == union


def test_the_composed_app_registers_each_operation_once() -> None:
    """Every app lists the common routers; the composer must include them once."""
    counts = Counter(
        op["operationId"]
        for item in main.app.openapi()["paths"].values()
        for op in item.values()
    )
    assert not [op_id for op_id, n in counts.items() if n > 1]


def test_no_app_mounts_the_service_bypassing_admin_panel() -> None:
    """Even a development panel must not bypass MFA, overrides, and auditing."""

    def has_panel(app: FastAPI) -> bool:
        return any(isinstance(r, Mount) and r.path == "/admin" for r in app.routes)

    assert not has_panel(admin_app)
    assert not has_panel(main.app)
    assert not has_panel(student_app)
    assert not has_panel(faculty_app)


def _calls(dependant: Dependant) -> Iterator[object]:
    """Every callable in a route's dependency tree, depth first."""
    for sub in dependant.dependencies:
        yield sub.call
        yield from _calls(sub)


def _superadmin_only(call: object) -> bool:
    """Whether a dependency admits nobody but a Superadmin.

    ``require_permission`` and ``require_any_permission`` return a closure over
    the actions; it is Superadmin-only when every action it names is one only
    Superadmins hold (``SUPERADMIN_ACTIONS``).
    """
    if call is require_superadmin_session:
        return True
    if not callable(call) or getattr(call, "__name__", "") != "checker":
        return False
    names = inspect.getclosurevars(call).nonlocals
    actions = set(names.get("required") or names.get("accepted") or ())
    return bool(actions) and actions <= SUPERADMIN_ACTIONS


def superadmin_routes(mounts: tuple[RouterMount, ...]) -> list[str]:
    """``METHOD /prefix/path`` for each Superadmin-only route in ``mounts``."""
    found = []
    for mount in mounts:
        router_wide = any(
            _superadmin_only(d.dependency) for d in mount.router.dependencies
        )
        for route in mount.router.routes:
            if not isinstance(route, APIRoute):
                continue
            if router_wide or any(_superadmin_only(c) for c in _calls(route.dependant)):
                found += [
                    f"{m} {mount.prefix}{route.path}" for m in sorted(route.methods)
                ]
    return found


@pytest.mark.parametrize(
    ("name", "mounts"), [("student", STUDENT_MOUNTS), ("faculty", FACULTY_MOUNTS)]
)
def test_student_and_faculty_apps_serve_no_superadmin_route(
    name: str, mounts: tuple[RouterMount, ...]
) -> None:
    """Endpoints are separated by role: Superadmin work lives on the admin app.

    A Superadmin token is valid on every app (they share the signing key), so a
    management route mounted on the student app would be a second door into
    admin operations, and its docs would advertise it to the student portal.
    """
    assert superadmin_routes(mounts) == [], name


def test_the_guard_finds_the_admin_apps_superadmin_routes() -> None:
    """Proves the detector works: the admin app does serve these, by design."""
    found = superadmin_routes(ADMIN_MOUNTS)
    for expected in (
        "POST /users",
        "GET /users",
        "POST /users/{user_id}/deactivate",
        "POST /users/{user_id}/reissue-temporary-password",
        "POST /users/import",
    ):
        assert expected in found, expected
