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
import json
import logging
from logging.config import dictConfig
from typing import Any

from cbpupsis_core.config import settings

#: The current request's correlation id. A ContextVar rather than a global
#: because it is async-safe: each task sees its own value, so concurrent
#: requests never read each other's id.
request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "request_id", default="-"
)


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
        # default=str so a UUID or Decimal in `extra` cannot raise inside the
        # logger — a logging call must never be the thing that breaks a request.
        return json.dumps(payload, default=str)


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
                # Our middleware already logs each request with timing and a
                # correlation id; uvicorn's access log would duplicate it.
                "uvicorn.access": {"handlers": [], "propagate": False},
                # INFO here logs every SQL statement — useful when debugging a
                # query, far too noisy as a default.
                "sqlalchemy.engine": {"level": "WARNING", "propagate": True},
                # Database drivers log statements WITH their bound parameters at
                # DEBUG, which puts password hashes and tokens into the log
                # store. Pinned above DEBUG so a global log_level=DEBUG cannot
                # turn that on by accident.
                "aiosqlite": {"level": "INFO", "propagate": True},
                "asyncpg": {"level": "INFO", "propagate": True},
            },
        }
    )
