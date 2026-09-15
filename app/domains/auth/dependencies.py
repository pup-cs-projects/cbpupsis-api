"""Authentication dependencies: turn a request into a known user.

The flow is: extract the bearer token, verify its signature and type, read the
``sub`` claim, then load the user row.

Authorization — what the user may *do* — is deliberately not handled here; that
lives in ``app.domains.iam.dependencies``. This module answers only "who is
this?", and the split is what lets permissions change without touching
authentication.

**Email verification does not gate login.** An unverified user authenticates
normally and gets a full token pair. Blocking login outright is a worse trade
than it looks: it strands every user whose verification mail bounced or landed
in spam, and it makes the login endpoint report whether an address is registered
*and* unverified. Instead, verification is enforced where it actually matters,
by declaring :func:`require_verified_email` on the specific endpoints that
should not run for an unproven address.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.domains.auth.exceptions import (
    InactiveUserError,
    NotAuthenticatedError,
    UnverifiedEmailError,
)
from app.domains.auth.security import decode_token
from app.domains.users import service as users_service

# auto_error=False so a MISSING header reaches our own code instead of raising
# Starlette's 403. A missing credential is 401 ("who are you?"), not 403 ("I
# know who you are and you may not"); the default conflates the two.
_bearer_scheme = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class CurrentUser:
    """The authenticated principal, as consumed by endpoints and guards.

    Deliberately minimal: the local user id plus identity attributes cheap to
    carry. Permissions are resolved separately, on demand, so they always
    reflect current state rather than state at token-issue time.
    """

    id: uuid.UUID
    email: str


async def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
    db: AsyncSession = Depends(get_db),
) -> CurrentUser:
    """Resolve the current user from a verified access token.

    The user row is loaded on every request rather than trusted from the token,
    so deactivating or deleting an account takes effect immediately instead of
    when the token happens to expire.
    """
    if credentials is None:
        raise NotAuthenticatedError
    claims = decode_token(credentials.credentials, expected_type="access")
    user = await users_service.get_active_user(db, uuid.UUID(claims["sub"]))
    if user is None:
        raise InactiveUserError

    # Published for the rate limiter, which keys authenticated callers by
    # identity rather than by address so users behind one NAT do not consume
    # each other's allowance. Set only after the token is verified and the
    # account confirmed live, so the value is never attacker-controlled.
    request.state.user_id = str(user.id)
    return CurrentUser(id=user.id, email=user.email)


async def require_verified_email(
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CurrentUser:
    """Authenticate, and additionally require a verified email address.

    Declare this instead of :func:`get_current_user` on endpoints that should
    not run for an unproven address — anything that emails other people, spends
    money, or is expensive to undo::

        @router.post("/invitations")
        async def invite(user: CurrentUser = Depends(require_verified_email)):
            ...

    Raises :class:`ForbiddenError` (403), not 401: the caller *is* authenticated,
    and a 401 would tell a client to discard a perfectly good token and send the
    user back through login, which would not fix anything.

    The verification state is read from the database per request rather than
    from the token, so verifying takes effect on the next call instead of when
    the access token happens to expire.
    """
    record = await users_service.get_by_id(db, user.id)
    if record.email_verified_at is None:
        raise UnverifiedEmailError("Email address not verified")
    return user
