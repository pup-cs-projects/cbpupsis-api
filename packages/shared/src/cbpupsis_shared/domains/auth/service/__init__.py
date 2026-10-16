"""The auth domain's service layer: this package, with its facade here.

Callers import ``cbpupsis_shared.domains.auth.service`` (other domains go through
``client.py``), never a module inside it. The rules live in the modules of this
package, split by the seam each one owns:

- ``accounts``: account creation, temporary passwords, and the generic
  email-and-password sign-in
- ``sessions``: server-side session rows, the idle timeout, account gates
- ``tokens``: token pairs, refresh rotation with reuse detection, revocation
- ``lockout``: the failed-step counter and lock shared by every role
- ``mfa``: challenges, TOTP, WebAuthn storage, recovery codes
- ``verification``: one-time tokens and email verification
- ``passwords``: password checks, recovery, and change
- ``role_sign_in``: the challenge-and-session flow every role sign-in shares

No SQL lives in any of them: the queries are in ``repository.py``. They keep the
transaction boundaries, because in this domain they *are* security decisions.

**Why user rows are unannotated.** Several functions take or return the row
``cbpupsis_shared.domains.users.service`` hands back. Importing
``users.models.User`` for an annotation is the boundary violation this domain is
not allowed to make, and ``UserRead`` is not a substitute because auth needs
``password_hash``, which that DTO omits on purpose. The rows stay opaque: read
for the attributes the flows need, never queried, written, or constructed here.
"""

from __future__ import annotations

from cbpupsis_shared.domains.auth.service.accounts import (
    TEMPORARY_PASSWORD_EVENT,
    create_account,
    create_accounts,
    create_console_account,
    login,
    register,
    reissue_temporary_password,
)
from cbpupsis_shared.domains.auth.service.lockout import (
    clear_sign_in_failures,
    record_sign_in_failure,
    require_not_locked,
)
from cbpupsis_shared.domains.auth.service.mfa import (
    begin_totp_enrollment,
    bind_mfa_challenge,
    enrolled_factors,
    get_webauthn_factor,
    issue_recovery_codes,
    record_webauthn_use,
    redeem_recovery_code,
    require_bound_challenge,
    retire_mfa_challenge,
    stage_webauthn_factor,
    verify_totp,
    webauthn_credential_owner,
)
from cbpupsis_shared.domains.auth.service.passwords import (
    change_password,
    check_password,
    forgot_password,
    record_request_refusal,
    reset_password,
    verify_reset_token,
)
from cbpupsis_shared.domains.auth.service.role_sign_in import (
    complete_sign_in,
    expiring_pair,
    open_challenge,
    record_attempt,
    refuse_expired_temporary_password,
    verify_identifier_password,
)
from cbpupsis_shared.domains.auth.service.sessions import (
    check_session,
    check_superadmin_session,
    require_superadmin_actor,
    stage_session,
)
from cbpupsis_shared.domains.auth.service.tokens import (
    issue_role_session,
    logout,
    refresh,
    revoke_all_for_user,
    stage_revoke_all_for_user,
)
from cbpupsis_shared.domains.auth.service.verification import (
    resend_verification,
    verify_email,
)

__all__ = [
    "TEMPORARY_PASSWORD_EVENT",
    "begin_totp_enrollment",
    "bind_mfa_challenge",
    "change_password",
    "check_password",
    "check_session",
    "check_superadmin_session",
    "clear_sign_in_failures",
    "complete_sign_in",
    "create_account",
    "create_accounts",
    "create_console_account",
    "enrolled_factors",
    "expiring_pair",
    "forgot_password",
    "get_webauthn_factor",
    "issue_recovery_codes",
    "issue_role_session",
    "login",
    "logout",
    "open_challenge",
    "record_attempt",
    "record_request_refusal",
    "record_sign_in_failure",
    "record_webauthn_use",
    "redeem_recovery_code",
    "refresh",
    "refuse_expired_temporary_password",
    "register",
    "reissue_temporary_password",
    "require_bound_challenge",
    "require_not_locked",
    "require_superadmin_actor",
    "resend_verification",
    "reset_password",
    "retire_mfa_challenge",
    "revoke_all_for_user",
    "stage_revoke_all_for_user",
    "stage_session",
    "stage_webauthn_factor",
    "verify_email",
    "verify_identifier_password",
    "verify_reset_token",
    "verify_totp",
    "webauthn_credential_owner",
]
