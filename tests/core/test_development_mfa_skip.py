"""The temporary MFA exception must fail closed outside opted-in development."""

import uuid

import pytest
from pydantic import ValidationError

from cbpupsis_core.config import Settings, settings
from cbpupsis_shared.domains.auth.exceptions import InvalidAuthTokenError
from cbpupsis_shared.domains.auth.security import (
    create_access_token,
    decode_token,
    session_mfa_satisfied,
)


def test_development_mfa_skip_defaults_off():
    configured = Settings(_env_file=None)
    assert configured.development_mfa_skip is False
    assert not configured.development_mfa_skip_for("admin")


@pytest.mark.parametrize("environment", ["staging", "production"])
def test_deployed_environments_refuse_mfa_skip(environment):
    with pytest.raises(ValidationError, match="for development only"):
        Settings(
            _env_file=None,
            environment=environment,
            development_mfa_skip=True,
            rate_limit_storage_uri="redis://localhost:6379",
        )


@pytest.mark.parametrize("role", ["superadmin", "student", "unknown", ""])
def test_skip_never_authorizes_other_roles(monkeypatch, role):
    monkeypatch.setattr(settings, "development_mfa_skip", True)
    claims = {"role": role, "mfa": False, "development_mfa_skipped": True}
    assert not session_mfa_satisfied(claims)
    token = create_access_token(uuid.uuid4(), claims=claims)
    with pytest.raises(InvalidAuthTokenError):
        decode_token(token, "access")


@pytest.mark.parametrize("role", ["admin", "faculty"])
def test_skip_tokens_are_rejected_after_disable_and_outside_development(
    monkeypatch, role
):
    monkeypatch.setattr(settings, "development_mfa_skip", True)
    claims = {"role": role, "mfa": False, "development_mfa_skipped": True}
    token = create_access_token(uuid.uuid4(), claims=claims)
    assert session_mfa_satisfied(decode_token(token, "access"))
    monkeypatch.setattr(settings, "development_mfa_skip", False)
    with pytest.raises(InvalidAuthTokenError):
        decode_token(token, "access")
    assert not session_mfa_satisfied(claims)
    monkeypatch.setattr(settings, "development_mfa_skip", True)
    monkeypatch.setattr(settings, "environment", "staging")
    with pytest.raises(InvalidAuthTokenError):
        decode_token(token, "access")
    assert not session_mfa_satisfied(claims)


@pytest.mark.parametrize("marker,mfa", [(False, False), (True, True), ("true", False)])
def test_invalid_skip_marker_cannot_be_used_as_verified_mfa(monkeypatch, marker, mfa):
    monkeypatch.setattr(settings, "development_mfa_skip", True)
    claims = {"role": "admin", "mfa": mfa, "development_mfa_skipped": marker}
    assert not session_mfa_satisfied(claims)
    with pytest.raises(InvalidAuthTokenError):
        decode_token(create_access_token(uuid.uuid4(), claims=claims), "access")
