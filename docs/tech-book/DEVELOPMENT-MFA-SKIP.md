# Temporary development-only MFA skip

This opt-in switch lets **Admin and Faculty only** sign in with their normal
credentials without authenticator enrollment or a code. It is not a password
bypass, recovery method, or supported staging/production configuration.

## Enable

Set these values in the environment passed to each API being tested (normally
`env/admin/env.dev` and `env/faculty/env.dev`), then restart those API processes:

```dotenv
ENVIRONMENT=development
DEVELOPMENT_MFA_SKIP=true
```

The default is `false`. Staging/production refuse startup if the switch is on.
Use only on an isolated development stack with synthetic accounts/data. An
environment mislabeled as development is not made safe by this guard.

The existing `POST /api/v1/auth/admin/login` and
`POST /api/v1/auth/faculty/login` return their normal role-specific token pair
instead of an MFA challenge when enabled. Cookies are set as on MFA completion.
Clients must handle both response types, discriminating on `access_token`.
Superadmin and Student flows are unchanged. Account/profile/role/scope checks,
password expiry/change requirements, lockout, permissions and sessions still apply.

Skipped sessions carry signed `mfa: false` and `development_mfa_skipped: true`
claims in both tokens. Each skip records `auth.development_mfa_skipped` in the
audit ledger and emits a warning; the sign-in ledger records `mfa_used=false`.
Refresh preserves the marker and never upgrades it to verified MFA.
Existing authenticator secrets and recovery codes are not modified.

## Disable / remove

Set `DEVELOPMENT_MFA_SKIP=false` (or remove it) and restart **all** API/worker
processes sharing the environment/signing key. Marked access and refresh tokens
are then rejected, including on shared endpoints. New logins again require the
existing authenticator or first-time enrollment. Re-enabling the switch can make
unexpired marked tokens usable again; log out before disabling, or revoke those
sessions through the existing account/session controls if permanent revocation
is required. Remove this temporary feature branch when testing is finished.
