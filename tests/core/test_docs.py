"""Tests for the API documentation surfaces and their production gate.

Docs exposure is a security boundary, not a cosmetic setting: the OpenAPI schema
enumerates every endpoint, field, and constraint in the app. These tests build a
fresh app per environment rather than reusing the session-wide one, because the
gate is evaluated at import time — reading it any other way would test the
setting rather than the behaviour.
"""

from __future__ import annotations

import importlib
import os
from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

DOC_ROUTES = ["/scalar", "/docs", "/redoc", "/openapi.json"]


@contextmanager
def app_for(environment: str) -> Iterator[FastAPI]:
    """Yield a freshly imported app built for ``environment``.

    ``app.config`` caches settings and ``app.main`` reads them at import time,
    so both modules are reloaded under the patched environment and restored
    afterwards, leaving the shared app untouched for other tests.
    """
    import app.config
    import app.main

    previous = os.environ.get("ENVIRONMENT")
    os.environ["ENVIRONMENT"] = environment
    try:
        importlib.reload(app.config)
        reloaded = importlib.reload(app.main)
        yield reloaded.app
    finally:
        if previous is None:
            os.environ.pop("ENVIRONMENT", None)
        else:
            os.environ["ENVIRONMENT"] = previous
        importlib.reload(app.config)
        importlib.reload(app.main)


@pytest.mark.parametrize("environment", ["production", "staging"])
@pytest.mark.parametrize("route", DOC_ROUTES)
def test_docs_are_unreachable_outside_development(environment: str, route: str) -> None:
    """No documentation surface may respond outside development.

    Staging counts: it usually holds real-shaped data and is reachable from the
    internet, so exposing the schema there leaks the same information.
    """
    with app_for(environment) as built, TestClient(built) as client:
        assert client.get(route).status_code == 404


@pytest.mark.parametrize("environment", ["production", "staging"])
def test_docs_routes_are_not_registered_outside_development(
    environment: str,
) -> None:
    """The routes must not exist at all, rather than existing and returning 404.

    A registered route that happens to 404 is one edit away from serving again.
    """
    with app_for(environment) as built:
        paths = {getattr(route, "path", None) for route in built.routes}
        assert paths.isdisjoint(DOC_ROUTES)


@pytest.mark.parametrize("route", DOC_ROUTES)
def test_docs_are_available_in_development(route: str) -> None:
    """The gate must not be so tight that local development loses its docs."""
    with app_for("development") as built, TestClient(built) as client:
        assert client.get(route).status_code == 200


def test_health_stays_up_in_production() -> None:
    """Closing the docs must not take the liveness probe with it."""
    with app_for("production") as built, TestClient(built) as client:
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json()["env"] == "production"


class TestScalarPage:
    def test_scalar_renders_and_points_at_the_local_schema(self) -> None:
        with app_for("development") as built, TestClient(built) as client:
            body = client.get("/scalar").text
        assert "/openapi.json" in body

    def test_scalar_does_not_route_requests_through_a_third_party(self) -> None:
        """A configured proxy would send request bodies, headers, and bearer
        tokens typed into the docs through Scalar's servers."""
        with app_for("development") as built, TestClient(built) as client:
            body = client.get("/scalar").text
        assert "proxy.scalar.com" not in body

    def test_scalar_telemetry_is_disabled(self) -> None:
        with app_for("development") as built, TestClient(built) as client:
            body = client.get("/scalar").text
        assert '"telemetry": false' in body.lower().replace("'", '"')
