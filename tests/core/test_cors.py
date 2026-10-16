"""CORS: what a browser client on a portal origin may read off a response.

US-105 promises a 429 with ``Retry-After`` on a refused reset request; a browser
hides any response header that is neither CORS-safelisted nor exposed, so the
wait is only usable by a portal on another origin if CORS exposes it.
"""

from __future__ import annotations

from httpx import AsyncClient

from cbpupsis_core.config import settings


def _exposed(response) -> set[str]:
    names = response.headers.get("access-control-expose-headers", "")
    return {name.strip().lower() for name in names.split(",") if name.strip()}


async def test_cross_origin_429_exposes_retry_after(
    client: AsyncClient, monkeypatch, registered_user
) -> None:
    """Breaks if a browser client cannot read how long to wait after a 429.

    ``Retry-After`` is not a CORS-safelisted response header, so without
    ``Access-Control-Expose-Headers`` a cross-origin script sees it as absent.
    """
    monkeypatch.setattr(settings, "rate_limit_enabled", True)
    origin = settings.cors_origins[0]
    body = {"email": registered_user["email"]}
    for _ in range(settings.password_reset_request_limit):
        await client.post("/api/v1/auth/forgot-password", json=body)

    refused = await client.post(
        "/api/v1/auth/forgot-password", json=body, headers={"Origin": origin}
    )

    assert refused.status_code == 429
    assert refused.headers["retry-after"]
    assert "retry-after" in _exposed(refused)


async def test_every_cross_origin_response_exposes_the_same_headers(
    client: AsyncClient,
) -> None:
    """The request id stays exposed beside the new header."""
    response = await client.post(
        "/api/v1/auth/forgot-password",
        json={"email": "nobody@example.com"},
        headers={"Origin": settings.cors_origins[0]},
    )
    assert response.status_code == 202
    assert {"retry-after", "x-request-id"} <= _exposed(response)
