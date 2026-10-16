"""WebAuthn ceremonies for Admin hardware keys.

The factor primitives every role shares (TOTP, challenge tokens, secret
encryption) live in ``cbpupsis_shared.domains.auth``; only the Admin role
offers hardware keys, so the WebAuthn library stays in this app.
"""

from __future__ import annotations

import base64
import json
import uuid
from typing import Any

from webauthn import (
    base64url_to_bytes,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers.exceptions import InvalidAuthenticationResponse, WebAuthnException
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    UserVerificationRequirement,
)

from cbpupsis_core.config import settings


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def webauthn_registration_options(
    *, user_id: uuid.UUID, email: str, challenge: str
) -> dict[str, Any]:
    """Build browser registration options bound to the password challenge."""
    options = generate_registration_options(
        rp_id=settings.webauthn_rp_id,
        rp_name="CBPUPSIS",
        user_id=user_id.bytes,
        user_name=email,
        challenge=base64url_to_bytes(challenge),
        authenticator_selection=AuthenticatorSelectionCriteria(
            user_verification=UserVerificationRequirement.REQUIRED
        ),
    )
    return json.loads(options_to_json(options))


def verify_webauthn_registration(
    *, credential: dict[str, Any], challenge: str
) -> tuple[str, str, int] | None:
    """Verify browser registration before any credential material is stored."""
    try:
        verified = verify_registration_response(
            credential=credential,
            expected_challenge=base64url_to_bytes(challenge),
            expected_rp_id=settings.webauthn_rp_id,
            expected_origin=settings.webauthn_origin,
            require_user_verification=True,
        )
    except (WebAuthnException, ValueError, TypeError, KeyError):
        return None
    return (
        _base64url(verified.credential_id),
        _base64url(verified.credential_public_key),
        verified.sign_count,
    )


def verify_webauthn_assertion(
    *,
    assertion: dict[str, Any],
    challenge: str,
    credential_public_key: str,
    current_sign_count: int,
) -> int | None:
    """Verify a hardware-key assertion and return its next sign counter."""
    try:
        verification = verify_authentication_response(
            credential=assertion,
            expected_challenge=base64url_to_bytes(challenge),
            expected_rp_id=settings.webauthn_rp_id,
            expected_origin=settings.webauthn_origin,
            credential_public_key=base64url_to_bytes(credential_public_key),
            credential_current_sign_count=current_sign_count,
            require_user_verification=True,
        )
    except (InvalidAuthenticationResponse, ValueError, TypeError):
        return None
    return verification.new_sign_count
