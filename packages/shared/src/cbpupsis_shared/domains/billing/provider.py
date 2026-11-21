"""Xendit hosted-checkout adapter and its testable gateway contract."""

from __future__ import annotations

import json
import unicodedata
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Protocol

import httpx

from cbpupsis_core.config import settings


class GatewayState(StrEnum):
    PENDING = "PENDING"
    PAID = "PAID"
    FAILED = "FAILED"


@dataclass(frozen=True)
class GatewayCreateResult:
    session_id: str
    checkout_url: str


@dataclass(frozen=True)
class GatewayCustomer:
    reference_id: str
    email: str
    given_names: str
    surname: str | None = None


@dataclass(frozen=True)
class GatewayStatusResult:
    state: GatewayState
    gateway_reference: str | None = None
    reason: str | None = None


class GatewayRejected(Exception):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class GatewayTimedOut(Exception):
    pass


class PaymentGateway(Protocol):
    async def create_session(
        self, *, merchant_reference: str, amount: Decimal, customer: GatewayCustomer
    ) -> GatewayCreateResult: ...

    async def get_status(
        self, *, merchant_reference: str, session_id: str | None
    ) -> GatewayStatusResult: ...


def _exact_json(payload: dict, amount: Decimal) -> bytes:
    """Encode the one monetary number without a binary-float conversion."""
    marker = "__CBPUPSIS_EXACT_AMOUNT__"
    encoded = json.dumps(payload | {"amount": marker}, separators=(",", ":"))
    return encoded.replace(f'"{marker}"', format(amount, "f"), 1).encode()


def _reason(response: httpx.Response) -> str:
    """Return a bounded, credential-free provider refusal reason."""
    try:
        body = response.json()
    except (ValueError, json.JSONDecodeError):
        return f"Xendit returned HTTP {response.status_code}"
    if isinstance(body, dict):
        for key in ("message", "error_code", "error"):
            value = body.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()[:255]
    return f"Xendit returned HTTP {response.status_code}"


def _xendit_name(value: str, *, fallback: str = "") -> str:
    """Fit a stored name to Xendit's alphanumeric, 50-character fields."""
    ascii_value = (
        unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    )
    cleaned = " ".join(
        "".join(
            character if character.isalnum() else " " for character in ascii_value
        ).split()
    )
    return cleaned[:50] or fallback


class XenditGateway:
    """Create and inspect Xendit Payment Sessions using server-side credentials."""

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client

    async def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        if not settings.xendit_enabled or settings.xendit_api_key is None:
            raise GatewayRejected("Xendit payments are not configured")
        owns_client = self._client is None
        client = self._client or httpx.AsyncClient(
            base_url=settings.xendit_base_url.rstrip("/"),
            auth=(settings.xendit_api_key.get_secret_value(), ""),
            timeout=settings.xendit_timeout_seconds,
            follow_redirects=False,
            trust_env=False,
        )
        try:
            kwargs.setdefault("auth", (settings.xendit_api_key.get_secret_value(), ""))
            return await client.request(method, path, **kwargs)
        except httpx.TimeoutException as exc:
            raise GatewayTimedOut from exc
        except httpx.HTTPError as exc:
            raise GatewayRejected("Xendit is temporarily unavailable") from exc
        finally:
            if owns_client:
                await client.aclose()

    async def create_session(
        self, *, merchant_reference: str, amount: Decimal, customer: GatewayCustomer
    ) -> GatewayCreateResult:
        individual_detail = {
            "given_names": _xendit_name(customer.given_names, fallback="Student")
        }
        surname = _xendit_name(customer.surname or "")
        if surname:
            individual_detail["surname"] = surname
        payload = {
            "reference_id": merchant_reference,
            "session_type": "PAY",
            "mode": "PAYMENT_LINK",
            "currency": "PHP",
            "country": "PH",
            "customer": {
                "reference_id": customer.reference_id,
                "type": "INDIVIDUAL",
                "email": customer.email,
                "individual_detail": individual_detail,
            },
            "success_return_url": settings.xendit_success_return_url,
            "cancel_return_url": settings.xendit_cancel_return_url,
            "allow_save_payment_method": "DISABLED",
            "metadata": {"source": "cbpupsis"},
        }
        response = await self._request(
            "POST",
            "/sessions",
            content=_exact_json(payload, amount),
            headers={"Content-Type": "application/json"},
        )
        if response.status_code not in {200, 201}:
            raise GatewayRejected(_reason(response))
        try:
            body = response.json()
            session_id = body["payment_session_id"]
            checkout_url = body["payment_link_url"]
            if not isinstance(session_id, str) or not isinstance(checkout_url, str):
                raise TypeError
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise GatewayRejected("Xendit returned an incomplete session") from exc
        return GatewayCreateResult(session_id=session_id, checkout_url=checkout_url)

    async def get_status(
        self, *, merchant_reference: str, session_id: str | None
    ) -> GatewayStatusResult:
        if session_id is not None:
            response = await self._request("GET", f"/sessions/{session_id}")
            if response.status_code == 200:
                body = response.json()
                status = str(body.get("status", "")).upper()
                if status == "COMPLETED":
                    return GatewayStatusResult(
                        GatewayState.PAID,
                        gateway_reference=body.get("payment_id") or session_id,
                    )
                if status in {"EXPIRED", "CANCELED", "FAILED"}:
                    return GatewayStatusResult(
                        GatewayState.FAILED, reason=f"Xendit session {status.lower()}"
                    )
                return GatewayStatusResult(GatewayState.PENDING)
            if response.status_code != 404:
                raise GatewayRejected(_reason(response))

        response = await self._request(
            "GET", "/transactions", params={"reference_id": merchant_reference}
        )
        if response.status_code != 200:
            raise GatewayRejected(_reason(response))
        body = response.json()
        rows = body.get("data", body) if isinstance(body, dict) else body
        if not isinstance(rows, list) or not rows:
            return GatewayStatusResult(GatewayState.PENDING)
        row = rows[0] if isinstance(rows[0], dict) else {}
        status = str(row.get("status", "")).upper()
        if status in {"SUCCESS", "SUCCEEDED", "COMPLETED", "PAID"}:
            return GatewayStatusResult(
                GatewayState.PAID,
                gateway_reference=str(row.get("id") or merchant_reference),
            )
        if status in {"FAILED", "VOIDED", "REVERSED", "EXPIRED"}:
            return GatewayStatusResult(
                GatewayState.FAILED, reason=f"Xendit transaction {status.lower()}"
            )
        return GatewayStatusResult(GatewayState.PENDING)
