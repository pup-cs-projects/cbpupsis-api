"""Middleware registration.

Two cross-cutting concerns that genuinely belong here, because neither depends
on which endpoint was matched:

- **Correlation id** — threads a request id through logs and back to the client
  in ``X-Request-ID``. Convenience now; essential for tracing after a split.
- **CORS** — browser-enforced origin policy, applied before routing.

Authorization deliberately does NOT live here. Middleware runs before routing
resolves, so it would have to re-derive "which permission does this path need?"
from a URL pattern table — a second source of truth that silently fails open
when someone adds an endpoint and forgets the entry. Permission checks belong in
endpoint dependencies (``app.domains.iam.dependencies``), where the requirement
sits beside the handler it guards.
"""

from __future__ import annotations

import logging
import time
import uuid

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.core.logging import request_id_var

logger = logging.getLogger(__name__)


def register_middleware(app: FastAPI) -> None:
    """Attach application middleware to ``app``."""

    # Added first so it runs outermost: every response, including those from
    # error handlers, carries a correlation id.
    @app.middleware("http")
    async def add_correlation_id(request: Request, call_next):
        """Assign or propagate a request id, and log the request's outcome."""
        # Accept an inbound id so a trace spans services once a domain is
        # extracted; generate one otherwise.
        request_id = request.headers.get("X-Request-ID", str(uuid.uuid4()))
        # The ContextVar reaches loggers in services and event handlers that
        # never see `request`; request.state serves code that has the request.
        request_id_var.set(request_id)
        request.state.request_id = request_id

        started = time.perf_counter()
        response = await call_next(request)
        elapsed_ms = (time.perf_counter() - started) * 1000

        response.headers["X-Request-ID"] = request_id
        # Structured: a stable message plus queryable fields, rather than one
        # formatted string a log aggregator would have to parse.
        logger.info(
            "request.completed",
            extra={
                "method": request.method,
                "path": request.url.path,
                "status_code": response.status_code,
                "duration_ms": round(elapsed_ms, 1),
            },
        )
        return response

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        # Lets browser clients read the correlation id off the response, so a
        # frontend bug report can cite the same id that appears in the logs.
        expose_headers=["X-Request-ID"],
    )
