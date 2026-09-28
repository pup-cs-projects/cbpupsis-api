"""Application entry point.

Thin by design: wires routers, middleware, and exception handlers. No business
logic here — that lives in each domain's ``service.py``.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Response, status
from fastapi.responses import HTMLResponse
from scalar_fastapi import get_scalar_api_reference
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.admin import setup_admin
from app.api.v1.router import v1_router
from app.config import settings
from app.core.events import event_bus
from app.core.exceptions import register_exception_handlers
from app.core.logging import configure_logging
from app.core.middleware import limiter, register_middleware
from app.database import engine

# Before anything else: a logger used before configuration silently keeps the
# default handler and ignores everything set up here.
configure_logging()

logger = logging.getLogger(__name__)

#: Readiness must fail faster than the probe interval, or a hung check is
#: indistinguishable from a failed one while holding a worker for the duration.
READINESS_TIMEOUT_SECONDS = 2.0


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown hooks.

    Draining the event bus first gives handlers scheduled in the background a
    chance to finish before their database connections are taken away. It is
    best-effort by nature — a crash or a SIGKILL reaches neither line — which is
    the honest limit of in-process dispatch, and the argument for a broker once
    an event must not be lost. See ``app.core.events``.

    Disposing the engine on shutdown then closes pooled connections cleanly,
    which matters on Neon where abandoned connections count against the pool
    limit.
    """
    yield
    await event_bus.drain()
    await engine.dispose()


app = FastAPI(
    title=settings.app_name,
    debug=settings.debug,
    lifespan=lifespan,
    # Every docs surface hangs off settings.docs_enabled, so production exposes
    # none of them. openapi_url must be gated too: without it, /openapi.json
    # still serves the full schema even with the UIs switched off — the schema
    # IS the sensitive part, the UIs merely render it.
    docs_url="/docs" if settings.docs_enabled else None,
    redoc_url="/redoc" if settings.docs_enabled else None,
    openapi_url="/openapi.json" if settings.docs_enabled else None,
)

# slowapi reads the limiter off app.state, so the decorators in the routers can
# find it at request time. Assigning it is not optional — without it every
# limited endpoint raises at runtime rather than at startup.
app.state.limiter = limiter

register_middleware(app)
# Registered after the limiter is attached, since it installs the 429 handler.
register_exception_handlers(app)

app.include_router(v1_router, prefix="/api/v1")

if settings.docs_enabled:

    @app.get("/scalar", include_in_schema=False)
    async def scalar_docs() -> HTMLResponse:
        """Scalar API reference — the primary docs UI.

        Registered inside the gate rather than returning 404 from the handler,
        so in production the route does not exist at all: nothing to probe, and
        no chance a later edit to the body re-exposes it.

        Swagger (/docs) and ReDoc (/redoc) stay mounted beside it; they cost
        nothing and Swagger's "Try it out" is still the quickest way to exercise
        an endpoint by hand.
        """
        return get_scalar_api_reference(
            openapi_url=app.openapi_url,
            title=f"{settings.app_name} — API Reference",
            # Off by default: telemetry pings Scalar when the built-in client
            # sends a request, and an internal API's usage is not theirs to see.
            telemetry=False,
            # No proxy: requests from the docs go straight to this API. Routing
            # them through proxy.scalar.com would put request bodies, headers,
            # and bearer tokens through a third party.
            scalar_proxy_url="",
        )

    # Mounted inside the same gate, and for a stronger reason than the docs: the
    # panel edits rows directly, bypassing every service-layer rule. Outside
    # development /admin is not a login page but nothing at all.
    setup_admin(app)


@app.get("/health", tags=["meta"])
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


@app.get("/ready", tags=["meta"])
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
