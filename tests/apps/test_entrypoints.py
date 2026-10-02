"""Tests that each app serves its own routers and the composed process serves all.

The composed ``main.py`` is what compose, Bruno, and this suite run, so its URL
paths are the published contract. Each app must serve a subset of it at the same
paths, or a client moving from the composed process to the separate apps would
see different URLs for the same endpoint.
"""

from __future__ import annotations

from collections import Counter

import pytest
from fastapi import FastAPI
from starlette.routing import Mount

import main
from cbpupsis_api_admin.main import app as admin_app
from cbpupsis_api_faculty.main import app as faculty_app
from cbpupsis_api_student.main import app as student_app

APPS: dict[str, FastAPI] = {
    "student": student_app,
    "faculty": faculty_app,
    "admin": admin_app,
}

#: The router prefixes under /api/v1 each app serves.
EXPECTED_PREFIXES: dict[str, set[str]] = {
    "student": {"auth", "student-auth", "users", "notifications", "items"},
    "faculty": {"auth", "users", "notifications"},
    "admin": {"auth", "users", "notifications", "admin", "iam", "audit"},
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


def test_only_the_admin_app_and_the_composer_mount_the_admin_panel() -> None:
    """The panel is admin tooling; the student and faculty apps must not carry it."""

    def has_panel(app: FastAPI) -> bool:
        return any(isinstance(r, Mount) and r.path == "/admin" for r in app.routes)

    assert has_panel(admin_app)
    assert has_panel(main.app)
    assert not has_panel(student_app)
    assert not has_panel(faculty_app)
