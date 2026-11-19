"""Logging configuration: structured JSON when deployed, readable text locally.

Two things make logs useful during an incident, and both are set up here:

- **Structure.** A log aggregator queries fields, not prose. Values passed via
  ``extra={...}`` are promoted to top-level JSON keys, so ``order_id`` becomes a
  dimension you can filter on rather than a substring to regex.
- **Correlation.** Every line carries the id of the request that produced it, so
  one request's lines can be retrieved as a group — including from services and
  event handlers that never see the ``Request`` object.

``configure_logging`` is called by ``cbpupsis_shared.application.create_app``
before the app is built, and by the worker before its loop starts.
"""

from __future__ import annotations

import contextvars
import functools
import inspect
import json
import logging
import time
from collections.abc import Callable, Iterable
from logging.config import dictConfig
from typing import Any

from fastapi import Request

from cbpupsis_core.config import settings

#: The current request's correlation id. A ContextVar rather than a global
#: because it is async-safe: each task sees its own value, so concurrent
#: requests never read each other's id.
request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "request_id", default="-"
)


def request_log_path(request: Request) -> str:
    """Log route templates, never student numbers or arbitrary URL identifiers."""
    return getattr(request.scope.get("route"), "path", "<unmatched>")


class CorrelationFilter(logging.Filter):
    """Attach the current request id to every record.

    A filter rather than a formatter detail, so the field exists on records
    emitted outside a request (startup, background work) and the text formatter
    never raises KeyError.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        return True


class JsonFormatter(logging.Formatter):
    """Render records as single-line JSON for log aggregators."""

    #: Attributes LogRecord always defines. Anything outside this set arrived
    #: via extra={...} and is caller data worth emitting.
    _RESERVED = frozenset(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {
        "taskName",
        "request_id",
        "message",
        "asctime",
    }

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": getattr(record, "request_id", "-"),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        payload.update(
            {
                key: value
                for key, value in record.__dict__.items()
                if key not in self._RESERVED
            }
        )
        return json.dumps(payload, default=str)


#: Sensitive field names that must never be opted into via ``@logged(..., fields=...)``.
#: Covers credentials, student PII (RA 10173), academic records, and financial data.
_DENIED_LOG_FIELDS: frozenset[str] = frozenset(
    {
        "password",
        "passwd",
        "password_hash",
        "secret",
        "token",
        "access_token",
        "refresh_token",
        "credential",
        "credentials",
        "otp",
        "totp",
        "pin",
        "student_number",
        "employee_number",
        "email",
        "phone",
        "phone_number",
        "mobile",
        "mobile_number",
        "address",
        "birthdate",
        "birth_date",
        "dob",
        "name",
        "first_name",
        "last_name",
        "middle_name",
        "full_name",
        "contact",
        "grade",
        "grades",
        "score",
        "gwa",
        "remarks",
        "comment",
        "notes",
        "reason",
        "balance",
        "amount",
        "tuition",
    }
)

#: Credential tokens rejected even when suffixed with ``_id``.
_DENIED_LOG_FIELD_TOKENS: frozenset[str] = frozenset(
    {
        "password",
        "passwd",
        "secret",
        "token",
        "credential",
        "credentials",
        "otp",
        "totp",
        "pin",
        "email",
        "birthdate",
        "phone",
        "mobile",
        "address",
        "remarks",
    }
)

#: Structured fields owned by LogRecord, CorrelationFilter, or @logged itself.
_RESERVED_LOG_FIELDS: frozenset[str] = JsonFormatter._RESERVED | frozenset(
    {
        "event",
        "phase",
        "outcome",
        "duration_ms",
        "error_code",
        "error_type",
    }
)


def _validate_logged_fields(
    fields: Iterable[str],
    sig: inspect.Signature,
) -> tuple[str, ...]:
    """Validate opt-in ``fields`` at decoration time so unsafe choices fail early."""
    if isinstance(fields, str | bytes):
        raise TypeError("fields must be an iterable of field names, not a string")

    validated: list[str] = []
    has_var_kw = any(
        p.kind is inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
    )
    for name in fields:
        if not isinstance(name, str) or not name:
            raise ValueError(f"Invalid @logged field name: {name!r}")
        lowered = name.lower()
        tokens = set(lowered.split("_"))
        if lowered in _DENIED_LOG_FIELDS or (tokens & _DENIED_LOG_FIELD_TOKENS):
            raise ValueError(
                f"Field {name!r} is denied in @logged fields to protect sensitive data"
            )
        if name in _RESERVED_LOG_FIELDS:
            raise ValueError(
                f"Field {name!r} is reserved by logging and cannot be used in @logged"
            )
        if name != "id" and not name.endswith("_id"):
            raise ValueError(
                f"Only id fields ('id' or '*_id') are allowed in @logged, got {name!r}"
            )
        if not has_var_kw and name not in sig.parameters:
            raise ValueError(
                f"Field {name!r} is not a parameter of the wrapped function"
            )
        validated.append(name)
    return tuple(validated)


def _extract_fields(
    sig: inspect.Signature,
    field_names: tuple[str, ...],
    var_kw: str | None,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> dict[str, Any]:
    if not field_names:
        return {}
    try:
        bound = sig.bind_partial(*args, **kwargs)
    except TypeError:
        return {}
    bound.apply_defaults()
    kw_bucket = bound.arguments.get(var_kw, {}) if var_kw is not None else {}
    extracted: dict[str, Any] = {}
    for name in field_names:
        if name in bound.arguments and name != var_kw:
            extracted[name] = bound.arguments[name]
        elif name in kw_bucket:
            extracted[name] = kw_bucket[name]
    return extracted


def _slow_threshold(slow_ms: float | None) -> float:
    return settings.log_slow_call_ms if slow_ms is None else slow_ms


def _log_start(
    func_logger: logging.Logger,
    event: str,
    bound_fields: dict[str, Any],
) -> None:
    func_logger.debug(
        event,
        extra={"event": event, "phase": "start", **bound_fields},
    )


def _log_success(
    func_logger: logging.Logger,
    event: str,
    elapsed_ms: float,
    threshold: float,
    bound_fields: dict[str, Any],
) -> None:
    level = logging.WARNING if elapsed_ms > threshold else logging.INFO
    func_logger.log(
        level,
        event,
        extra={
            "event": event,
            "phase": "end",
            "outcome": "ok",
            "duration_ms": round(elapsed_ms, 1),
            **bound_fields,
        },
    )


def _log_failure(
    func_logger: logging.Logger,
    event: str,
    exc: Exception,
    elapsed_ms: float,
    threshold: float,
    bound_fields: dict[str, Any],
) -> None:
    # Deferred to break the cbpupsis_core.exceptions -> logging/middleware cycle.
    from cbpupsis_core.exceptions import AppError  # noqa: PLC0415

    duration_ms = round(elapsed_ms, 1)
    if isinstance(exc, AppError):
        level = logging.WARNING if elapsed_ms > threshold else logging.INFO
        func_logger.log(
            level,
            event,
            extra={
                "event": event,
                "phase": "end",
                "outcome": "refused",
                "error_code": exc.code or type(exc).__name__,
                "duration_ms": duration_ms,
                **bound_fields,
            },
        )
    else:
        func_logger.error(
            event,
            exc_info=True,
            extra={
                "event": event,
                "phase": "end",
                "outcome": "error",
                "error_type": type(exc).__name__,
                "duration_ms": duration_ms,
                **bound_fields,
            },
        )


def logged[**P, R](
    event: str,
    *,
    fields: Iterable[str] = (),
    slow_ms: float | None = None,
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """Log structured start, end, and failure lines for a service function.

    Works on both ``async def`` and ``def`` functions while preserving the
    wrapped function's name, docstring, and signature. Arguments and return
    values are never logged unless explicitly opted in through ``fields``
    (id parameters only; sensitive names fail at decoration time).
    """
    if not event or not event.strip():
        raise ValueError("event name must not be empty")

    def decorator(func: Callable[P, R]) -> Callable[P, R]:
        sig = inspect.signature(func)
        field_names = _validate_logged_fields(fields, sig)
        var_kw = next(
            (
                p.name
                for p in sig.parameters.values()
                if p.kind is inspect.Parameter.VAR_KEYWORD
            ),
            None,
        )
        func_logger = logging.getLogger(func.__module__)

        if inspect.iscoroutinefunction(func):

            @functools.wraps(func)
            async def async_wrapper(*args: P.args, **kwargs: P.kwargs) -> Any:
                bound_fields = _extract_fields(sig, field_names, var_kw, args, kwargs)
                _log_start(func_logger, event, bound_fields)
                started = time.perf_counter()
                try:
                    result = await func(*args, **kwargs)
                except Exception as exc:
                    elapsed_ms = (time.perf_counter() - started) * 1000
                    _log_failure(
                        func_logger,
                        event,
                        exc,
                        elapsed_ms,
                        _slow_threshold(slow_ms),
                        bound_fields,
                    )
                    raise
                elapsed_ms = (time.perf_counter() - started) * 1000
                _log_success(
                    func_logger,
                    event,
                    elapsed_ms,
                    _slow_threshold(slow_ms),
                    bound_fields,
                )
                return result

            return async_wrapper  # type: ignore[return-value]

        @functools.wraps(func)
        def sync_wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            bound_fields = _extract_fields(sig, field_names, var_kw, args, kwargs)
            _log_start(func_logger, event, bound_fields)
            started = time.perf_counter()
            try:
                result = func(*args, **kwargs)
            except Exception as exc:
                elapsed_ms = (time.perf_counter() - started) * 1000
                _log_failure(
                    func_logger,
                    event,
                    exc,
                    elapsed_ms,
                    _slow_threshold(slow_ms),
                    bound_fields,
                )
                raise
            elapsed_ms = (time.perf_counter() - started) * 1000
            _log_success(
                func_logger,
                event,
                elapsed_ms,
                _slow_threshold(slow_ms),
                bound_fields,
            )
            return result

        return sync_wrapper

    return decorator


def configure_logging() -> None:
    """Install the application's logging configuration.

    Uses ``dictConfig`` rather than ``basicConfig``: the latter silently does
    nothing when a handler already exists, which is a confusing way to lose
    every log line.
    """
    dictConfig(
        {
            "version": 1,
            # Keep third-party loggers attached; silencing them hides the
            # library errors most needed during an incident.
            "disable_existing_loggers": False,
            "formatters": {
                "text": {
                    "format": (
                        "%(asctime)s %(levelname)-5s [%(name)s] "
                        "%(message)s [%(request_id)s]"
                    ),
                },
                "json": {"()": "cbpupsis_core.logging.JsonFormatter"},
            },
            "filters": {
                "correlation": {"()": "cbpupsis_core.logging.CorrelationFilter"},
            },
            "handlers": {
                "default": {
                    "class": "logging.StreamHandler",
                    "stream": "ext://sys.stdout",
                    "formatter": "json" if settings.log_json else "text",
                    "filters": ["correlation"],
                },
            },
            "root": {"handlers": ["default"], "level": settings.log_level},
            "loggers": {
                "uvicorn.access": {"handlers": [], "propagate": False},
                "sqlalchemy.engine": {"level": "WARNING", "propagate": True},
                "aiosqlite": {"level": "INFO", "propagate": True},
                "asyncpg": {"level": "INFO", "propagate": True},
                "boto3": {"level": "WARNING", "propagate": True},
                "botocore": {"level": "WARNING", "propagate": True},
            },
        }
    )
