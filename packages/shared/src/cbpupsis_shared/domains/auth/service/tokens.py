"""Token pairs, refresh rotation, logout, and revocation.

The refresh flow implements **rotation with reuse detection**, the standard
defence for tokens held by clients that cannot keep a secret (browsers, mobile
apps): every refresh invalidates the token presented and issues a new one, so a
stolen token has a short useful life, and a second use of an already-rotated
token is strong evidence of theft: the whole family is revoked in response.

This module alone owns JWT construction and the refresh-token ledger; role
domains own their credential checks.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_core.exceptions import UnauthorizedError
from cbpupsis_shared.domains.audit import client as audit_client
from cbpupsis_shared.domains.auth import repository
from cbpupsis_shared.domains.auth.constants import (
    IDENTIFIER_SIGN_IN_ROLES,
    ROLE_SUPERADMIN,
)
from cbpupsis_shared.domains.auth.exceptions import (
    InactiveUserError,
    InvalidAuthTokenError,
    RefreshTokenReusedError,
)
from cbpupsis_shared.domains.auth.schemas import TokenPair
from cbpupsis_shared.domains.auth.security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    require_session_version,
)
from cbpupsis_shared.domains.auth.service.sessions import (
    check_session,
    require_verified_email,
    stage_session,
)
from cbpupsis_shared.domains.iam import service as iam_service
from cbpupsis_shared.domains.iam.constants import SUPERADMIN_GROUP
from cbpupsis_shared.domains.users import service as users_service


async def refresh(
    db: AsyncSession,
    refresh_token: str,
    *,
    session_claims: dict[str, object] | None = None,
    commit: bool = True,
) -> TokenPair:
    """Rotate a refresh token, returning a fresh pair.

    Raises :class:`UnauthorizedError` if the token is invalid, expired, or has
    already been used. The last case also revokes every other token for that
    user, on the assumption that a replayed token means it leaked.

    Applies the same email-verification gate as login, and for the same reason:
    refresh mints access tokens, so leaving it ungated would let a session
    started before the rule (or before the address was un-verified) keep
    renewing itself indefinitely and walk straight around the block. The account
    state is re-read from the database on every rotation rather than trusted
    from the token, which is what makes the gate take effect immediately.
    """
    token_claims = decode_token(refresh_token, expected_type="refresh")
    if token_claims.get("role") and session_claims is None:
        # A role session rotates only through its own role's refresh endpoint,
        # which re-checks that role's live membership and scope first.
        raise InvalidAuthTokenError
    user_id = uuid.UUID(token_claims["sub"])
    if token_claims.get(
        "role"
    ) != ROLE_SUPERADMIN and await iam_service.is_user_in_group(
        db, user_id, SUPERADMIN_GROUP
    ):
        raise InvalidAuthTokenError

    user = await users_service.get_active_user_for_update(db, user_id)
    if user is not None:
        require_session_version(token_claims, user.session_version)
    record = await repository.get_refresh_token(db, token_claims["jti"])
    if record is None:
        raise InvalidAuthTokenError

    if record.revoked_at is not None:
        # Reuse of a rotated token: treat as compromise and cut off the family.
        await revoke_all_for_user(db, user_id)
        raise RefreshTokenReusedError

    repository.mark_refresh_token_revoked(db, record, datetime.now(UTC))
    await db.flush()

    # Confirm the account is still usable before minting new credentials.
    user = await users_service.get_active_user(db, user_id)
    if user is None:
        await db.commit()
        raise InactiveUserError
    if token_claims.get("role") != ROLE_SUPERADMIN:
        # After the revocation above, so an idle session's token dies too. The
        # Superadmin refresh checks its own session before calling here.
        await check_session(db, token_claims, allow_password_change=True)

    # Committed before the gate can raise, so the token presented here stays
    # revoked either way. Letting the rollback resurrect it would hand a
    # blocked account an endlessly retryable credential.
    if commit:
        await db.commit()
    if token_claims.get("role") not in IDENTIFIER_SIGN_IN_ROLES:
        # Student and faculty sign-in verify an institutional identifier, not
        # an email, so the email gate applies only to email-based sessions.
        require_verified_email(user)

    claims = {
        **(
            session_claims
            if session_claims is not None
            else carried_claims(token_claims)
        ),
        "sid": token_claims["sid"],
    }
    return await issue_pair(
        db,
        user_id,
        claims=claims,
        expected_session_version=token_claims.get("session_version", 0),
        commit=commit,
    )


async def logout(db: AsyncSession, refresh_token: str) -> None:
    """Revoke a single refresh token.

    Invalid or already-revoked tokens are accepted silently: logout should be
    idempotent, and reporting failure leaks whether a token was genuine.
    """
    try:
        claims = decode_token(refresh_token, expected_type="refresh")
    except UnauthorizedError:
        return
    user_id = uuid.UUID(claims["sub"])
    if claims.get("sid"):
        await repository.delete_active_sessions(db, user_id, uuid.UUID(claims["sid"]))
    if claims.get("role") == ROLE_SUPERADMIN:
        audit_client.stage(
            db,
            action="superadmin.logout",
            actor_id=user_id,
            target_type="session",
            target_id=claims["sid"],
            prior_state={"active": True},
            new_state={"active": False},
        )
    await repository.revoke_refresh_token(db, claims["jti"], datetime.now(UTC))
    await db.commit()


async def revoke_all_for_user(db: AsyncSession, user_id: uuid.UUID) -> int:
    """Revoke every live refresh token for a user.

    Used on suspected token theft, and the right call after a password change
    or account deactivation. Returns the count of revoked tokens.
    """
    count = await stage_revoke_all_for_user(db, user_id)
    await db.commit()
    return count


async def stage_revoke_all_for_user(db: AsyncSession, user_id: uuid.UUID) -> int:
    """Stage refresh-token revocation in a caller-owned transaction."""
    await users_service.stage_invalidate_sessions(db, user_id)
    count = await repository.revoke_all_refresh_tokens_for_user(
        db, user_id, datetime.now(UTC)
    )
    await repository.delete_active_sessions(db, user_id)
    return count


async def issue_role_session(
    db: AsyncSession,
    user_id: uuid.UUID,
    *,
    role: str | None,
    ip_address: str | None,
    device_info: str = "",
    mfa: bool = False,
    claims: dict[str, object] | None = None,
    commit: bool = True,
) -> TokenPair:
    """Open a server-side session and issue its token pair, for any role."""
    sid = stage_session(
        db, user_id=user_id, ip_address=ip_address, device_info=device_info
    )
    session_claims: dict[str, object] = {**(claims or {}), "sid": str(sid)}
    if role is not None:
        session_claims["role"] = role
    if mfa:
        session_claims["mfa"] = True
    return await issue_pair(db, user_id, claims=session_claims, commit=commit)


def carried_claims(claims: dict[str, object]) -> dict[str, object]:
    """Keep role/scope claims while dropping JWT control claims on rotation."""
    control = {"sub", "type", "iat", "exp", "jti", "session_version"}
    return {name: value for name, value in claims.items() if name not in control}


async def issue_pair(
    db: AsyncSession,
    user_id: uuid.UUID,
    *,
    claims: dict[str, object] | None = None,
    expected_session_version: int | None = None,
    commit: bool = True,
) -> TokenPair:
    """Mint a pair under the same account lock used by resets."""
    user = await users_service.get_active_user_for_update(db, user_id)
    if user is None:
        raise InactiveUserError
    if expected_session_version is not None:
        require_session_version(
            {"session_version": expected_session_version}, user.session_version
        )
    claims = {**(claims or {}), "session_version": user.session_version}
    access_token = create_access_token(user_id, claims=claims)
    refresh_token, jti, expires_at = create_refresh_token(user_id, claims=claims)

    repository.add_refresh_token(
        db,
        user_id,
        jti,
        expires_at,
        session_id=uuid.UUID(str(claims["sid"])) if claims.get("sid") else None,
    )
    if commit:
        await db.commit()

    return TokenPair(access_token=access_token, refresh_token=refresh_token)
