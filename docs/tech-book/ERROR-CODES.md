# Error codes

<!--
The registry of every error this API returns. It is a contract: the frontend
renders these messages and branches on these statuses, so an undocumented error
is a broken contract even when the code works.

Add the row in the same commit as the code that raises it.
-->

| | |
|---|---|
| **Last updated** | 2026-10-20 |
| **Source** | `cbpupsis_core/exceptions.py`, `cbpupsis_core/middleware/rate_limit.py` (in `packages/core`), and each domain's `exceptions.py`: `auth`, `users`, `iam`, `notifications` in `packages/shared`; `items` in `apps/api-student`; `admin_auth`, `superadmin_ops`, and `user_import` in `apps/api-admin`. The student, faculty, and sections domains raise the shared `auth` classes and add none of their own; `curriculum` raises none. |

## The envelope

Every known application error has the same body:

```json
{
  "status": "error",
  "message": "Human-readable message, rendered to the user as-is.",
  "detail": "Human-readable message, rendered to the user as-is.",
  "request_id": "0188d021-c9c1-4fe6-bfa3-050b5fc268cf"
}
```

`message` is an alias of `detail`. `request_id` matches the `X-Request-ID`
response header and every log line for that request. **Include it in bug
reports**: it is what makes an opaque 500 traceable.

Rate-limit refusals use the same envelope plus a `code` and a `Retry-After`
header in seconds:

```json
{
  "status": "error",
  "message": "Too many requests. Please try again in 60 seconds.",
  "detail": "Too many requests. Please try again in 60 seconds.",
  "request_id": "0188d021-c9c1-4fe6-bfa3-050b5fc268cf",
  "code": "RATE_LIMIT_EXCEEDED"
}
```

Password-reset refusals use `RATE_LIMIT_EXCEEDED`; other endpoint limits use
`rate_limited`.

Validation errors (422) raised by FastAPI carry the field-level errors from
Pydantic in `detail` instead of a string.

### The optional `code` field

A few errors add a `code` key. `code` is a **stable machine-readable
identifier**, present only on errors the client must *act* on rather than
merely display. `detail` is prose for a human and may be reworded at any time;
`code` is contract and may not.

**Branch on `code`, never on `detail`.** The key is absent, not null, on errors
that do not define one.

Some errors add further fields, listed in the **Extra fields** column: the
lockout's `retry_after_seconds`, and the weak-password `unmet_rules`.

## Choosing a status

| Status | Class | Use when |
|---|---|---|
| 400 | `AppError` | the request is malformed in a way validation did not catch |
| 401 | `UnauthorizedError` | no credentials, or they are invalid or expired |
| 403 | `ForbiddenError` | authenticated, but not allowed to do this |
| 404 | `NotFoundError` | the resource does not exist **or the caller may not know it does** |
| 409 | `ConflictError` | the request conflicts with existing state (duplicate, already-consumed token) |
| 422 | `ValidationError` | a business rule rejected well-formed input (FastAPI's own 422 covers schema errors) |
| 423 | `AccountLockedError` | the sign-in subject is temporarily locked |
| 429 | | the caller exceeded a rate limit (raised by slowapi, rendered by our handler) |

**404 over 403 for someone else's resource.** Returning 403 confirms the id
exists, which is an enumeration oracle. Reserve 403 for a resource the caller can
see but may not act on.

## Writing the message

The frontend renders `detail` verbatim to a real person. So:

- **Plain language with a next step.** *"This link has expired. Request a new one
  to continue."*, not *"token_expired"*, not *"FK violation on user_id"*.
- **Never leak internals.** No table names, no SQL, no stack traces, no ids the
  user has no use for.
- **Identical messages where distinguishing them leaks.** Unknown account and
  wrong password must return the same 401, or the endpoint enumerates accounts.

## Registry

Every table has the same columns. A blank **Code** means the error carries no
`code` key.

### auth (every role's sign-in, sessions, MFA, recovery)

| Status | Code | Message | When | Raised in |
|---|---|---|---|---|
| 401 | | Invalid authentication token | JWT signature, expiry, or token-type check failed; a challenge token presented where a session is required; a token without a session id. Identical for all. | `auth/security.py`, `auth/service/sessions.py`, `auth/service/tokens.py`, `auth/service/role_sign_in.py` |
| 401 | | Not authenticated | No `Authorization` header (or Admin cookie). | `auth/dependencies.py` |
| 401 | | Incorrect email or password | Email sign-in failed. Identical for unknown account, wrong password, inactive or deleted user, and accounts that must use a role sign-in (student, faculty, Admin, Superadmin). | `auth/service/accounts.py`, `admin_auth/service.py` |
| 401 | `AUTH_FAILED` | Invalid credentials. Please verify your Student ID, birthdate, and password. | Student sign-in failed. Byte-identical for unknown student number, wrong birthdate, wrong password, and inactive account. | `auth/exceptions.py` (raised by `student_auth/service.py`) |
| 401 | `AUTH_FAILED` | Invalid employee ID or password. | Faculty sign-in failed. Byte-identical for unknown employee id, wrong password, and inactive account or faculty profile. | `auth/exceptions.py` (raised by `faculty_auth/service.py`) |
| 401 | `AUTH_SESSION_EXPIRED` | Your session has ended. Sign in again. | The server-side session is missing or idle past `SESSION_IDLE_TIMEOUT_MINUTES` (15), for every role; or an account-wide revocation changed the session generation. The session and its refresh tokens are deleted. | `auth/service/sessions.py`, `auth/security.py` |
| 401 | | User is no longer active | The account was deactivated after the token was issued. | `auth/dependencies.py`, `auth/service/sessions.py`, `auth/service/tokens.py` |
| 401 | | Refresh token already used | A rotated token was replayed. Revokes the user's whole token family. | `auth/service/tokens.py` |
| 401 | `AUTH_TEMPORARY_PASSWORD_EXPIRED` | Your temporary password has expired. Ask the system administrator to issue a new one. | A sign-in (student, faculty, Admin, Superadmin, `/auth/login`) with a correct temporary password past `TEMPORARY_PASSWORD_TTL_DAYS`, or a password change from one. **Only reachable after the credential step passes**, so a wrong password still gets that role's identical 401. Recorded on the sign-in ledger; does not count toward the lockout. A Superadmin must reissue (`POST /users/{id}/reissue-temporary-password`). | `auth/service/role_sign_in.py`, `auth/service/passwords.py` |
| 401 | `AUTH_MFA_INVALID` | The security code or hardware-key response is invalid | A wrong TOTP code, an unknown or used recovery code, or an invalid hardware-key response. Counts toward the lockout. | `auth/service/mfa.py`, `admin_auth/service.py` |
| 401 | `AUTH_MFA_CODE_REUSED` | That security code has already been used; enter the current code | A TOTP time-step at or below the last accepted one. Counts toward the lockout. | `auth/service/mfa.py` |
| 401 | `AUTH_MFA_REQUIRED` | A second factor is required | An Admin challenge token presented where a completed session is required. | `admin_auth/dependencies.py` |
| 403 | `AUTH_MFA_ENROLLMENT_REQUIRED` | Set up multi-factor authentication to continue | Enrollment is not available or not finished: enrolling when TOTP is already enrolled, confirming without a pending seed, verifying before enrolling, or an Admin enrollment challenge presented as a session. | `auth/service/mfa.py`, `admin_auth/*`, `faculty_auth/service.py` |
| 403 | `AUTH_INSUFFICIENT_ROLE` | This action is not available to your role | The session is not a current MFA-completed session of the role the endpoint needs (student, faculty, Admin), or the faculty profile is inactive. | `auth/exceptions.py` (raised by each role's `dependencies.py`) |
| 403 | `SUPERADMIN_ROLE_REQUIRED` | A live MFA-completed Superadmin session is required | Live, verified, non-conflicting Superadmin MFA session required. Also every account-management route (`POST /users`, `POST /users/import`, `POST /users/{id}/reissue-temporary-password`) called with another role's session. | `auth/service/sessions.py`, `iam/dependencies.py` |
| 403 | `PASSWORD_CHANGE_REQUIRED` | Change your temporary password to continue. | **Every authenticated route, for every role,** while the account holds a Superadmin-issued temporary password, except `POST /auth/change-password` (which clears it) and the logout routes. Refresh still rotates; the tokens it mints get this 403 too. | `auth/service/sessions.py` |
| 403 | `email_not_verified` | Verify your email address to continue. Check your inbox, or request a new verification link. | Email sign-in or refresh with a correct password, but the address is unverified. **Only reachable after the password check passes**; see below. | `auth/service/sessions.py` |
| 404 | `RESOURCE_NOT_FOUND` | Resource not found. | The record is absent or outside the caller's reach: an Admin position's scope, or a course section not assigned to the faculty member. Identical for both cases. | `auth/exceptions.py` |
| 503 | `SERVICE_UNAVAILABLE` | The schedule is temporarily unavailable. Please retry shortly. | Student schedule/export data is invalid or exceeds its generation bounds, or document generation/database access fails or exceeds the ten-second export deadline. No partial attachment; `Retry-After: 30` and private/no-store. | `schedule/exceptions.py` in `apps/api-student` |
| 400 | `INVALID_EMAIL_ADDRESS` | Enter a valid email address. | A reset request contains a malformed email. | `auth/service/passwords.py` |
| 410 | `AUTH_RESET_TOKEN_EXPIRED` | The password reset link has expired; request a new one | A reset token older than its TTL was submitted. | `auth/service/passwords.py` |
| 410 | `AUTH_RESET_TOKEN_USED` | The password reset link has already been used; request a new one | An already-redeemed reset token was submitted. | `auth/service/passwords.py` |
| 422 | `AUTH_ID_FORMAT_INVALID` | Invalid Student ID format. | A student number fails `YYYY-NNNNN-XX-N`. Rejected before any lookup or password hash. | `auth/exceptions.py` (raised by `student_auth/service.py`) |
| 422 | `AUTH_ID_FORMAT_INVALID` | Invalid employee ID format. Expected format: YYYY-NNNNN-XX-N | An employee id fails the same format. Rejected before any lookup or password hash. | `auth/exceptions.py` (raised by `faculty_auth/service.py`) |
| 422 | `AUTH_PASSWORD_TOO_WEAK` | Password does not meet complexity requirements: ... | A new password failed complexity requirements, or a password change tried to keep the temporary password (`unmet_rules: ["different from the temporary password"]`). Extra field `unmet_rules`. | `auth/service/passwords.py` |
| 423 | `AUTH_ACCOUNT_LOCKED` | Account temporarily locked after repeated sign-in failures | **The 5th failed password or MFA step in 15 minutes answers 423 itself, for every role**, and every further step answers 423 until the lock ends (15 minutes). Correct credentials are refused during the lock, and refused attempts do not extend it. An identifier that matches no account locks the same way. Extra field `retry_after_seconds`; `Retry-After` header. A security notice is sent to the account through the outbox. | `auth/service/lockout.py` |
| 429 | `rate_limited` | Too many requests. Please try again in N seconds. | A rate limit was exceeded on a sign-in or `/resend-verification`. `Retry-After` header. | `core/exceptions.py` |
| 429 | `RATE_LIMIT_EXCEEDED` | Too many requests. Try again later or use a link already sent to you. | A reset request exceeded the IP or shared address allowance. `Retry-After` header. | `auth/service/passwords.py`, `core/exceptions.py` |

**Why the `email_not_verified` 403 is safe.** It sits *after* the password check,
so it only ever tells a caller who already holds valid credentials that their own
address is unverified. Wrong password on an unverified account returns 401, never
403; `tests/domains/auth/test_email_flows.py` asserts it explicitly. Accounts with
a role sign-in get the 401 before this check, so it cannot reveal them either.

**Why the 423 is safe.** Unknown identifiers lock through a keyed pseudonym of the
identifier, so a lock is no evidence that an account exists.

### admin_auth

| Status | Code | Message | When | Raised in |
|---|---|---|---|---|
| 403 | `AUTH_ADMIN_PROFILE_REQUIRED` | An administrator position must be assigned before sign-in | The Admin has no active, complete position profile. | `admin_auth/exceptions.py` |

For `POST /auth/admin/mfa/webauthn/enroll` and `.../confirm`, an expired or
consumed challenge is an invalid auth token, an already-enrolled account receives
`AUTH_MFA_ENROLLMENT_REQUIRED`, and a locked account receives
`AUTH_ACCOUNT_LOCKED`. An invalid registration response returns `AUTH_MFA_INVALID`
and counts toward the lockout without issuing a session.

### superadmin_ops

| Status | Code | Message | When | Raised in |
|---|---|---|---|---|
| 404 | | Unknown override rule | The override rule id is not one the API defines. | `superadmin_ops/exceptions.py` |
| 404 | | Backup not found | No backup with that id. | `superadmin_ops/exceptions.py` |
| 409 | | A restore request already exists | A restore was requested for a backup that already has one. | `superadmin_ops/exceptions.py` |
| 409 | | Restore request is not pending | Approval of a restore that is not pending. | `superadmin_ops/exceptions.py` |
| 403 | `DUAL_AUTH_SAME_ACTOR` | A different Superadmin must approve the restore | A restore requester tried to approve their own request. | `superadmin_ops/exceptions.py` |
| 422 | `OVERRIDE_JUSTIFICATION_REQUIRED` | Override justification is required | Override reason missing, empty, or whitespace-only. | `superadmin_ops/exceptions.py` |

### users

| Status | Code | Message | When | Raised in |
|---|---|---|---|---|
| 404 | | User {id} not found | No live user with that id, **or** someone else's profile on `GET /users/{id}` without the Superadmin role and `ReadAllUser`. Identical for both cases, so the response never confirms the id. | `users/exceptions.py` |
| 422 | | Missing required profile fields: [...] | Onboarding completed with required fields unset. | `users/exceptions.py` |
| 401 | | Password is incorrect | Password confirmation failed on account deletion. | `users/exceptions.py` |
| 422 | | A deleted account cannot be reactivated | Deletion scrubs identity, so there is nothing to restore. | `users/exceptions.py` |
| 422 | | You cannot deactivate your own account through the admin endpoint... | An admin aimed `POST /users/{id}/deactivate` at themselves. Refused because it has no undo through the API: reactivating needs `ManageUser`, which they would just have lost. Self-deactivation is `POST /users/me/deactivate`. | `users/exceptions.py` |
| 403 | | Missing required permission(s): ['ReadAllUser'] | `GET /users` without `ReadAllUser`. A listing is inherently a cross-ownership read, so unlike `GET /users/{id}` there is no self-service branch. | `users/exceptions.py` |
| 403 | | Missing required permission(s): ['ManageUser'] | Admin deactivate or reactivate without `ManageUser`. `ReadAllUser` alone does **not** confer it. | `users/exceptions.py` |
| 403 | `OVERRIDE_REQUIRED` | Admin suspension requires a justified override | `POST /users/{id}/deactivate` aimed at an Admin; that needs the Superadmin override instead. | `users/service.py` |
| 403 | `OVERRIDE_TARGET_INVALID` | Override target must be an Admin | The Admin suspension override was aimed at a non-Admin. | `users/service.py` |
| 409 | `AUTH_ROLE_CONFLICT` | Admin and Superadmin memberships are mutually exclusive | A user was added to both groups. | `iam/service.py` |
| 409 | `USER_ALREADY_EXISTS` | Email already registered | `POST /users` with an email some account already holds (case-insensitive, deleted accounts included). Extra field `field: "email"`. | `users/exceptions.py` |
| 409 | `USER_ALREADY_EXISTS` | Institutional ID already registered | `POST /users` with a student number or employee id its profile table already holds. Extra field `field: "institutional_id"`. | `users/exceptions.py` |
| 409 | `USER_ALREADY_EXISTS` | An account with these details already exists | A concurrent create won the unique constraint after the checks passed (`POST /users` or `POST /users/import`). Extra field `field: null`. | `users/exceptions.py` |
| 422 | `ACCOUNT_PROGRAM_UNKNOWN` | Unknown or inactive program code | `POST /users` for a student whose `program` names no active program. | `users/exceptions.py` |
| 422 | | (validation envelope) required for this role: ... | `POST /users` without a field the role requires (student: `institutional_id`, `birthdate`, `program`, `year_level`; faculty: `institutional_id`, `department`; chairperson: `department`; dean: `college`), or with an invalid value (institutional id not `YYYY-NNNNN-XX-N`, birthdate not in the past, `year_level` outside 1 to 6, unknown `role`). The standard request-validation 422. | `users/schemas.py` |
| 404 | | User {id} not found | `POST /users/{id}/reissue-temporary-password` for a missing, deleted, or deactivated account. | `users/service.py` |

### user_import (`POST /users/import`, admin app)

| Status | Code | Message | When | Raised in |
|---|---|---|---|---|
| 422 | `USER_IMPORT_INVALID` | The file was not imported; fix the listed rows and upload it again | **Nothing was created.** Extra field `errors`: every problem as `{"row", "column", "rule"}`, row numbers counting the header as row 1. Row problems: a missing or invalid field, a field the role requires, an unknown program, a wrong cell count, an email or institutional id duplicated in the file or already registered. File problems (`row` and `column` null, or row 1 for the header): not UTF-8, empty, no data rows, a missing, unknown, or repeated header column, more than `USER_IMPORT_MAX_ROWS` rows, broken CSV quoting. **No cell value is ever echoed.** | `user_import/exceptions.py` |
| 413 | `USER_IMPORT_TOO_LARGE` | The file is larger than N bytes | The upload exceeds `USER_IMPORT_MAX_BYTES`. Extra field `max_bytes`. | `user_import/exceptions.py` |

### iam

| Status | Code | Message | When | Raised in |
|---|---|---|---|---|
| 403 | | Missing required permission(s): [...] | The caller lacks a permission the endpoint requires. Also raised by `users` for `GET /users` and admin deactivate or reactivate, through the same class so the two cannot diverge. Reading another profile by id answers 404 instead. | `iam/exceptions.py` |
| 403 | | Requires at least one of: [...] | The caller holds none of a set where any one would suffice. | `iam/exceptions.py` |
| 404 | | Unknown permissions: [...] | A policy referenced an action that does not exist. | `iam/exceptions.py` |
| 404 | | Permission {id} not found | `GET /iam/permissions/{id}` for an id that does not exist. | `iam/exceptions.py` |
| 404 | | Policy {id} not found | `GET /iam/policies/{id}` for an id that does not exist. | `iam/exceptions.py` |
| 404 | | Group {id} not found | `GET /iam/groups/{id}` for an id that does not exist. | `iam/exceptions.py` |

### items

| Status | Code | Message | When | Raised in |
|---|---|---|---|---|
| 404 | | Item {id} not found | No live item with that id, **or** someone else's item without `ReadAllItem`. 404 rather than 403 on purpose: a 403 would confirm the id exists. | `items/exceptions.py` |
| 403 | | You do not have access to this item | `PATCH` or `DELETE` on someone else's item by a caller who holds `ReadAllItem` (so may see it) but not `ModerateItem`. Without `ReadAllItem` the same request answers the 404 above. | `items/exceptions.py` |

### audit

| Status | Code | Message | When | Raised in |
|---|---|---|---|---|
| 403 | | Missing required permission(s): ['ReadAllAuditEntry'] | `GET /audit` or `GET /audit/sign-in-attempts` without the permission. Deliberately **not** implied by `ManageIAM`: reading the trail and reshaping authorization are different powers. | `iam/exceptions.py` |

The audit trail and the sign-in ledger are append-only and have no write
endpoint at all. Entries arrive through the event bus, the outbox, and the
sign-in flows' own transactions; see `cbpupsis_shared/domains/audit/service.py`.

### notifications

| Status | Code | Message | When | Raised in |
|---|---|---|---|---|
| 404 | | Notification {id} not found | No notification with that id, **or** it belongs to somebody else. 404 rather than 403 for the same reason as items. | `notifications/exceptions.py` |
| 422 | | Unknown notification type: '...' | A preference named a type the application does not define. Refused rather than stored, because a preference that suppresses nothing would mislead the user. | `notifications/exceptions.py` |
| 422 | | '...' is a security notice and cannot be disabled | An attempt to switch off a non-optional type, such as a password change or an account lock. | `notifications/exceptions.py` |

Reading and clearing your **own** notifications carries no permission. The
service scopes every query to the caller, so there is no id a client could
supply to reach somebody else's. `ReadAllNotification` and
`ManageNotificationDelivery` exist only for crossing that boundary.

There is deliberately **no endpoint that creates a notification.** Notifications
arrive through the outbox.

### Removed in this revision

These were returned by earlier builds and no longer are. Clients branching on
them must move to the rows above.

| Was | Now |
|---|---|
| Student 423 `ACCOUNT_LOCKED` with `data.retry_after_seconds` | 423 `AUTH_ACCOUNT_LOCKED` with top-level `retry_after_seconds` |
| Faculty 400 `INVALID_INPUT` | 422 `AUTH_ID_FORMAT_INVALID` |
| Faculty 401 `AUTH_FAILED` "... {n} attempts remaining before lockout." | 401 `AUTH_FAILED` "Invalid employee ID or password." |
| Faculty 401 `AUTH_MFA_INVALID` "The code has expired..." and other faculty-only MFA wordings | 401 `AUTH_MFA_INVALID` with the shared message |
| Faculty 403 `PERMISSION_DENIED` (audit deletion refused) | 405: the ledger has no write endpoint |
| Faculty 503 `SERVICE_UNAVAILABLE` | The generic 500 with a `request_id` |
| Student 401 "Your session has expired due to inactivity." | 401 `AUTH_SESSION_EXPIRED` "Your session has ended. Sign in again." |
| Admin 404 "Resource not found" (no period) | 404 `RESOURCE_NOT_FOUND` "Resource not found." |
| 403 `Missing required permission(s): ['ReadAllUser']` (or 403 `SUPERADMIN_ROLE_REQUIRED`) on `GET /users/{id}` for another user's profile | 404 `User {id} not found` |
| 403 `You do not have access to this item` on `PATCH` or `DELETE /items/{id}` for someone else's item, without `ReadAllItem` | 404 `Item {id} not found` |
| 422 `INVALID_CLIENT_ADDRESS` on every sign-in route (student, faculty, admin, superadmin, `/auth/login`) | No error: the sign-in proceeds and the session and sign-in ledger record a NULL address |
| `POST /auth/register` (201, 409 "Email already registered", 429 `rate_limited`) | 404: there is no public registration. A Superadmin creates accounts with `POST /users` or `POST /users/import` |
| 409 "Email already registered" (no code) on `POST /users` | 409 `USER_ALREADY_EXISTS` with `field` |
| `POST /users` body `{email, password, full_name}` | The account fields (`first_name`, `surname`, `email`, `role`, and the role's fields); the system generates the password |

## Adding a code

1. Raise the semantic error **from the service layer**, never from a router and
   never as a bare `HTTPException`. Raise a **named error from the domain's own
   `exceptions.py`** (or a shared `auth` class when every role means the same
   thing), not `NotFoundError(f"...")` at the call site. A `get_X` that finds
   nothing returns `None`; the service decides what that absence means.
2. Add the row above in the same commit.
3. Add or extend the test asserting the status *and* the message.
4. If the frontend must branch on it, say so in the PR: a new error is a
   contract change.
