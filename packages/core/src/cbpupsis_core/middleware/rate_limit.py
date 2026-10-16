"""Request rate limiting.

Wraps slowapi so the rest of the app depends on this module rather than on the
library: routers import :data:`limiter` and the ``rate_limit_*`` helpers, and
swapping the implementation later touches one file.

**What is limited and why.** Rate limits here protect the *unauthenticated*
surface — the endpoints an anonymous caller can reach without proving anything.
Two of them (``/resend-verification``, ``/forgot-password``) send email on
demand, so an unlimited caller can bill you for messages and bomb a stranger's
inbox from your domain. The third (``/login``) is the classic
credential-stuffing target. Limits are set in
``config.py``, not in the decorators, so they can be retuned without a deploy.

**This is not a substitute for the account-level defences.** A distributed
attacker with a large IP pool gets one bucket per address, so a per-IP limit
raises the cost of an attack without ending it. It is the cheap outer layer;
argon2 hashing, token rotation, and the identical login error remain the ones
that matter.

**Limits are per route, applied as a decorator** — not as middleware. Middleware
runs before routing resolves, so limiting there would mean re-deriving "which
limit does this path get?" from a URL pattern table: a second source of truth
that fails *open* the moment someone adds an endpoint and forgets the entry. The
decorator sits on the handler, where it is visible in review and impossible to
desynchronise from the route it guards. It is the same argument that keeps
permission checks out of ``http.py``.

Limiting one endpoint::

    @router.post("/login")
    @rate_limit("login")
    async def login(request: Request, ...): ...

``rate_limit("login")`` reads ``settings.rate_limit_login``, and an unknown name
raises :class:`UnknownRateLimitError` at import. So a new limit is two steps —
add the settings field, decorate the route — with nothing to edit here.
"""

from __future__ import annotations

import ipaddress
import logging
from collections.abc import Callable
from typing import TypeVar

from fastapi import Request
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded

from cbpupsis_core.config import settings

logger = logging.getLogger(__name__)

#: What :func:`client_ip` returns when the request carries no usable address.
UNKNOWN_CLIENT = "unknown"

F = TypeVar("F", bound=Callable[..., object])


class UnknownRateLimitError(RuntimeError):
    """Raised when a route asks for a limit that ``settings`` does not define.

    Import-time, by design: an endpoint decorated with a misspelled name would
    otherwise serve unlimited traffic, and nothing in its responses would say
    so.
    """

    def __init__(self, name: str, attribute: str) -> None:
        super().__init__(
            f"No rate limit named {name!r}: add a {attribute!r} field to Settings "
            f"in cbpupsis_core/config.py. Configured: {', '.join(limit_names())}."
        )


#: Prefix on IP-derived keys, so a caller identified by address can never
#: collide with one identified by user id (a UUID and an IP cannot be confused,
#: but the prefixes make the key self-describing in storage and in logs).
IP_KEY_PREFIX = "ip:"
USER_KEY_PREFIX = "user:"


def client_ip(request: Request) -> str:
    """Return the caller's IP, honouring ``X-Forwarded-For`` when present.

    Behind a load balancer ``request.client.host`` is the balancer itself, so
    every caller would share one bucket and the first burst would lock out
    everyone. The left-most ``X-Forwarded-For`` entry is the original client.

    **Trusting that header is only safe behind a proxy that overwrites it**, which
    is the deployment this template ships: the Docker CMD runs uvicorn with
    ``--proxy-headers --forwarded-allow-ips "*"``. A client can otherwise send
    any value and mint itself a fresh bucket per request. If this app is ever
    exposed directly to the internet, drop the header handling rather than
    keeping a limit that is trivially bypassed.
    """
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        # "client, proxy1, proxy2" — the client is first. Each hop appends, so
        # anything after the first entry is an intermediary. A value that is not
        # an address is ignored rather than trusted, so it can neither mint a
        # bucket nor reach an INET column as junk.
        client = normalized_ip_address(forwarded.split(",")[0])
        if client is not None:
            return client
    peer = normalized_ip_address(request.client.host) if request.client else None
    # No usable peer address (some ASGI transports, unix sockets): a constant
    # shares one bucket rather than exempting the caller entirely.
    return peer or UNKNOWN_CLIENT


def client_ip_or_none(request: Request) -> str | None:
    """Return :func:`client_ip`'s validated address, or ``None`` if there is none.

    For values stored in an ``INET`` column: the limiter's shared ``"unknown"``
    bucket is not an address, so it is recorded as NULL rather than refusing
    the request. A missing address usually means a proxy is not forwarding it,
    so each one logs a warning carrying only the route (the request id comes
    from the correlation filter), never the caller.
    """
    address = client_ip(request)
    if address != UNKNOWN_CLIENT:
        return address
    route = request.scope.get("route")
    logger.warning(
        "No usable client address on %s; recording NULL",
        getattr(route, "path", request.url.path),
    )
    return None


def normalized_ip_address(value: str | None) -> str | None:
    """Return ``value`` as a canonical IP address, or ``None`` if it is not one.

    A zone id (``fe80::1%eth0``) is refused: it is not storable in ``INET``.
    """
    candidate = (value or "").strip()
    if not candidate or "%" in candidate:
        return None
    try:
        return str(ipaddress.ip_address(candidate))
    except ValueError:
        return None


def rate_limit_key(request: Request) -> str:
    """Identify the caller for rate-limiting purposes.

    Prefers the authenticated user id, falling back to the client IP. Keying an
    authenticated caller by identity rather than address is what stops several
    users behind one office NAT or mobile carrier gateway from consuming each
    other's allowance — and it follows the user across networks, so an attacker
    cannot reset their own counter by changing IP.

    The user id is read from ``request.state``, populated by whatever has
    already authenticated the request. This function never decodes a token
    itself: a rate limiter must stay cheap and must not raise, and a limiter
    that rejects a malformed token would turn a 401 into a 429. An
    unauthenticated or unparseable request simply falls back to the IP — which
    is the correct key for the endpoints limited here anyway, since all four are
    reachable without credentials.
    """
    user_id = getattr(request.state, "user_id", None)
    if user_id is not None:
        return f"{USER_KEY_PREFIX}{user_id}"
    return f"{IP_KEY_PREFIX}{client_ip(request)}"


#: The application limiter.
#:
#: ``enabled`` is wired to settings so the test suite can switch limiting off:
#: the limiter is a module-level singleton whose counters would otherwise
#: persist across the whole session, making tests pass or fail depending on the
#: order they ran in.
#:
#: ``headers_enabled`` is deliberately left **off**. It would add X-RateLimit-*
#: to successful responses, but slowapi injects them by mutating the object the
#: endpoint returned, and it raises if that is not a ``Response``. Three of the
#: four limited endpoints answer 204 by returning ``None``, so switching it on
#: turns every successful call into a 500 — the advisory headers would cost a
#: reshape of the endpoints' return types to buy a nicety. ``Retry-After`` on the
#: 429, which is the header that actually matters, is set by the exception
#: handler instead and does not depend on this flag.
limiter = Limiter(
    key_func=rate_limit_key,
    storage_uri=settings.rate_limit_storage_uri,
    enabled=settings.rate_limit_enabled,
)


def rate_limit(name: str) -> Callable[[F], F]:
    """Return the route decorator that applies the ``name`` limit.

    This is the entry point routers use::

        @router.post("/login")
        @rate_limit("login")
        async def login(request: Request, ...): ...

    ``name`` selects the ``rate_limit_<name>`` field on ``settings``, so adding a
    limit to a new endpoint is a settings field plus a decorator — no edit to
    this module. **The name is validated at import time**: a typo raises
    :class:`UnknownRateLimitError` when the router module loads, not on the first
    request to that endpoint. A limit that silently never applies is the failure
    worth spending a check on, because nothing about the response distinguishes
    it from one that does.

    The decorated endpoint **must declare a ``request: Request`` parameter**.
    slowapi reads the limiter and the caller's key off it and refuses to
    decorate an endpoint without it.

    The limit is passed to slowapi as a callable it calls on each request, so the
    value is read from ``settings`` per request rather than frozen at import.

    Decorator order matters: ``@router.post`` goes above ``@rate_limit`` so the
    limit wraps the handler *before* the route registers the result. Reversed,
    the route captures the undecorated function and the limit never runs.
    """
    attribute = f"rate_limit_{name}"
    if not hasattr(settings, attribute):
        raise UnknownRateLimitError(name, attribute)
    return limiter.limit(lambda: getattr(settings, attribute))


def limit_names() -> tuple[str, ...]:
    """Return every limit name configured on ``settings``, sorted.

    Exists for the tests: one asserts that each configured limit is a valid
    slowapi expression, so a typo like ``"5/minutes"`` is caught by the suite
    rather than by a 500 on the first request to that endpoint.
    """
    prefix = "rate_limit_"
    skip = {"rate_limit_enabled", "rate_limit_storage_uri"}
    return tuple(
        sorted(
            field.removeprefix(prefix)
            for field in type(settings).model_fields
            if field.startswith(prefix) and field not in skip
        )
    )


#: Named decorators for the endpoints limited today. Equivalent to calling
#: :func:`rate_limit` with the same name — kept because the intent reads at the
#: call site, and because they resolve their settings field at import time.
rate_limit_login = rate_limit("login")
rate_limit_forgot_password = rate_limit("forgot_password")
rate_limit_resend_verification = rate_limit("resend_verification")


def retry_after_seconds(exc: RateLimitExceeded) -> int:
    """Return a conservative ``Retry-After`` for a refused request.

    The window length is an upper bound on the wait: the caller's oldest hit
    could have landed a moment ago, in which case the bucket frees up almost a
    full window later. Erring long is the right direction — advising too short a
    wait invites an immediate retry that is refused again.
    """
    return int(exc.limit.limit.get_expiry())


__all__ = [
    "UnknownRateLimitError",
    "client_ip",
    "client_ip_or_none",
    "limit_names",
    "limiter",
    "rate_limit",
    "rate_limit_forgot_password",
    "rate_limit_key",
    "rate_limit_login",
    "rate_limit_resend_verification",
    "retry_after_seconds",
]
