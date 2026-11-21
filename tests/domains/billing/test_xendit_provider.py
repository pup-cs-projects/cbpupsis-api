"""Contract tests for the Xendit adapter; all HTTP stays in process."""

from __future__ import annotations

import json
from decimal import Decimal

import httpx
import pytest
from pydantic import SecretStr

from cbpupsis_core.config import settings
from cbpupsis_shared.domains.billing.provider import (
    GatewayCustomer,
    GatewayState,
    XenditGateway,
)


@pytest.mark.asyncio
async def test_ac_fin_002_xendit_session_uses_exact_decimal_and_basic_auth(
    monkeypatch,
):
    captured = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["request"] = request
        captured["body"] = json.loads(request.content.decode(), parse_float=Decimal)
        return httpx.Response(
            201,
            json={
                "payment_session_id": "ps-1",
                "payment_link_url": "https://checkout.xendit.co/ps-1",
            },
        )

    monkeypatch.setattr(settings, "xendit_enabled", True)
    monkeypatch.setattr(settings, "xendit_api_key", SecretStr("xnd_test_key"))
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://api.xendit.co"
    ) as client:
        result = await XenditGateway(client).create_session(
            merchant_reference="cbp-reference",
            amount=Decimal("10.50"),
            customer=GatewayCustomer(
                reference_id="1234567890abcdef",
                email="student@example.com",
                given_names="María-Jane",
                surname="Dela Cruz",
            ),
        )

    assert result.session_id == "ps-1"
    assert captured["request"].headers["authorization"].startswith("Basic ")
    assert captured["body"]["amount"] == Decimal("10.50")
    assert captured["body"]["reference_id"] == "cbp-reference"
    assert captured["body"]["allow_save_payment_method"] == "DISABLED"
    assert captured["body"]["customer"] == {
        "reference_id": "1234567890abcdef",
        "type": "INDIVIDUAL",
        "email": "student@example.com",
        "individual_detail": {
            "given_names": "Maria Jane",
            "surname": "Dela Cruz",
        },
    }
    assert not {"card", "pan", "cvv", "expiry"} & captured["body"].keys()


@pytest.mark.asyncio
async def test_ac_fin_002_xendit_reconciliation_reads_completed_session(monkeypatch):
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/sessions/ps-1"
        return httpx.Response(
            200,
            json={"status": "COMPLETED", "payment_id": "py-1"},
        )

    monkeypatch.setattr(settings, "xendit_enabled", True)
    monkeypatch.setattr(settings, "xendit_api_key", SecretStr("xnd_test_key"))
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://api.xendit.co"
    ) as client:
        result = await XenditGateway(client).get_status(
            merchant_reference="cbp-reference", session_id="ps-1"
        )

    assert result.state == GatewayState.PAID
    assert result.gateway_reference == "py-1"
