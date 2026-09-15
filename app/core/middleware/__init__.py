"""What runs around a request, rather than inside a handler.

Layout::

    http.py         correlation id and CORS — registered on the app
    rate_limit.py   the limiter, the key function, and the @rate_limit decorator

The two are grouped because they answer the same question — what wraps a
request — but they attach at different points, and the difference is the reason
rate limiting is not ASGI middleware:

- ``http.py`` holds the concerns that are the same for **every** endpoint. A
  correlation id and a CORS policy do not depend on which route matched, so they
  can run before routing resolves.
- ``rate_limit.py`` holds a concern that is **per route**. Middleware would have
  to map a URL pattern back to a limit, which is a second source of truth that
  fails open when an endpoint is added and the table is not. The decorator sits
  on the handler instead.

That is the same reasoning that keeps authorization out of here entirely: a
permission check belongs in an endpoint dependency
(``app.domains.iam.dependencies``), beside the handler it guards.

Import from the package, not the submodules::

    from app.core.middleware import rate_limit, register_middleware
"""

from __future__ import annotations

from app.core.middleware.http import register_middleware
from app.core.middleware.rate_limit import (
    UnknownRateLimitError,
    client_ip,
    limit_names,
    limiter,
    rate_limit,
    rate_limit_forgot_password,
    rate_limit_key,
    rate_limit_login,
    rate_limit_register,
    rate_limit_resend_verification,
    retry_after_seconds,
)

__all__ = [
    "UnknownRateLimitError",
    "client_ip",
    "limit_names",
    "limiter",
    "rate_limit",
    "rate_limit_forgot_password",
    "rate_limit_key",
    "rate_limit_login",
    "rate_limit_register",
    "rate_limit_resend_verification",
    "register_middleware",
    "retry_after_seconds",
]
