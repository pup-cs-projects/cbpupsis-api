"""Bounded calls to fixed Google endpoints; provider bodies never escape here."""

import logging
import secrets
from urllib.parse import quote

import httpx
import jwt

from cbpupsis_api_student.domains.google_calendar.exceptions import ProviderError
from cbpupsis_core.config import settings

SCOPE = "https://www.googleapis.com/auth/calendar.app.created"
TOKEN_URL = "https://oauth2.googleapis.com/token"
API_URL = "https://www.googleapis.com/calendar/v3"
logger = logging.getLogger(__name__)


class GoogleProvider:
    def __init__(self, client: httpx.AsyncClient):
        self.client = client

    async def request(
        self,
        method,
        url,
        *,
        token=None,
        allowed=(),
        allow_invalid_token=False,
        **kwargs,
    ):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        try:
            response = await self.client.request(method, url, headers=headers, **kwargs)
        except httpx.HTTPError:
            raise ProviderError(uncertain=method not in {"GET", "DELETE"}) from None
        if len(response.content) > 1024 * 1024:
            raise ProviderError(uncertain=method not in {"GET", "DELETE"})
        if response.status_code in allowed:
            return response.status_code, {}
        try:
            data = response.json() if response.content else {}
        except ValueError:
            raise ProviderError(uncertain=method not in {"GET", "DELETE"}) from None
        if not response.is_success:
            error = data.get("error") if isinstance(data, dict) else None
            if (
                allow_invalid_token
                and response.status_code == 400
                and error == "invalid_token"
            ):
                return response.status_code, {}
            if error == "invalid_grant" or response.status_code == 401:
                raise ProviderError("reconnectRequired")
            if response.status_code == 403:
                reasons = error.get("errors") if isinstance(error, dict) else None
                first = reasons[0] if isinstance(reasons, list) and reasons else None
                reason = first.get("reason") if isinstance(first, dict) else None
                raise ProviderError(
                    "quotaLimited"
                    if reason
                    in {
                        "rateLimitExceeded",
                        "userRateLimitExceeded",
                        "quotaExceeded",
                        "dailyLimitExceeded",
                    }
                    else "permissionDenied"
                )
            if response.status_code == 429:
                raise ProviderError("quotaLimited")
            if response.status_code in {404, 410}:
                raise ProviderError("calendarMissing")
            raise ProviderError(
                uncertain=response.status_code >= 500
                and method not in {"GET", "DELETE"}
            )
        if not isinstance(data, dict):
            raise ProviderError
        return response.status_code, data

    async def exchange(self, code: str, verifier: str, nonce: str):
        _, token = await self.request(
            "POST",
            TOKEN_URL,
            data={
                "client_id": settings.google_calendar_client_id,
                "client_secret": (
                    settings.google_calendar_client_secret.get_secret_value()
                ),
                "code": code,
                "code_verifier": verifier,
                "redirect_uri": settings.google_calendar_redirect_uri,
                "grant_type": "authorization_code",
            },
        )
        stage = "scope"
        try:
            if SCOPE not in token.get("scope", "").split():
                raise ValueError
            for field in ("access_token", "refresh_token", "id_token"):
                stage = field
                value = token.get(field)
                if not isinstance(value, str) or not 0 < len(value) <= 16384:
                    raise ValueError
            refresh, encoded = token["refresh_token"], token["id_token"]
            stage = "signing_keys"
            header = jwt.get_unverified_header(encoded)
            _, jwks = await self.request(
                "GET", "https://www.googleapis.com/oauth2/v3/certs"
            )
            key = next(
                key
                for key in jwks["keys"]
                if key.get("kid") == header.get("kid") and key.get("kty") == "RSA"
            )
            stage = "identity"
            claims = jwt.decode(
                encoded,
                jwt.PyJWK.from_dict(key).key,
                algorithms=["RS256"],
                audience=settings.google_calendar_client_id,
                issuer=["https://accounts.google.com", "accounts.google.com"],
                options={"require": ["exp", "iat", "sub", "nonce"]},
            )
            stage = "identity_binding"
            if (
                not isinstance(claims["sub"], str)
                or not claims["sub"]
                or not secrets.compare_digest(claims["nonce"], nonce)
                or claims.get("azp", settings.google_calendar_client_id)
                != settings.google_calendar_client_id
                or (
                    isinstance(claims.get("aud"), list)
                    and len(claims["aud"]) > 1
                    and "azp" not in claims
                )
            ):
                raise ValueError
            return refresh, claims["sub"]
        except (
            KeyError,
            ValueError,
            TypeError,
            AttributeError,
            StopIteration,
            jwt.PyJWTError,
        ):
            logger.warning("calendar_oauth.exchange_rejected.%s", stage)
            raise ProviderError("authorizationFailed") from None

    async def refresh(self, refresh_token: str):
        _, data = await self.request(
            "POST",
            TOKEN_URL,
            data={
                "client_id": settings.google_calendar_client_id,
                "client_secret": (
                    settings.google_calendar_client_secret.get_secret_value()
                ),
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
        )
        if (
            not isinstance(data.get("access_token"), str)
            or not 0 < len(data["access_token"]) <= 16384
        ):
            raise ProviderError
        if data.get("refresh_token") is not None and (
            not isinstance(data["refresh_token"], str)
            or not 0 < len(data["refresh_token"]) <= 16384
        ):
            raise ProviderError
        return data["access_token"], data.get("refresh_token")

    async def revoke(self, refresh_token: str):
        await self.request(
            "POST",
            "https://oauth2.googleapis.com/revoke",
            data={"token": refresh_token},
            allowed=(200,),
            allow_invalid_token=True,
        )

    async def create_calendar(self, token: str, summary: str):
        _, data = await self.request(
            "POST",
            f"{API_URL}/calendars",
            token=token,
            json={"summary": summary, "timeZone": "Asia/Manila"},
        )
        if (
            not isinstance(data.get("id"), str)
            or not data["id"]
            or data["id"] == "primary"
        ):
            raise ProviderError(uncertain=True)
        return data["id"]

    async def event(self, token, calendar_id, event_id, *, method="GET", body=None):
        url = (
            f"{API_URL}/calendars/{quote(calendar_id, safe='')}/events/"
            f"{quote(event_id, safe='')}"
        )
        kwargs = {"json": body} if body is not None else {}
        return await self.request(
            method, url, token=token, allowed=(404, 410), **kwargs
        )

    async def insert_event(self, token, calendar_id, event_id, body):
        url = f"{API_URL}/calendars/{quote(calendar_id, safe='')}/events"
        return await self.request(
            "POST", url, token=token, allowed=(409,), json={**body, "id": event_id}
        )
