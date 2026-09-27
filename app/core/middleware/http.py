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

import jwt
from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

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

    @app.middleware("http")
    async def enforce_admin_session(request: Request, call_next):
        """Fail closed for every route in the administrative namespace.

        The namespace is the policy boundary, so a newly added
        ``/api/v1/admin/...`` handler is protected without adding a dependency.
        Position and scope come only from the signed session and are published
        on ``request.state`` before routing invokes the handler.
        """
        if not request.url.path.startswith("/api/v1/admin"):
            return await call_next(request)

        authorization = request.headers.get("Authorization", "")
        token = (
            authorization.removeprefix("Bearer ")
            if authorization.startswith("Bearer ")
            else request.cookies.get("admin_session")
        )
        if not token:
            return _admin_error(
                request, status.HTTP_401_UNAUTHORIZED, "Not authenticated"
            )

        claims = _decode_token(token, "access")
        if claims is None:
            challenge = _decode_token(token, "mfa_challenge")
            if challenge is None:
                return _admin_error(
                    request,
                    status.HTTP_401_UNAUTHORIZED,
                    "Invalid authentication token",
                )
            if challenge.get("enrollment_required") is True:
                return _admin_error(
                    request,
                    status.HTTP_403_FORBIDDEN,
                    "Set up multi-factor authentication to continue",
                    code="AUTH_MFA_ENROLLMENT_REQUIRED",
                )
            return _admin_error(
                request,
                status.HTTP_401_UNAUTHORIZED,
                "A second factor is required",
                code="AUTH_MFA_REQUIRED",
            )

        if claims.get("role") != "admin" or claims.get("mfa") is not True:
            return _admin_error(
                request,
                status.HTTP_403_FORBIDDEN,
                "This action is not available to your role",
                code="AUTH_INSUFFICIENT_ROLE",
            )
        position = claims.get("position")
        scope_is_invalid = (
            position not in {"chairperson", "dean", "registrar"}
            or (position == "chairperson" and not claims.get("department_id"))
            or (position == "dean" and not claims.get("college_id"))
        )
        if scope_is_invalid:
            return _admin_error(
                request,
                status.HTTP_403_FORBIDDEN,
                "An administrator position must be assigned before sign-in",
                code="AUTH_ADMIN_PROFILE_REQUIRED",
            )

        request.state.admin_claims = claims
        return await call_next(request)


def _decode_token(token: str, expected_type: str) -> dict[str, object] | None:
    """Validate one signed token without importing the auth domain.

    Keeping middleware below the domain layer avoids a core/auth import cycle;
    this repeats only the JWT boundary, not any authentication policy.
    """
    try:
        claims = jwt.decode(
            token,
            settings.jwt_secret.get_secret_value(),
            algorithms=[settings.jwt_algorithm],
        )
    except jwt.InvalidTokenError:
        return None
    return claims if claims.get("type") == expected_type else None


def _admin_error(
    request: Request,
    status_code: int,
    detail: str,
    *,
    code: str | None = None,
) -> JSONResponse:
    request_id = getattr(request.state, "request_id", None) or request.headers.get(
        "X-Request-ID", str(uuid.uuid4())
    )
    request.state.request_id = request_id
    content: dict[str, object] = {
        "detail": detail,
        "request_id": request_id,
    }
    if code is not None:
        content["code"] = code
    logger.warning(
        "admin.authorization_refused",
        extra={
            "method": request.method,
            "path": request.url.path,
            "status_code": status_code,
            "error_code": code,
        },
    )
    return JSONResponse(
        status_code=status_code,
        content=content,
        headers={"X-Request-ID": request_id},
    )
