"""Application error hierarchy and FastAPI exception handlers.

Domain and service code raises the semantic errors defined here instead of
constructing HTTP responses directly — keeping those layers framework-agnostic.
The registered handlers translate each error into a JSON response at the edge.

All responses share one envelope, ``{"detail": ..., "request_id": ...}``, so
clients parse errors uniformly and every failure can be traced back to a log
line via the correlation id.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from slowapi.errors import RateLimitExceeded

from cbpupsis_core.config import settings
from cbpupsis_core.middleware import retry_after_seconds

logger = logging.getLogger(__name__)


class AppError(Exception):
    """Base class for expected, translatable application errors.

    Subclasses set :attr:`status_code`; the message passed to the constructor
    becomes the response ``detail``.

    An optional :attr:`code` carries a **stable machine-readable identifier** for
    errors the frontend must branch on rather than merely display. ``detail`` is
    prose written for a human and may be reworded at any time; ``code`` is part
    of the API contract and may not. Most errors need no code — a client that
    only renders the message should not be given something to switch on — so it
    is omitted from the response entirely unless set, leaving every existing
    error body byte-identical.
    """

    status_code: int = 400
    #: Class-level default, so a subclass can pin one code for every instance.
    code: str | None = None

    def __init__(
        self,
        detail: str,
        *,
        code: str | None = None,
        response_fields: dict[str, object] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.detail = detail
        if code is not None:
            self.code = code
        self.response_fields = response_fields or {}
        self.headers = headers or {}
        super().__init__(detail)


class NotFoundError(AppError):
    """A requested resource does not exist (HTTP 404)."""

    status_code = 404


class UnauthorizedError(AppError):
    """Authentication is missing or invalid (HTTP 401)."""

    status_code = 401


class ForbiddenError(AppError):
    """The authenticated user lacks the required permission (HTTP 403)."""

    status_code = 403


class EmailNotVerifiedError(ForbiddenError):
    """Login was refused because the address has not been verified (HTTP 403).

    Carries a fixed :attr:`code` because this is the one auth failure a client
    must *act* on rather than display: the correct response is to route the user
    to a "resend verification" screen, not back to the login form. Branching on
    the prose message instead would break the moment the copy is reworded.
    """

    code = "email_not_verified"


class ConflictError(AppError):
    """The request conflicts with existing state, e.g. a duplicate unique
    value such as an already-registered email (HTTP 409)."""

    status_code = 409


class ValidationError(AppError):
    """The request was well-formed but breaks a business rule (HTTP 422).

    Distinct from FastAPI's ``RequestValidationError``, which covers what the
    schema layer can see (a missing field, a bad type). This one covers rules
    that need state to evaluate — "onboarding needs a display name you have not
    set" — and so can only be raised once the row is loaded, in the service.
    """

    status_code = 422


def _request_id(request: Request) -> str | None:
    """Return the correlation id attached by the middleware, if present."""
    return getattr(request.state, "request_id", None)


def register_exception_handlers(app: FastAPI) -> None:
    """Register the handlers that render errors as JSON."""

    @app.exception_handler(AppError)
    async def handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        """Render a known application error with its status code.

        ``code`` is added only when the error carries one, so responses for the
        errors that do not are unchanged — clients parsing them keep working and
        no key appears with a null value for them to misread as meaningful.
        """
        content: dict[str, object] = {
            "detail": exc.detail,
            "request_id": _request_id(request),
        }
        if exc.code is not None:
            content["code"] = exc.code
        if hasattr(exc, "data") and exc.data is not None:
            content["data"] = exc.data

        headers: dict[str, str] = {}
        if hasattr(exc, "retry_after_seconds") and exc.retry_after_seconds is not None:
            headers["Retry-After"] = str(exc.retry_after_seconds)

        return JSONResponse(
            status_code=exc.status_code,
            content=content,
            headers=headers if headers else None,
        )

    @app.exception_handler(RateLimitExceeded)
    async def handle_rate_limit_exceeded(
        request: Request, exc: RateLimitExceeded
    ) -> JSONResponse:
        """Render a rate-limit rejection in the standard envelope.

        slowapi's own handler emits ``{"error": "3 per 1 hour"}`` — a different
        shape from every other error this API returns, leaking the configured
        limit, and phrased for a developer rather than the person who will read
        it. Registering this handler overrides it, so a client parses one
        envelope for all failures.

        ``Retry-After`` is what makes the 429 actionable: without it a client
        can only guess, and well-behaved ones typically retry immediately.
        """
        retry_after = retry_after_seconds(exc)
        # Deliberately not a warning: being rate limited is the system working,
        # and at scale a warning per refused request is what buries the real
        # ones. The key is included so an operator can see who is hitting it;
        # it is an IP or a user id, never a credential.
        logger.info(
            "request.rate_limited",
            extra={
                "method": request.method,
                "path": request.url.path,
                "limit": str(exc.limit.limit),
            },
        )
        return JSONResponse(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            content={
                "detail": (
                    f"Too many requests. Please try again in {retry_after} seconds."
                ),
                "request_id": _request_id(request),
                "code": "rate_limited",
            },
            headers={"Retry-After": str(retry_after)},
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        """Render a 422 with the field-level errors that caused it.

        FastAPI's default omits the correlation id and, in debug, the body —
        both of which make a malformed-request report actionable.
        """
        # jsonable_encoder because error context can hold values json cannot
        # serialise directly (Decimal from a numeric constraint, bytes from a
        # malformed body).
        content: dict[str, object] = {
            "detail": jsonable_encoder(exc.errors()),
            "request_id": _request_id(request),
        }
        if settings.debug:
            content["body"] = jsonable_encoder(exc.body)
        return JSONResponse(
            status_code=422,  # UNPROCESSABLE_CONTENT; the constant was renamed
            content=content,
        )

    @app.exception_handler(Exception)
    async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        """Catch anything unhandled: log it in full, return an opaque 500.

        Without this, an unexpected exception can surface a stack trace or SQL
        fragment to the client. The correlation id is the bridge between the
        generic response and the full detail in the logs.
        """
        logger.exception(
            "Unhandled error on %s %s (request_id=%s)",
            request.method,
            request.url.path,
            _request_id(request),
            exc_info=exc,
        )
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "detail": "Internal server error",
                "request_id": _request_id(request),
            },
        )
