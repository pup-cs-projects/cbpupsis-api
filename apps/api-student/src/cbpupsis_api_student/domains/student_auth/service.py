"""Student sign-in rules (US-101): student number, birthdate, and password.

Identity lookup goes through the users client; the lockout, the sign-in
ledger, sessions, and tokens through the auth client's shared role sign-in.
What stays here is the student rule: which identifier, the birthdate check,
and the one identical failure message.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_api_student.domains.student_auth.schemas import (
    StudentLoginRequest,
    StudentTokenPair,
)
from cbpupsis_shared.domains.auth import client as auth_client
from cbpupsis_shared.domains.auth.constants import (
    INSTITUTIONAL_ID_PATTERN,
    ROLE_STUDENT,
    STUDENT_ID_FORMAT_MESSAGE,
    STUDENT_SIGN_IN_FAILED_MESSAGE,
)
from cbpupsis_shared.domains.auth.exceptions import (
    IdentifierFormatInvalidError,
    IdentifierSignInFailedError,
    InvalidAuthTokenError,
)
from cbpupsis_shared.domains.auth.schemas import RefreshRequest
from cbpupsis_shared.domains.users import client as users_client


async def login_student(
    db: AsyncSession,
    data: StudentLoginRequest,
    *,
    ip_address: str | None,
    device_info: str,
) -> StudentTokenPair:
    """Verify a student's credentials and open a student session.

    Wrong student number, wrong birthdate, wrong password, and an inactive
    account all fail identically; the 5th failure in the window locks the
    student number (423) whether or not it belongs to an account.
    """
    number = data.student_number
    if not INSTITUTIONAL_ID_PATTERN.match(number):
        raise IdentifierFormatInvalidError(STUDENT_ID_FORMAT_MESSAGE)

    subject = await users_client.get_student_sign_in(db, number)

    async def reject(_subject_id: uuid.UUID, password_ok: bool) -> str | None:
        return _failure_reason(subject, data, password_ok)

    subject_id = await auth_client.verify_identifier_password(
        db,
        identifier=number,
        subject=subject,
        password=data.password,
        ip_address=ip_address,
        device_info=device_info,
        reject=reject,
        failure=IdentifierSignInFailedError(STUDENT_SIGN_IN_FAILED_MESSAGE),
    )
    pair = await auth_client.complete_sign_in(
        db,
        subject_id,
        role=ROLE_STUDENT,
        attempted_id=number,
        ip_address=ip_address,
        device_info=device_info,
        mfa=False,
    )
    return auth_client.expiring_pair(StudentTokenPair, pair)


def _failure_reason(
    subject, data: StudentLoginRequest, password_ok: bool
) -> str | None:
    """Name the failed check for the ledger only; the caller sees one message."""
    if subject is None:
        return "unknown_student_number"
    if not subject.is_active:
        return "inactive_account"
    if subject.birthdate is None or subject.birthdate != data.birthdate:
        return "wrong_birthdate"
    if not password_ok:
        return "wrong_password"
    return None


async def refresh_student(
    db: AsyncSession, request: RefreshRequest
) -> StudentTokenPair:
    """Rotate a student session's refresh token."""
    claims = auth_client.decode_refresh_session(request.refresh_token)
    if claims.get("role") != ROLE_STUDENT:
        raise InvalidAuthTokenError
    pair = await auth_client.refresh_role_session(
        db, request.refresh_token, session_claims={"role": ROLE_STUDENT}
    )
    return auth_client.expiring_pair(StudentTokenPair, pair)


async def logout_student(db: AsyncSession, request: RefreshRequest) -> None:
    """End the student session and revoke its refresh token. Idempotent."""
    await auth_client.revoke_refresh_token(db, request.refresh_token)
