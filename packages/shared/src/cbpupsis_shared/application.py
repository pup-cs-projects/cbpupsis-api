"""The one way a FastAPI application is built in this workspace.

Every entry point (each app's ``main.py`` and the composed root ``main.py``)
calls :func:`create_app` with the routers it serves. Middleware, error handling,
the docs gate, and the probes are wired here once, so the three apps and the
composed process cannot drift apart on any of them.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass

from fastapi import APIRouter, FastAPI, Request, Response, status
from fastapi.responses import HTMLResponse
from scalar_fastapi import get_scalar_api_reference
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from cbpupsis_core.config import settings
from cbpupsis_core.events import event_bus
from cbpupsis_core.exceptions import register_exception_handlers
from cbpupsis_core.logging import configure_logging
from cbpupsis_core.middleware import client_ip, limiter, register_middleware
from cbpupsis_database.session import AsyncSessionLocal, engine
from cbpupsis_shared.domains.auth import service as auth_service

logger = logging.getLogger(__name__)

#: Every domain router is mounted under this prefix. A future incompatible
#: version gets its own prefix and its own mounts, leaving v1 frozen.
API_V1_PREFIX = "/api/v1"

#: Readiness must fail faster than the probe interval, or a hung check is
#: indistinguishable from a failed one while holding a worker for the duration.
READINESS_TIMEOUT_SECONDS = 2.0


@dataclass(frozen=True, eq=False)
class RouterMount:
    """One domain router and the path it is served at under ``/api/v1``.

    Compared by identity: each mount is declared once as a module constant, so
    two apps listing ``AUTH`` hold the same object, and that is the duplicate
    :func:`create_app` drops.
    """

    router: APIRouter
    prefix: str
    tag: str


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown hooks.

    Draining the event bus first gives handlers scheduled in the background a
    chance to finish before their database connections are taken away. It is
    best-effort by nature — a crash or a SIGKILL reaches neither line — which is
    the honest limit of in-process dispatch, and the argument for a broker once
    an event must not be lost. See ``cbpupsis_core.events``.

    Disposing the engine on shutdown then closes pooled connections cleanly,
    which matters on Neon where abandoned connections count against the pool
    limit.
    """
    yield
    await event_bus.drain()
    await engine.dispose()


def create_app(
    *,
    title: str,
    mounts: Iterable[RouterMount],
    development_tools: Iterable[Callable[[FastAPI], None]] = (),
) -> FastAPI:
    """Build an application serving ``mounts``.

    A mount listed more than once is included once, so the composed process can
    pass the union of every app's mounts without registering a route twice.
    ``development_tools`` are mounted only when the docs gate is open, for the
    same reason the docs are: the admin panel edits rows directly, bypassing
    every service-layer rule, so outside development it must not exist at all.
    """
    # Before anything else: a logger used before configuration silently keeps
    # the default handler and ignores everything set up here.
    configure_logging()

    app = FastAPI(
        title=title,
        debug=settings.debug,
        lifespan=lifespan,
        # Every docs surface hangs off settings.docs_enabled, so production
        # exposes none of them. openapi_url must be gated too: without it,
        # /openapi.json still serves the full schema even with the UIs switched
        # off — the schema IS the sensitive part, the UIs merely render it.
        docs_url="/docs" if settings.docs_enabled else None,
        redoc_url="/redoc" if settings.docs_enabled else None,
        openapi_url="/openapi.json" if settings.docs_enabled else None,
    )

    # slowapi reads the limiter off app.state, so the decorators in the routers
    # can find it at request time. Assigning it is not optional — without it
    # every limited endpoint raises at runtime rather than at startup.
    app.state.limiter = limiter
    app.state.rate_limit_observer = _audit_rate_limit_refusal

    register_middleware(app)
    # Registered after the limiter is attached, since it installs the 429 handler.
    register_exception_handlers(app)

    v1_router = APIRouter()
    for mount in dict.fromkeys(mounts):
        v1_router.include_router(mount.router, prefix=mount.prefix, tags=[mount.tag])
    app.include_router(v1_router, prefix=API_V1_PREFIX)

    if settings.docs_enabled:
        _mount_scalar(app, title)
        for tool in development_tools:
            tool(app)

    app.add_api_route("/health", health, methods=["GET"], tags=["meta"])
    app.add_api_route("/ready", ready, methods=["GET"], tags=["meta"])
    return app


def _mount_scalar(app: FastAPI, title: str) -> None:
    """Mount the Scalar API reference, the primary docs UI.

    Called only inside the docs gate rather than returning 404 from the
    handler, so in production the route does not exist at all: nothing to
    probe, and no chance a later edit to the body re-exposes it.

    Swagger (/docs) and ReDoc (/redoc) stay mounted beside it; they cost nothing
    and Swagger's "Try it out" is still the quickest way to exercise an endpoint
    by hand.
    """

    @app.get("/scalar", include_in_schema=False)
    async def scalar_docs() -> HTMLResponse:
        return get_scalar_api_reference(
            openapi_url=app.openapi_url,
            title=f"{title} — API Reference",
            # Off by default: telemetry pings Scalar when the built-in client
            # sends a request, and an internal API's usage is not theirs to see.
            telemetry=False,
            # No proxy: requests from the docs go straight to this API. Routing
            # them through proxy.scalar.com would put request bodies, headers,
            # and bearer tokens through a third party.
            scalar_proxy_url="",
        )


async def health() -> dict[str, str]:
    """Liveness probe: reports the process is up, without touching the database.

    Deliberately does not query Postgres — a readiness check that fails on a
    slow database will get your healthy container killed by the orchestrator.
    """
    return {"status": "ok", "env": settings.environment}


async def _check_database() -> None:
    """Run the cheapest possible query that proves the database is reachable.

    Separate from the endpoint so the probe's failure handling can be tested
    without a real outage: the engine's ``connect`` is read-only and cannot be
    patched, this can.
    """
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))


async def ready(response: Response) -> dict[str, str]:
    """Readiness probe: reports whether this process can actually serve traffic.

    The counterpart to ``/health`` above, and deliberately a separate endpoint
    rather than a flag on it. They answer different questions and a failure of
    each should have the opposite effect:

    - ``/health`` is liveness — "is the process alive?" A failure means restart
      me. It must not touch the database, or a slow query gets healthy
      containers killed and turns a database blip into a rolling outage.
    - ``/ready`` is readiness — "should traffic be routed here?" A failure means
      take me out of the load balancer, but leave me running: the database may
      come back, and a restart would not help if it does not.

    ``SELECT 1`` under a short timeout, because the check has to fail faster
    than the probe interval — a readiness check that hangs is indistinguishable
    from one that failed, except it also ties up a worker each time it runs.

    503 rather than a 200 carrying a status field: orchestrators route on the
    status code, and a body saying "not ready" behind a 200 keeps traffic
    coming.
    """
    try:
        async with asyncio.timeout(READINESS_TIMEOUT_SECONDS):
            await _check_database()
    except (TimeoutError, SQLAlchemyError) as exc:
        # Logged, not returned: the reason names infrastructure and an
        # unauthenticated probe endpoint is the wrong place to describe it.
        logger.warning("Readiness check failed: %s", exc)
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "unavailable", "env": settings.environment}
    return {"status": "ready", "env": settings.environment}


async def _audit_rate_limit_refusal(request: Request) -> None:
    """A reset rejected before its handler still resolves with one audit row."""
    if request.url.path.endswith("/auth/forgot-password"):
        async with AsyncSessionLocal() as db:
            await auth_service.record_request_refusal(db, client_ip(request))
