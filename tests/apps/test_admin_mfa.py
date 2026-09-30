"""Focused Admin sign-in checks for the ACs this PR actually delivers."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession
from webauthn.helpers.exceptions import InvalidRegistrationResponse

from cbpupsis_api_admin.domains.admin_auth import security
from cbpupsis_api_admin.domains.admin_auth import service as admin_service
from cbpupsis_api_admin.domains.admin_auth.repository import get_mfa_credential
from cbpupsis_database.models.admin_auth import AdminPosition, MfaType
from cbpupsis_shared.domains.iam import service as iam_service
from cbpupsis_shared.domains.iam.constants import ADMIN_GROUP

AUTH = "/api/v1/auth"


@pytest.fixture
async def admin_user(
    client: AsyncClient, db: AsyncSession, registered_user: dict[str, str]
) -> dict[str, str]:
    group = await iam_service.create_group(db, name=ADMIN_GROUP)
    user_id = uuid.UUID(registered_user["id"])
    await iam_service.add_user_to_group(db, user_id, group.id)
    await admin_service.configure_admin_profile(
        db, user_id=user_id, position=AdminPosition.registrar
    )
    return {**registered_user, "admin_group_id": str(group.id)}


async def _challenge(client: AsyncClient, admin_user: dict[str, str]) -> dict:
    response = await client.post(
        f"{AUTH}/admin/login",
        json={"email": admin_user["email"], "password": admin_user["password"]},
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _totp_pair(client: AsyncClient, challenge_token: str) -> tuple[dict, str]:
    enrolled = await client.post(
        f"{AUTH}/admin/mfa/totp/enroll", json={"challenge_token": challenge_token}
    )
    assert enrolled.status_code == 200, enrolled.text
    secret = enrolled.json()["secret"]
    code = security.totp_at(secret, int(datetime.now(UTC).timestamp()) // 30)
    confirmed = await client.post(
        f"{AUTH}/admin/mfa/totp/confirm",
        json={"challenge_token": challenge_token, "code": code},
    )
    assert confirmed.status_code == 200, confirmed.text
    return confirmed.json(), code


async def test_ac_003_1_password_issues_only_a_challenge(
    client: AsyncClient, admin_user: dict[str, str]
) -> None:
    challenge = await _challenge(client, admin_user)
    assert challenge["mfa_required"] is True
    assert challenge["enrollment_required"] is True
    assert "access_token" not in challenge
    assert "refresh_token" not in challenge

    refused = await client.get(
        "/api/v1/admin/me",
        headers={"Authorization": f"Bearer {challenge['challenge_token']}"},
    )
    assert refused.status_code == 403
    assert refused.json()["code"] == "AUTH_MFA_ENROLLMENT_REQUIRED"

    password_only = await client.post(
        f"{AUTH}/login",
        json={"email": admin_user["email"], "password": admin_user["password"]},
    )
    assert password_only.status_code == 401


async def test_ac_003_2_6_totp_enrollment_and_replay(
    client: AsyncClient, admin_user: dict[str, str]
) -> None:
    challenge = await _challenge(client, admin_user)
    pair, used_code = await _totp_pair(client, challenge["challenge_token"])
    assert pair["role"] == "admin"
    assert pair["position"] == "registrar"

    me = await client.get(
        "/api/v1/admin/me",
        headers={"Authorization": f"Bearer {pair['access_token']}"},
    )
    assert me.status_code == 200
    assert me.json()["role"] == "admin"

    next_challenge = await _challenge(client, admin_user)
    assert next_challenge["enrollment_required"] is False
    replay = await client.post(
        f"{AUTH}/admin/mfa/verify",
        json={"challenge_token": next_challenge["challenge_token"], "code": used_code},
    )
    assert replay.status_code == 401
    assert replay.json()["code"] == "AUTH_MFA_CODE_REUSED"


async def test_ac_003_2_6_first_time_webauthn_enrollment_and_sign_in(
    client: AsyncClient,
    db: AsyncSession,
    admin_user: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    challenge = await _challenge(client, admin_user)
    options = await client.post(
        f"{AUTH}/admin/mfa/webauthn/enroll",
        json={"challenge_token": challenge["challenge_token"]},
    )
    assert options.status_code == 200, options.text
    public_key = options.json()["public_key"]
    assert public_key["challenge"] == challenge["webauthn_challenge"]
    assert public_key["rp"]["id"] == challenge["webauthn_rp_id"]

    monkeypatch.setattr(
        admin_service,
        "verify_webauthn_registration",
        lambda **_kwargs: ("AQID", "BAUG", 0),
    )
    confirmed = await client.post(
        f"{AUTH}/admin/mfa/webauthn/confirm",
        json={
            "challenge_token": challenge["challenge_token"],
            "credential": {"id": "AQID"},
        },
    )
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["role"] == "admin"

    stored = await get_mfa_credential(
        db, user_id=uuid.UUID(admin_user["id"]), mfa_type=MfaType.webauthn
    )
    assert stored is not None and stored.is_enrolled
    assert stored.credential_id != "AQID"
    assert stored.public_key != "BAUG"

    next_challenge = await _challenge(client, admin_user)
    assert next_challenge["methods"] == ["webauthn"]
    assert next_challenge["webauthn_credential_id"] == "AQID"
    monkeypatch.setattr(admin_service, "verify_webauthn_assertion", lambda **_kwargs: 1)
    verified = await client.post(
        f"{AUTH}/admin/mfa/verify",
        json={
            "challenge_token": next_challenge["challenge_token"],
            "assertion": {"id": "AQID"},
        },
    )
    assert verified.status_code == 200, verified.text
    assert verified.json()["role"] == "admin"


async def test_invalid_webauthn_registration_locks_without_a_session(
    client: AsyncClient,
    admin_user: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    challenge = await _challenge(client, admin_user)
    monkeypatch.setattr(
        admin_service, "verify_webauthn_registration", lambda **_kwargs: None
    )
    for _ in range(5):
        refused = await client.post(
            f"{AUTH}/admin/mfa/webauthn/confirm",
            json={"challenge_token": challenge["challenge_token"], "credential": {}},
        )
        assert refused.status_code == 401
        assert refused.json()["code"] == "AUTH_MFA_INVALID"
        assert "access_token" not in refused.json()

    locked = await client.post(
        f"{AUTH}/admin/mfa/webauthn/confirm",
        json={"challenge_token": challenge["challenge_token"], "credential": {}},
    )
    assert locked.status_code == 423
    assert locked.json()["code"] == "AUTH_ACCOUNT_LOCKED"


async def test_admin_refresh_only_uses_live_role_and_scope(
    client: AsyncClient, db: AsyncSession, admin_user: dict[str, str]
) -> None:
    challenge = await _challenge(client, admin_user)
    pair, _ = await _totp_pair(client, challenge["challenge_token"])
    user_id = uuid.UUID(admin_user["id"])

    generic = await client.post(
        f"{AUTH}/refresh", json={"refresh_token": pair["refresh_token"]}
    )
    assert generic.status_code == 401

    await admin_service.configure_admin_profile(
        db, user_id=user_id, position=AdminPosition.dean, college_id="college-1"
    )
    stale_scope = await client.post(
        f"{AUTH}/refresh", json={"refresh_token": pair["refresh_token"]}
    )
    assert stale_scope.status_code == 401
    updated = await client.post(
        f"{AUTH}/admin/refresh", json={"refresh_token": pair["refresh_token"]}
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["position"] == "dean"
    assert updated.json()["college_id"] == "college-1"

    await iam_service.remove_user_from_group(
        db, user_id, int(admin_user["admin_group_id"])
    )
    refused = await client.post(
        f"{AUTH}/admin/refresh",
        json={"refresh_token": updated.json()["refresh_token"]},
    )
    assert refused.status_code == 401
    generic_without_role = await client.post(
        f"{AUTH}/refresh",
        json={"refresh_token": updated.json()["refresh_token"]},
    )
    assert generic_without_role.status_code == 401


def test_webauthn_registration_verifies_challenge_rp_origin_and_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict = {}

    def verify(**kwargs):
        seen.update(kwargs)
        return SimpleNamespace(
            credential_id=b"\x01\x02\x03",
            credential_public_key=b"\x04\x05\x06",
            sign_count=3,
        )

    monkeypatch.setattr(security, "verify_registration_response", verify)
    result = security.verify_webauthn_registration(
        credential={"id": "AQID"}, challenge="AQID"
    )
    assert result == ("AQID", "BAUG", 3)
    assert seen["expected_challenge"] == b"\x01\x02\x03"
    assert seen["expected_rp_id"] == security.settings.webauthn_rp_id
    assert seen["expected_origin"] == security.settings.webauthn_origin
    assert seen["require_user_verification"] is True

    monkeypatch.setattr(
        security,
        "verify_registration_response",
        lambda **_kwargs: (_ for _ in ()).throw(InvalidRegistrationResponse()),
    )
    assert (
        security.verify_webauthn_registration(credential={}, challenge="AQID") is None
    )
