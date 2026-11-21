"""Exercise the real HTTP/OIDC boundary with signed synthetic tokens."""

import base64
import json
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from pydantic import SecretStr, ValidationError

from cbpupsis_api_student.domains.google_calendar.exceptions import ProviderError
from cbpupsis_api_student.domains.google_calendar.provider import SCOPE, GoogleProvider
from cbpupsis_core.config import Settings, settings


@pytest.fixture(scope="module")
def key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.mark.parametrize(
    "bad",
    [
        None,
        "matchingAuthorizedParty",
        "matchingMultipleAudiences",
        "nonce",
        "audience",
        "issuer",
        "expired",
        "scope",
        "signature",
        "refresh",
        "authorizedParty",
        "multipleAudiencesWithoutParty",
        "missingScope",
        "scopeType",
        "missingAccessToken",
        "missingIDToken",
        "refreshType",
    ],
)
async def test_verified_google_identity_before_accepting_refresh_token(
    monkeypatch, key, bad, caplog
):
    monkeypatch.setattr(settings, "google_calendar_client_id", "synthetic-client")
    monkeypatch.setattr(
        settings, "google_calendar_client_secret", SecretStr("synthetic-secret")
    )
    monkeypatch.setattr(
        settings,
        "google_calendar_redirect_uri",
        "https://student.example.test/callback",
    )
    claims = {
        "iss": "https://accounts.google.com",
        "aud": "synthetic-client",
        "sub": "synthetic-subject",
        "nonce": "expected-nonce",
        "iat": datetime.now(UTC),
        "exp": datetime.now(UTC) + timedelta(minutes=5),
    }
    if bad == "nonce":
        claims["nonce"] = "wrong"
    if bad == "audience":
        claims["aud"] = "wrong"
    if bad == "authorizedParty":
        claims["azp"] = "another-client"
    if bad == "multipleAudiencesWithoutParty":
        claims["aud"] = ["synthetic-client", "another-client"]
    if bad in {"matchingAuthorizedParty", "matchingMultipleAudiences"}:
        claims["azp"] = "synthetic-client"
    if bad == "matchingMultipleAudiences":
        claims["aud"] = ["synthetic-client", "another-client"]
    if bad == "issuer":
        claims["iss"] = "https://not-google.example.test"
    if bad == "expired":
        claims["exp"] = datetime.now(UTC) - timedelta(seconds=1)
    signer = (
        rsa.generate_private_key(public_exponent=65537, key_size=2048)
        if bad == "signature"
        else key
    )
    encoded = jwt.encode(
        claims, signer, algorithm="RS256", headers={"kid": "synthetic-key"}
    )
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    jwk["kid"] = "synthetic-key"

    def transport(request):
        if request.url.path == "/token":
            form = parse_qs(request.content.decode())
            assert form["code_verifier"] == ["synthetic-pkce"]
            data = {
                "access_token": "synthetic-access",
                "refresh_token": "synthetic-refresh",
                "id_token": encoded,
                "scope": SCOPE if bad != "scope" else "openid",
            }
            if bad == "refresh":
                data.pop("refresh_token")
            if bad == "missingScope":
                data.pop("scope")
            if bad == "scopeType":
                data["scope"] = [SCOPE]
            if bad == "missingAccessToken":
                data.pop("access_token")
            if bad == "missingIDToken":
                data.pop("id_token")
            if bad == "refreshType":
                data["refresh_token"] = True
            return httpx.Response(200, json=data)
        assert (
            request.url.host == "www.googleapis.com"
            and request.url.path == "/oauth2/v3/certs"
        )
        return httpx.Response(200, json={"keys": [jwk]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        provider = GoogleProvider(client)
        if bad in {None, "matchingAuthorizedParty", "matchingMultipleAudiences"}:
            assert await provider.exchange(
                "synthetic-code", "synthetic-pkce", "expected-nonce"
            ) == ("synthetic-refresh", "synthetic-subject")
        else:
            with pytest.raises(ProviderError) as error:
                await provider.exchange(
                    "synthetic-code", "synthetic-pkce", "expected-nonce"
                )
            assert error.value.code == "authorizationFailed"
            stage = {
                "scope": "scope",
                "missingScope": "scope",
                "scopeType": "scope",
                "missingAccessToken": "access_token",
                "refresh": "refresh_token",
                "refreshType": "refresh_token",
                "missingIDToken": "id_token",
                "nonce": "identity_binding",
                "authorizedParty": "identity_binding",
                "multipleAudiencesWithoutParty": "identity_binding",
            }.get(bad, "identity")
            assert f"calendar_oauth.exchange_rejected.{stage}" in caplog.text
            for private in (
                "synthetic-code",
                "synthetic-pkce",
                "synthetic-access",
                "synthetic-refresh",
                "synthetic-subject",
                "expected-nonce",
                encoded,
            ):
                assert private not in caplog.text


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (400, {"error": "invalid_grant"}, "reconnectRequired"),
        (401, {}, "reconnectRequired"),
        (403, {"error": {"errors": [{"reason": "rateLimitExceeded"}]}}, "quotaLimited"),
        (
            403,
            {"error": {"errors": [{"reason": "dailyLimitExceeded"}]}},
            "quotaLimited",
        ),
        (403, {"error": {"errors": []}}, "permissionDenied"),
        (429, {}, "quotaLimited"),
        (500, {"private": "do-not-echo"}, "providerUnavailable"),
    ],
)
async def test_provider_errors_are_sanitized(status, body, expected):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(status, json=body))
    ) as client:
        with pytest.raises(ProviderError) as error:
            await GoogleProvider(client).request(
                "POST", "https://oauth2.googleapis.com/token"
            )
        assert error.value.code == expected
        assert "do-not-echo" not in str(error.value)


@pytest.mark.parametrize("error", ["invalid_token", "invalid_request"])
async def test_revocation_only_accepts_invalid_token_as_already_revoked(error):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(400, json={"error": error})
        )
    ) as client:
        provider = GoogleProvider(client)
        if error == "invalid_token":
            await provider.revoke("synthetic-refresh")
        else:
            with pytest.raises(ProviderError):
                await provider.revoke("synthetic-refresh")


def test_disabled_settings_need_no_google_credentials():
    assert not Settings(
        _env_file=None,
        database_url="postgresql://test:test@localhost/test",
        jwt_secret="x" * 40,
    ).google_calendar_enabled


@pytest.mark.parametrize("bad", ["missing", "key", "redirect", "debug"])
def test_enabled_settings_fail_closed_without_echoing_secrets(bad):
    values = {
        "database_url": "postgresql://test:test@localhost/test",
        "jwt_secret": "x" * 40,
        "google_calendar_enabled": True,
        "google_calendar_client_id": "synthetic",
        "google_calendar_client_secret": "SENSITIVE-CLIENT-SECRET",
        "google_calendar_redirect_uri": "https://student.example.test/callback",
        "google_calendar_encryption_key": base64.urlsafe_b64encode(b"x" * 32).decode(),
    }
    if bad == "missing":
        values.pop("google_calendar_client_id")
    if bad == "key":
        values["google_calendar_encryption_key"] = "wrong"
    if bad == "redirect":
        values["google_calendar_redirect_uri"] = "http://external.example.test/callback"
    if bad == "debug":
        values["debug"] = True
    with pytest.raises(ValidationError) as error:
        Settings(_env_file=None, **values)
    assert "SENSITIVE-CLIENT-SECRET" not in str(error.value)
