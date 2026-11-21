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


@pytest.mark.parametrize("app", [student_app, main.app])
def test_student_records_have_one_public_route_per_operation(app: FastAPI) -> None:
    paths = {
        (method, path)
        for method, path in operations(app)
        if path.startswith(("/api/v1/student/", "/api/v1/students/"))
    }
    assert paths == {
        ("get", "/api/v1/students/me/grades"),
        ("get", "/api/v1/students/me/transcript"),
        ("get", "/api/v1/students/me/transcript/export"),
        ("get", "/api/v1/students/{student_number}/grades"),
        ("get", "/api/v1/student/profile"),
        ("patch", "/api/v1/student/profile"),
        ("post", "/api/v1/student/profile/avatar"),
        ("get", "/api/v1/student/profile/avatar/{avatar_id}"),
        ("get", "/api/v1/student/profile/emergency-contacts"),
        ("post", "/api/v1/student/profile/emergency-contacts"),
        ("patch", "/api/v1/student/profile/emergency-contacts/{contact_id}"),
        ("delete", "/api/v1/student/profile/emergency-contacts/{contact_id}"),
        ("get", "/api/v1/student/dashboard"),
        ("get", "/api/v1/student/me/dashboard"),
        ("get", "/api/v1/student/{student_id}/dashboard"),
        ("get", "/api/v1/students/me/schedule"),
        ("get", "/api/v1/students/me/schedule/export"),
        ("get", "/api/v1/students/{student_number}/schedule"),
        ("get", "/api/v1/students/me/schedule/google-calendar"),
        ("delete", "/api/v1/students/me/schedule/google-calendar"),
        ("post", "/api/v1/students/me/schedule/google-calendar/authorization"),
        ("post", "/api/v1/students/me/schedule/google-calendar/callback"),
        ("post", "/api/v1/students/me/schedule/google-calendar/sync"),
    }


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


@pytest.mark.parametrize("app", [student_app, main.app], ids=["student", "composed"])
def test_schedule_self_routes_and_removed_alias_contract(app: FastAPI) -> None:
    paths = app.openapi()["paths"]
    for suffix in ("", "/export"):
        canonical = paths[f"/api/v1/students/me/schedule{suffix}"]["get"]
        assert f"/api/v1/student/schedule{suffix}" not in paths
        assert not canonical.get("deprecated", False)
        assert canonical["security"] == [{"HTTPBearer": []}]
    assert "/api/v1/students/{student_number}/schedule" in paths


@pytest.mark.parametrize("app", [student_app, main.app], ids=["student", "composed"])
def test_schedule_openapi_documents_selectors_filters_and_downloads(
    app: FastAPI,
) -> None:
    schema = app.openapi()
    paths = schema["paths"]
    base = "/api/v1/students/me/schedule"
    for path in (base, f"{base}/export", "/api/v1/students/{student_number}/schedule"):
        operation = paths[path]["get"]
        assert len(operation["description"]) > 600
        assert "ReadSchedule" in operation["description"]
        assert {"200", "401", "403", "404", "422", "503"} <= operation[
            "responses"
        ].keys()
        parameters = {
            parameter["name"]: parameter for parameter in operation["parameters"]
        }
        for name in ("year", "semester"):
            assert "both" in parameters[name]["description"]
            assert parameters[name]["schema"]["examples"]
        assert "private, no-store" in operation["description"]
    read_parameters = {p["name"]: p for p in paths[base]["get"]["parameters"]}
    assert "faculty" in read_parameters["q"]["description"]
    assert "notEnrolled" in paths[base]["get"]["description"]
    export = paths[f"{base}/export"]["get"]
    assert "q" not in {p["name"] for p in export["parameters"]}
    for media_type in ("application/pdf", "text/calendar"):
        assert export["responses"]["200"]["content"][media_type]["schema"] == {
            "type": "string",
            "format": "binary",
        }
    assert "Content-Disposition" in export["responses"]["200"]["headers"]
    assert (
        export["responses"]["503"]["headers"]["Retry-After"]["schema"]["example"] == 30
    )
    for model in ("ScheduleRead", "ScheduleCourse", "ScheduleMeeting", "ScheduleTerm"):
        assert all(
            field.get("description")
            for field in schema["components"]["schemas"][model]["properties"].values()
        )


@pytest.mark.parametrize("app", [student_app, main.app], ids=["student", "composed"])
def test_schedule_openapi_exposes_snake_case_response_properties(app: FastAPI) -> None:
    schemas = app.openapi()["components"]["schemas"]
    expected = {
        "ScheduleRead": {
            "student_name",
            "student_number",
            "term",
            "courses",
            "total_credits",
            "total_lecture_hours",
            "total_lab_hours",
            "state",
            "message",
        },
        "ScheduleTerm": {"id", "year", "semester", "start_date", "end_date"},
        "ScheduleCourse": {
            "code",
            "title",
            "section",
            "section_id",
            "room",
            "faculty_name",
            "units",
            "lecture_hours",
            "lab_hours",
            "status",
            "meetings",
        },
        "ScheduleMeeting": {
            "id",
            "day_of_week",
            "start_time",
            "end_time",
            "room",
            "type",
        },
    }
    for model, properties in expected.items():
        assert set(schemas[model]["properties"]) == properties
    assert schemas["ScheduleRead"]["properties"]["state"]["enum"] == [
        "ready",
        "notEnrolled",
        "noMatches",
        "termUnavailable",
    ]


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
