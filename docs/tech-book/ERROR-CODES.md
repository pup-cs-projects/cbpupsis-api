# Error codes

<!--
The registry of every error this API returns. It is a contract: the frontend
renders these messages and branches on these statuses, so an undocumented error
is a broken contract even when the code works.

Add the row in the same commit as the code that raises it.
-->

| | |
|---|---|
| **Last updated** | 2026-09-28 |
| **Source** | `cbpupsis_core/exceptions.py`, `cbpupsis_core/middleware/rate_limit.py` (in `packages/core`), and each domain's `exceptions.py` (`auth`, `users`, `iam`, `notifications` in `packages/shared`; `items` in `apps/api-student`; `admin_auth`, `superadmin_ops` in `apps/api-admin`) |

## The envelope

Every error response has the same shape, so clients parse one thing:

```json
{
  "detail": "Human-readable message, rendered to the user as-is.",
  "request_id": "0188d021-c9c1-4fe6-bfa3-050b5fc268cf"
}
```

`request_id` matches the `X-Request-ID` response header and every log line for
that request. **Include it in bug reports** — it is what makes an opaque 500
traceable.

Validation errors (422) carry the field-level errors from Pydantic in `detail`
instead of a string.

### The optional `code` field

A few errors add a third key:

```json
{
  "detail": "Verify your email address to continue.",
  "request_id": "0188d021-c9c1-4fe6-bfa3-050b5fc268cf",
  "code": "email_not_verified"
}
```

`code` is a **stable machine-readable identifier**, present only on errors the
client must *act* on rather than merely display. `detail` is prose for a human
and may be reworded at any time; `code` is contract and may not.

**Branch on `code`, never on `detail`.** The key is absent — not null — on
errors that do not define one, so every other error response is byte-identical
to what it was before the field existed. The codes in use are listed in the
registry below.

## Choosing a status

| Status | Class | Use when |
|---|---|---|
| 400 | `AppError` | the request is malformed in a way validation did not catch |
| 401 | `UnauthorizedError` | no credentials, or they are invalid or expired |
| 403 | `ForbiddenError` | authenticated, but not allowed to do this |
| 404 | `NotFoundError` | the resource does not exist **or the caller may not know it does** |
| 409 | `ConflictError` | the request conflicts with existing state (duplicate, already-consumed token) |
| 422 | — | the body failed schema validation (raised by FastAPI, not by you) |
| 429 | — | the caller exceeded a rate limit (raised by slowapi, rendered by our handler) |

**404 over 403 for someone else's resource.** Returning 403 confirms the id
exists, which is an enumeration oracle. Reserve 403 for a resource the caller can
see but may not act on.

## Writing the message

The frontend renders `detail` verbatim to a real person. So:

- **Plain language with a next step.** *"This link has expired. Request a new one
  to continue."* — not *"token_expired"*, not *"FK violation on user_id"*.
- **Never leak internals.** No table names, no SQL, no stack traces, no ids the
  user has no use for.
- **Identical messages where distinguishing them leaks.** Unknown email and wrong
  password must return the same 401, or the endpoint enumerates accounts.

## Registry

<!--
One row per distinct error. Group by domain. The "raised in" column is what makes
this maintainable: it is how you find the code when the copy needs changing.
-->

### auth

| Status | Message | When | Raised in |
|---|---|---|---|
| 401 | Invalid authentication token | Signature, expiry, or token-type check failed. Deliberately identical for all three. | `auth/security.py` |
| 401 | Not authenticated | No `Authorization` header. | `auth/dependencies.py` |
| 401 | Incorrect email or password | Login failed. Identical for unknown account, wrong password, and inactive/deleted user. | `auth/service.py` |
| 403 | Verify your email address to continue… (`code: email_not_verified`) | Login or refresh with a correct password, but the address is unverified. **Only reachable after the password check passes**, so it cannot be used to enumerate accounts — see below. | `auth/service.py` |
| 401 | User is no longer active | The account was deactivated after the token was issued. | `auth/dependencies.py` |
| 401 | Refresh token already used | A rotated token was replayed. Revokes the user's whole token family. | `auth/service.py` |
| 409 | Email already registered | Registration hit the unique constraint on `users.email`. | `auth/service.py` |
| 429 | Too many requests. Please try again in N seconds. (`code: rate_limited`) | A rate limit was exceeded on `/login`, `/register`, `/forgot-password`, or `/resend-verification`. Carries a `Retry-After` header, in seconds. | `core/exceptions.py` |

**Why the 403 is safe.** It sits *after* the password check, so it only ever
tells a caller who already holds valid credentials that their own address is
unverified. An attacker probing an email list without the password sees the
identical 401 above, exactly as before. Wrong password on an unverified account
returns 401, never 403 — that is the enumeration guarantee, and
`tests/domains/auth/test_email_flows.py` asserts it explicitly.

Clients should branch on `code == "email_not_verified"` and route the user to a
resend-verification screen rather than back to the login form.

### admin_auth

| Status | Code | When |
|---|---|---|
| 401 | `AUTH_MFA_INVALID` | The Google Authenticator code or hardware-key assertion is invalid. |
| 401 | `AUTH_MFA_CODE_REUSED` | A TOTP time-step already completed an earlier challenge. |
| 401 | `AUTH_MFA_REQUIRED` | An Admin challenge was presented where a completed session is required. |
| 403 | `AUTH_MFA_ENROLLMENT_REQUIRED` | The Admin has not yet enrolled a factor; only enrollment is reachable. |
| 403 | `AUTH_ADMIN_PROFILE_REQUIRED` | The Admin has no active position/scope profile. |
| 403 | `AUTH_INSUFFICIENT_ROLE` | The session is not a current MFA-completed Admin or Superadmin session for the requested route. |
| 401 | `AUTH_SESSION_EXPIRED` | The Superadmin server-side session is missing or idle for 15 minutes; refresh cannot revive it. |
| 404 | `RESOURCE_NOT_FOUND` | The record is absent or outside the Admin position's scope. |
| 423 | `AUTH_ACCOUNT_LOCKED` | Five failures in the rolling window locked both sign-in steps. Includes `Retry-After` and `retry_after_seconds`. |

### iam

| Status | Message | When | Raised in |
|---|---|---|---|
| 403 | Missing required permission(s): [...] | The caller lacks a permission the endpoint requires. Also raised by `users` when reading another profile, through the same class so the two cannot diverge. | `iam/exceptions.py` |
| 403 | Requires at least one of: [...] | The caller holds none of a set where any one would suffice. | `iam/exceptions.py` |
| 404 | Unknown permissions: [...] | A policy referenced an action that does not exist. | `iam/exceptions.py` |
| 404 | Permission {id} not found | `GET /iam/permissions/{id}` for an id that does not exist. | `iam/exceptions.py` |
| 404 | Policy {id} not found | `GET /iam/policies/{id}` for an id that does not exist. | `iam/exceptions.py` |
| 404 | Group {id} not found | `GET /iam/groups/{id}` for an id that does not exist. | `iam/exceptions.py` |
| 409 | Admin and Superadmin memberships are mutually exclusive | A privileged group assignment would put an account in both groups. | `iam/exceptions.py` |

### users

| Status | Message | When | Raised in |
|---|---|---|---|
| 404 | User {id} not found | No live user with that id. | `users/exceptions.py` |
| 422 | Missing required profile fields: [...] | Onboarding completed with required fields unset. | `users/exceptions.py` |
| 401 | Password is incorrect | Password confirmation failed on account deletion. | `users/exceptions.py` |
| 422 | A deleted account cannot be reactivated | Deletion scrubs identity, so there is nothing to restore. | `users/exceptions.py` |
| 422 | You cannot deactivate your own account through the admin endpoint… | An admin aimed `POST /users/{id}/deactivate` at themselves. Refused because it has no undo through the API: reactivating needs `ManageUser`, which they would just have lost. Self-deactivation is `POST /users/me/deactivate`. | `users/exceptions.py` |
| 403 | Protected administrative accounts require an explicit override | Ordinary deactivation targeted an Admin or Superadmin account. | `users/exceptions.py` |
| 409 | Account is already inactive | An override tried to deactivate an already-inactive Admin account. | `users/exceptions.py` |
| 403 | Missing required permission(s): ['ReadAllUser'] | `GET /users` without `ReadAllUser`. A listing is inherently a cross-ownership read, so unlike `GET /users/{id}` there is no self-service branch. | `users/exceptions.py` |
| 403 | Missing required permission(s): ['ManageUser'] | Admin deactivate/reactivate without `ManageUser`. Note `ReadAllUser` alone does **not** confer it: seeing every account must not imply suspending one. | `users/exceptions.py` |

### items

| Status | Message | When | Raised in |
|---|---|---|---|
| 404 | Item {id} not found | No live item with that id, **or** the caller may not read it — someone else's item without `ReadAllItem`. 404 rather than 403 on purpose: a 403 would confirm the id exists and make the endpoint an enumeration oracle. | `items/exceptions.py` |
| 403 | You do not have access to this item | The caller neither owns it nor holds `ModerateItem`. Reserved for **write** paths, where the caller is allowed to see the item but not act on it; where seeing it is itself privileged, the 404 above is raised instead. | `items/exceptions.py` |

### audit

| Status | Message | When | Raised in |
|---|---|---|---|
| 403 | Missing required permission(s): ['ReadAllAuditEntry'] | `GET /audit` without the permission. Deliberately **not** implied by `ManageIAM`: reading the trail and reshaping authorization are different powers, and an auditor should not be able to grant. | `iam/exceptions.py` |

The audit trail is append-only and has no write endpoint. Ordinary domain
events arrive through the outbox; Superadmin operations write one synchronous
row, with mutations staged in their business transaction.

### superadmin_ops

| Status | Code or message | When |
|---|---|---|
| 422 | `OVERRIDE_JUSTIFICATION_REQUIRED` | The override reason is blank; the target is unchanged. |
| 403 | `DUAL_AUTH_SAME_ACTOR` | The backup-restore requester tried to approve their own request. |
| 409 | Restore authorization is not pending | A request or approval conflicts with the current authorization state. |
| 404 | Overridable rule not found / Backup not found | The named rule or backup does not exist. |

### notifications

| Status | Message | When | Raised in |
|---|---|---|---|
| 404 | Notification {id} not found | No notification with that id, **or** it belongs to somebody else. 404 rather than 403 for the same reason as items: a 403 would confirm the id exists and let a caller learn how many notifications other accounts hold, and when they arrived. | `notifications/exceptions.py` |
| 422 | Unknown notification type: '...' | A preference named a type the application does not define. Refused rather than stored, because a preference that suppresses nothing would tell the user they had switched something off — a failure they only discover by not receiving something. | `notifications/exceptions.py` |
| 422 | '...' is a security notice and cannot be disabled | An attempt to switch off a non-optional type. Security notices (a password change, an account deactivation) are the signal that surfaces an account takeover to its victim, so neither a user nor an attacker holding their session may silence them. | `notifications/exceptions.py` |

Reading and clearing your **own** notifications carries no permission — it is
what having an account means, the same way `GET /users/me` is ungated. The
service scopes every query to the caller, so there is no id a client could
supply to reach somebody else's. `ReadAllNotification` and
`ManageNotificationDelivery` exist only for crossing that boundary.

There is deliberately **no endpoint that creates a notification.** One that
accepted a recipient and a body would let any authenticated caller forge a
message appearing to come from the platform. Notifications arrive through the
outbox.

### <your domain>

| Status | Message | When | Raised in |
|---|---|---|---|
| | | | |

## Adding a code

1. Raise the semantic error **from the service layer**, never from a router and
   never as a bare `HTTPException`. The service stays framework-agnostic; the
   handler translates at the edge.
   Raise a **named error from the domain's own `exceptions.py`** —
   `ItemNotFoundError(item_id)`, not `NotFoundError(f"Item {item_id} not found")`.
   The status and the wording then live in one place, so two raises of the same
   condition cannot drift into two different messages, and this registry has a
   single class to point at.
   Not from the repository either: a `get_X` that finds nothing returns `None`,
   and the service decides whether that absence is a 404, an enumeration-safe
   silent return, or nothing at all. A repository that raised would fix that
   choice for every caller, including jobs and scripts that have no HTTP status
   to return.
2. Add the row above in the same commit.
3. Add or extend the test asserting the status *and* the message.
4. If the frontend must branch on it, say so in the PR — a new error is a
   contract change.
