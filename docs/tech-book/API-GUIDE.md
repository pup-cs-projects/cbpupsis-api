# FastAPI Modular-Monolith Template

A production-ready FastAPI starter, organized by business domain, with
boundaries clean enough that any domain can be extracted into its own service
later. CBPUPSIS runs it as a uv workspace: three apps (student, faculty, admin),
each buildable as its own service, over a shared kernel that owns every model
and every migration. The same three apps also run as one process.

**Stack:** FastAPI · Pydantic v2 · SQLAlchemy 2.0 (async) · Neon Postgres ·
Alembic · JWT auth (argon2) · IAM-style RBAC in-database · uv · Docker.

## Quick start

```bash
mkdir -p env/student env/faculty env/admin
for app in student faculty admin; do cp env.example env/$app/env.dev; done
make setup                          # build, start, migrate, seed
make create-admin EMAIL=you@example.com   # after registering through the API
uv run python -m scripts.bootstrap_superadmin owner@example.com
```

Configuration lives in `env/<app>/env.<stage>`, one file per app and stage, never
committed. `make up env=dev` reads `env/student/env.dev`, `env/faculty/env.dev`,
and `env/admin/env.dev`; `env=staging` reads the `env.staging` file in each.
The migrate service and the worker read the admin file, and so does a host-side
script. `DATABASE_URL` and `JWT_SECRET` must be identical in all three files.
`DATABASE_URL` uses the restricted API role, while `DIRECT_DATABASE_URL` uses
the separate migration owner. Set `AUDIT_RUNTIME_ROLE` to the API role name.
`env.example` is the committed shape to copy
from, and `make up` refuses to start when any file for the chosen env is missing.

`make setup` brings up the three apps (student on :8001, faculty on :8002, admin
on :8003) and a local Postgres, generates the first migration if none exists,
applies it once through the `migrate` service, and seeds the RBAC baseline.
Then:

```bash
make help                    # every target
make up / down / logs        # lifecycle          (add wipe=1 to down to drop the DB)
make migrate                 # apply migrations   (migrate-status, migrate-create MSG="...")
make create-user EMAIL=a@b.com GROUP=Members
make check                   # lint + tests, before a PR
```

Every target takes `env=dev` (the default) or `env=staging`; staging runs the
same image production would. There is no `env=prod` on purpose — production is
built by CI and run by an orchestrator.

Also add `make install` if you want the local virtualenv (recommended: it is what
your editor and the test suite use). It also installs the git hooks.

## Linting, formatting, and hooks

**Ruff is the only linter and formatter.** It covers what autoflake, isort, flake8,
and black used to do between them, configured once under `[tool.ruff]` in
`pyproject.toml`. Do not add black or flake8 beside it — black and `ruff format`
disagree on real code, so `make fmt` and the commit hook would undo each other
on every run.

`.pre-commit-config.yaml` runs on each commit: file checks (YAML/TOML validity,
trailing whitespace, merge markers, LF endings), `ruff check --fix`,
`ruff format`, and `bandit` for security linting.

```bash
make install    # uv sync + pre-commit install
make hooks      # run every hook over the whole repo
```

Anything slow or network-bound stays in CI (`.github/workflows/ci.yml`): the test
suite, coverage, and `pip-audit` for dependency CVEs. A commit hook that reaches
the network is one people learn to skip with `--no-verify`.

Then verify everything works:

```bash
uv run pytest          # 455 tests
uv run ruff check .
```

## Granting access

Build bottom-up, attach top-down:

```
PERMISSION ──> POLICY ──> GROUP ──> USER
"CreateItem"  "ItemAuthor"  "Editors"   alice@corp.com
 an action     a bundle      a role      a person
```

```bash
# 1. Register and verify the first account, then bootstrap the Superadmin role:
uv run python -m scripts.bootstrap_superadmin owner@example.com
```

From there everything is API calls using a completed Superadmin MFA session:

```bash
POST   /api/v1/iam/permissions          {"action": "CreateItem"}
POST   /api/v1/iam/policies             {"name": "ItemAuthor",
                                         "permission_actions": ["CreateItem"]}
POST   /api/v1/iam/groups               {"name": "Editors"}
POST   /api/v1/iam/groups/{id}/policies {"policy_id": 1}
POST   /api/v1/iam/groups/{id}/users    {"user_id": "..."}

DELETE /api/v1/iam/groups/{id}/policies/{policy_id}
DELETE /api/v1/iam/groups/{id}/users/{user_id}

GET    /api/v1/iam/permissions          paginated; also /{id}
GET    /api/v1/iam/policies             paginated, with each policy's permissions
GET    /api/v1/iam/groups               paginated; also /{id}
GET    /api/v1/iam/users/{id}/permissions   effective set, split by grant path
```

Every one of them requires a current Superadmin session. The reads exist because a screen
cannot render a model it can only write to — and `GET /iam/users/{id}/permissions`
answers the question that actually gets asked, "why can this person do that?",
by reporting group-derived and directly-attached grants separately rather than
as one merged set.

Grants and revocations take effect on the **next request** — no re-login, no
token refresh — because permissions resolve from the database per request rather
than living in the JWT. That is the whole reason they are not token claims.

Ongoing changes are usually just group membership; new permissions only appear
when you add a genuinely new capability to the app.

## API documentation

Three UIs over the same schema, all **development-only**:

| Route | UI |
|---|---|
| `/scalar` | Scalar — the primary reference; modern, built-in API client |
| `/docs` | Swagger UI — quickest "Try it out" for a single endpoint |
| `/redoc` | ReDoc — dense three-panel reading view |

`/openapi.json` is gated too, not just the UIs: the schema itself enumerates
every endpoint, field, and constraint, and the UIs merely render it. Outside
development the routes are **not registered at all**, so there is nothing to
probe — verified in `tests/core/test_docs.py` and against a real production container.
The client-facing composed contract is exported to `docs/openapi.json` with
`uv run python -m scripts.export_openapi` after API changes.

Scalar runs with telemetry off and no proxy, so nothing typed into the docs —
bodies, headers, bearer tokens — reaches a third party.

## Admin panel

The legacy SQLAdmin panel is not mounted, even in development. Its direct table
writes bypass MFA, explicit overrides, and synchronous audit staging. Use the
Superadmin API for administrative changes; `/admin` returns 404.

## Structure

```
packages/                    # the shared kernel; every app depends on it
  core/src/cbpupsis_core/    #   no database: the bottom of the dependency graph
    config.py                #     pydantic-settings; validated at startup
    events.py                #     in-process event bus -> swap for a broker after a split
    exceptions.py            #     AppError hierarchy + handlers
    logging.py               #     JSON logs, request-id correlation
    pagination.py            #     Page[T] envelope
    emails/                  #     EmailSender protocol, SES and console backends, bodies
    middleware/              #     correlation-id, request logging, CORS; @rate_limit

  database/src/cbpupsis_database/
    base.py                  #   the declarative Base; UUID, timestamp, soft-delete mixins
    session.py               #   async engine and session (Neon-aware), get_db
    models/                  #   EVERY table, one module per domain; __init__ imports all

  shared/src/cbpupsis_shared/  # what every app needs
    application.py           #   create_app(): middleware, handlers, docs gate, probes
    routes.py                #   where each shared router is mounted, declared once
    domains/                 #   cross-role domains: schemas, repository, service, router
      auth/                  #     register/login/refresh/logout, argon2, JWT
      users/                 #     identity and profile
      iam/                   #     RBAC: permissions, policies, groups (served by admin)
      audit/                 #     the administrative trail (served by admin)
      notifications/         #     in-app + email notices, delivered via the worker
    outbox/                  #   transactional outbox: side effects that survive a crash
    worker.py                #   the outbox consumer; runs as its OWN process

  migrations/                # the one Alembic history; env.py imports cbpupsis_database.models

apps/                        # one folder per role; each builds as its own service
  api-student/src/cbpupsis_api_student/
    main.py                  #   uvicorn cbpupsis_api_student.main:app
    routes.py                #   the routers this app serves
    domains/items/           #   <-- the reference app-owned domain; copy this shape
      schemas.py             #       Pydantic DTOs = the domain's public data contract
      repository.py          #       ALL data access; returns ORM rows, holds no rules
      service.py             #       business logic, framework-agnostic = public interface
      client.py              #       the extraction swap point (local call now, HTTP later)
      router.py              #       HTTP only (thin): parse -> call service -> return
  api-faculty/               #   the shared routers only, until faculty features land
  api-admin/                 #   IAM, audit, and Superadmin operations

main.py                      # all three apps in one process: uvicorn main:app
scripts/seed_iam.py          # idempotent RBAC seed
scripts/seed_e2e.py          # verified accounts the HTTP suite logs in as

tests/                       # against in-memory SQLite; no database required
  conftest.py                #   shared fixtures (db, client, make_user, auth_headers)
  domains/<name>/            #   one folder per domain, wherever the domain lives
  core/                      #   kernel infrastructure, plus the conventions that span
                             #   the workspace (architecture, permission naming)
  apps/                      #   which app serves which routers; the composed union

bruno/                       # end-to-end HTTP tests; needs the RUNNING apps (see bruno/README.md)
```

The apps are what a client talks to; the kernel is what they share. Each app
lists the router mounts it serves, its `main.py` passes them to `create_app`,
and the root `main.py` passes the union of all three. Every mount is declared
once, so a path served by two apps is the same path in both, and in the
composed process.

### Why three kernel packages rather than one

`core` sits below `database` because the session reads its settings, and
`shared` sits above both because the outbox and the domains query the database.
Putting settings and the outbox in one package, as the natural split suggests,
would make that package and `database` depend on each other.
`tests/core/test_architecture.py` enforces the order.

## Starting a new feature

1. Add its table to `packages/database/src/cbpupsis_database/models/<name>.py`,
   and import that module in `models/__init__.py`, or autogenerate silently
   skips the table (a test catches this).
2. Put the other layers in `apps/api-<role>/src/cbpupsis_api_<role>/domains/<name>/`
   if one app owns the feature, or in `packages/shared/src/cbpupsis_shared/domains/<name>/`
   if every app needs it. Copy the shape of `items`.
3. Mount its router: a `RouterMount` in that app's `routes.py`, or, for a shared
   domain, one in `cbpupsis_shared/routes.py` added to the `MOUNTS` of each app
   that serves it.
4. Generate the migration: `make migrate-create MSG="add <name>"`, and read it.
5. Replace the example actions in `scripts/seed_iam.py` with the ones your app
   actually authorizes.

## The three rules

1. **Organize by domain, not by file type.** All of a feature's code in one
   folder, apart from its table, which lives with every other table in the
   kernel.
2. **Domains talk only through each other's `service.py`**, via `client.py` —
   never by importing another domain's models or querying its tables. Cross-domain
   ids are stored as bare UUIDs with no foreign key, because a hard FK is a
   coupling a service split cannot sever.
3. **Separate authentication from authorization.** Who you are (`auth`) is not
   what you may do (`iam`).

## The five layers

Each domain is split so that a change has one obvious home:

| Layer | Responsibility | Hard rule |
|---|---|---|
| `cbpupsis_database/models/<domain>.py` | ORM tables, in the kernel | every schema change gets a migration |
| `schemas.py` | request/response DTOs | never one class for both input and output |
| `repository.py` | builds and runs queries | no rules, no domain errors, no DTOs |
| `service.py` | rules, ownership checks, orchestration | never calls `session.execute` |
| `router.py` | HTTP wiring | dependencies plus one service call |

The repository/service boundary is the one worth stating precisely, because it
is easy to blur:

- A `get_X` that finds nothing returns **`None`**. The repository does not
  raise; converting that absence into a `NotFoundError` is the service's job.
  That is what lets a background job reuse the same lookup without catching an
  HTTP-shaped exception back out.
- **Commits live in the service.** The service knows when a unit of work is
  finished; a repository that committed per call would make a multi-step
  operation impossible to make atomic. Repositories may `flush()` when they
  need a generated id.

Method names are a contract, so a reader knows the shape without opening the
file: `get_X` (one row or None), `list_X` (returns `(rows, total)`), `add_X`,
`update_X`, `delete_X`, `count_X`, `exists_X`, `lock_X`.

## Authentication

Self-contained: argon2 password hashes, a short-lived access token (15 min) and
a long-lived refresh token (30 days) that **rotates on every use**. Reusing an
already-rotated token is treated as theft and revokes the user's whole token
family.

The user row is loaded on every authenticated request rather than trusted from
the token, so deactivating an account takes effect immediately instead of
whenever the token happens to expire.

To delegate to an external IdP (Auth0/Supabase/Cognito) later, replace
`decode_token` with a JWKS verify and swap `password_hash` for an `external_sub`
column. The authorization half is untouched either way.

### Account lifecycle endpoints

| Endpoint | Notes |
|---|---|
| `POST /auth/register` | Creates the account and mails a verification link. Rate limited. |
| `POST /auth/login` | Creates an ordinary session; Admin and Superadmin accounts use their dedicated MFA routes. |
| `POST /auth/verify-email` | Consumes a single-use token. |
| `POST /auth/resend-verification` | Always 204; invalidates any earlier token. Rate limited. |
| `POST /auth/forgot-password` | Always 204, registered or not. Rate limited. |
| `POST /auth/reset-password` | Consumes a token, then revokes **all** refresh tokens. |
| `POST /auth/change-password` | Needs the current password; returns a fresh pair. |
| `POST /auth/admin/login` | Verifies admin credentials and returns an MFA challenge, never a session. |
| `POST /auth/admin/mfa/verify` | Completes an admin challenge with TOTP or WebAuthn and sets the secure admin cookie. |
| `POST /auth/admin/mfa/totp/enroll` | Starts Google Authenticator-compatible TOTP enrollment and returns an `otpauth://` URI. |
| `POST /auth/admin/mfa/totp/confirm` | Proves the seed and creates the first admin session. |
| `POST /auth/admin/refresh` | Rotates an Admin refresh token while retaining role, position, and scope claims. |
| `POST /auth/admin/logout` | Revokes the Admin refresh token and clears the Admin cookie. |
| `GET /admin/me` | Requires a live MFA-completed Admin session and returns its current position and data scope. |
| `POST /auth/superadmin/login` | Verifies Superadmin credentials; returns only a challenge. |
| `POST /auth/superadmin/mfa/totp/enroll` | Starts encrypted TOTP enrollment from that challenge. |
| `POST /auth/superadmin/mfa/totp/confirm` | Confirms the TOTP seed and issues a bearer token pair. |
| `POST /auth/superadmin/mfa/verify` | Verifies enrolled TOTP or WebAuthn and issues a bearer token pair. |
| `POST /auth/superadmin/refresh` | Rotates the refresh token only while the same server-side session is active. |
| `POST /auth/superadmin/logout` | Revokes the refresh token and removes the active session. |
| `GET /superadmin/me` | Returns the current MFA-completed Superadmin identity. |
| `POST /superadmin/overrides/{rule_id}` | Requires `target_id` and nonblank `justification`; first rule is `users.deactivate_admin`. |
| `POST /superadmin/backups/{id}/restore-requests` | Marks an existing backup restore authorization pending; does not restore it. |
| `POST /superadmin/backups/{id}/restore-requests/approve` | A second, distinct active Superadmin approves authorization; does not execute restoration. |
| `PATCH /users/me` | Partial profile update (`extra="forbid"`). |
| `POST /users/me/complete-onboarding` | 422 listing whatever is still missing. |
| `POST /users/me/deactivate` | Reversible; revokes all refresh tokens. |
| `DELETE /users/me` | Needs the password; soft-deletes and scrubs PII. |
| `GET /users` | Superadmin-only, paginated. Tri-state `is_active` / `is_verified` filters. |
| `POST /users/{id}/deactivate` | Superadmin-only; refuses Admin and Superadmin targets. |
| `POST /users/{id}/reactivate` | Superadmin-only. 422 on a deleted (scrubbed) account. |
| `GET /audit` | Superadmin-only administrative trail, newest first. |
| `GET /ready` | Readiness probe: queries the database, 503 when it cannot. |

There is deliberately **no** admin delete: account deletion stays self-service
and password-confirmed, because an administrator cannot supply the password that
authorizes it.

### Email verification is required to log in

Login runs four checks, in this order, and the order is the security property:

| # | Condition | Result |
|---|---|---|
| 1 | Unknown email **or** wrong password | **401**, one identical message |
| 2 | Correct password, account inactive or deleted | **401**, the *same* message |
| 3 | Correct password, email not verified | **403**, `code: "email_not_verified"` |
| 4 | Correct password, verified | **200** with a token pair |

`/auth/refresh` applies the same gate at step 3, because refresh mints access
tokens: without it a session opened earlier would renew itself around the block
forever.

**The 403 does not enable account enumeration.** It is unreachable until the
password has already been verified, so it only tells a caller who *already holds
valid credentials* that their own address is unverified. An attacker probing an
email list without the password sees nothing but the identical 401 at step 1.
Wrong password on an unverified account returns 401, never 403 — the single most
important assertion in `tests/domains/auth/test_email_flows.py`.

Clients branch on `code`, never on the prose in `detail`, and send the user to a
resend-verification screen rather than back to the login form.

### Administrative MFA and scope

Membership in IAM's `Admins` group is one role. Each member additionally has a
`chairperson`, `dean`, or `registrar` position. Valid credentials return a
five-minute, non-privileged challenge rather than access/refresh tokens. Only a
valid RFC 6238 TOTP code (including one generated by Google Authenticator) or
WebAuthn assertion creates an admin session; its access and refresh JWTs carry
`role`, `position`, `department_id`, and `college_id` so downstream scope
queries do not perform another profile lookup after the pre-handler guard.
Google Authenticator is a TOTP client: the API generates the seed and standard
`otpauth://` URI and makes no Google service call.

TOTP seeds and WebAuthn credential material are stored only as AES-256-GCM
envelopes. A keyed credential-id digest enforces uniqueness without indexing
randomized ciphertext. WebAuthn verifies the RP id, origin, signed challenge,
user-verification flag, credential public key, and monotonic sign counter. The
completed access token is also set as
`admin_session` with `HttpOnly`, `Secure`, `SameSite=Lax`, and an
`/api/v1/admin` path. Admin refresh rotates that cookie, and logout deletes it.

The reusable `require_admin_session` dependency runs before an Admin handler.
It requires a signed token carrying the Admin role, completed MFA, a valid
position, an active matching profile, and current membership in `Admins`.
`GET /admin/me` is the first protected handler. Admin-owned Home, Calendar,
Courses, and Enrollment routes must declare the same dependency when those
domains land, and their repository queries use the returned position scope: a
Chairperson reaches their department, a Dean their college, and a Registrar the
university. A scope miss is `404 RESOURCE_NOT_FOUND`, never a confirming 403.

Admin MFA does **not** grant IAM, user-management, notification-administration,
or audit-reading permissions. Those are Superadmin capabilities and remain
outside the Admin role even though their shared routers are composed into the
same back-office API process.

### Superadmin sessions, overrides, and dual control

`Admins` and `Superadmins` are mutually exclusive IAM groups. Bootstrap only
verified, active existing accounts with `scripts.bootstrap_superadmin`; do not
put one account in both groups. Run the bootstrap separately for each of the two
named Superadmin accounts required by SRS 2.3. A Superadmin receives all currently registered
permission actions, including actions registered after bootstrap. User-management,
IAM, and audit-reading routes additionally require a live Superadmin session,
not merely a permission grant.

The Superadmin sign-in challenge uses the same encrypted TOTP/WebAuthn factors
and rolling five-failure lockout as Admin, but issues only bearer tokens. Its
JWTs carry `role=superadmin`, `mfa=true`, and a stable `sid`. Every authenticated
request in any app atomically checks and touches that `sid` in
`user_active_sessions`. A 15-minute idle gap deletes it and returns
`401 AUTH_SESSION_EXPIRED`; neither shared refresh nor the dedicated refresh
route can restore an expired session. Membership is checked live on privileged
requests. No Superadmin cookie or CSRF flow is used.

The generic override route accepts a rule id in the path and a JSON body with
`target_id` and `justification`. `users.deactivate_admin` is the first registered
rule. The ordinary user-deactivation route refuses administrative targets, so
the override cannot be bypassed. Blank or whitespace-only justification returns
`422 OVERRIDE_JUSTIFICATION_REQUIRED` without changing the account.

Backup restore is *authorization only*: one Superadmin marks an existing backup
pending, and another distinct, currently active Superadmin approves it. The
requester cannot approve their own request (`403 DUAL_AUTH_SAME_ACTOR`);
conflicting repeats return 409. Approval does not run a restore job.

Each authenticated Superadmin operation writes one audit row before returning,
including protected reads and session lifecycle. Explicit mutations stage their
row in the business transaction; the request audit fallback records other
protected activity without request bodies, personal data, or factor secrets.
The migration owner owns `audit_entries` and `admin_audit_trails`; the API role
has only SELECT and INSERT on those ledgers, not UPDATE, DELETE, or TRUNCATE.

Credential and factor failures share one rolling counter. The fifth failure in
15 minutes locks the account; subsequent calls return `423
AUTH_ACCOUNT_LOCKED`, `Retry-After: 900`, and `retry_after_seconds: 900`.
PostgreSQL serializes each account's sign-in transaction with an advisory lock,
so concurrent password and MFA failures cannot under-count the rolling window.

For endpoints that need verification *beyond* login — anything that mails other
people, spends money, or is expensive to undo — declare
`Depends(require_verified_email)` (in `auth/dependencies.py`). It returns 403 for
the same reason: the caller *is* authenticated, and sending them back to login
would not fix anything.

### Rate limiting

`slowapi` guards the unauthenticated surface. Limits live in `config.py` (not in
the decorators) so they can be retuned without a code change:

| Endpoint | Default | Why |
|---|---|---|
| `POST /auth/resend-verification` | 3/hour | sends mail to an address the caller names |
| `POST /auth/forgot-password` | 3/hour | same — mail-bombing a stranger, on your SES bill |
| `POST /auth/login`, `POST /auth/admin/login` | 10/minute | credential stuffing |
| `POST /auth/register` | 5/hour | signup spam |

Callers are keyed by **user id when authenticated, client IP otherwise**, so
several users behind one office NAT do not consume each other's allowance. Behind
a proxy the left-most `X-Forwarded-For` entry is used — safe only because the
Docker CMD runs uvicorn with `--proxy-headers --forwarded-allow-ips "*"`.

Refusals return **429** in the standard error envelope with a `Retry-After`
header, rather than slowapi's default body, which is a different shape and leaks
the configured limit.

**Limiting a new endpoint is two steps** — a settings field and a decorator:

```python
# packages/core/src/cbpupsis_core/config.py
rate_limit_export: str = "2/hour"

# the router
@router.post("/export")
@rate_limit("export")                    # reads settings.rate_limit_export
async def export(request: Request, ...):  # `request` is required by slowapi
    ...
```

`rate_limit("expot")` — a name with no matching settings field — raises
`UnknownRateLimitError` when the router module is imported, rather than serving
unlimited traffic quietly. Nothing in a response distinguishes an endpoint whose
limit never applies from one whose limit is generous, so the import-time check is
the only place that typo is visible.

The limit is a **route decorator, not middleware**, for the same reason
authorization is not middleware: middleware runs before routing resolves, so it
would need a URL-pattern-to-limit table — a second source of truth that fails
*open* whenever someone adds an endpoint and forgets the entry.

Note the decorator order: `@router.post` sits **above** `@rate_limit`. Reversed,
the route registers the undecorated function and the limit never runs.

Storage defaults to `memory://`, which means the limit is enforced **per
replica**. A multi-replica deployment that needs a global limit points
`RATE_LIMIT_STORAGE_URI` at Redis — configuration only, no code change. Tests set
`RATE_LIMIT_ENABLED=false`, because one limiter shared across a session makes
tests order-dependent.

### One-time tokens

Verification and reset share one `one_time_tokens` table with a `purpose`
discriminator, because their mechanics are identical and duplicating the table
would duplicate the redemption logic — the one part that must not have two
subtly different versions. A token presented to the wrong flow does not match on
purpose and is rejected.

Only a **SHA-256 digest** is stored; the raw 256-bit secret exists in the email
and nowhere else, so a database leak yields nothing replayable. A plain digest
rather than argon2 is correct here: the token carries full entropy, so there is
no dictionary to slow an attacker over. Redemption is a conditional
`UPDATE ... WHERE consumed_at IS NULL`, so two requests racing the same token
produce exactly one winner.

Every failure — unknown, expired, already used, wrong purpose — returns one
identical 401, so the endpoint cannot be used to probe which.

### What account deletion keeps

The row survives, because items, audit entries, and IAM grants reference the
user id and deleting it would orphan them. What survives is only what preserves
those references: the primary key, the timestamps, and `anonymized_at`.
Everything identifying (email, names, bio, avatar, phone, locale, timezone) is
scrubbed, and the password hash is overwritten with an unusable marker. The
email becomes `deleted+<uuid>@invalid` rather than NULL — the column is NOT NULL
and uniquely indexed, so several deletions would otherwise collide, and
`.invalid` is reserved by RFC 2606 so it can never route anywhere.

### Email delivery

`cbpupsis_core/emails/sender.py` picks a backend from configuration: **`SESEmailSender`** when
`SES_FROM_EMAIL` is set, **`ConsoleEmailSender`** otherwise — so development and
tests need no AWS credentials and cannot mail a real person by accident. boto3 is
synchronous, so every SES call goes through `asyncio.to_thread`; calling it
directly would block the event loop for the whole round-trip.

**Sending never fails a request.** A user who registered but whose email bounced
must still exist, so `send_email` logs the failure and returns `False`.

## Authorization

An AWS-IAM-shaped RBAC that lives in your database:

```
User --< user_groups >-- Group --< group_policies >-- Policy --< policy_permissions >-- Permission
User --< user_policies >------------------------------------------ Policy   (direct grant)
```

Business endpoints check **permissions, never group names**, so "who can do what" changes
by editing data rather than code. Permissions are resolved from the database on
each request — never baked into a token — so a revoked grant takes effect at
once. The administrative namespace additionally checks the single Admin role;
its signed position/scope claims describe data reach, not function permission.

### The permission vocabulary is fixed

Group names are yours; permission names are not. Every app calls its people something
different (`Cashiers`, `Teachers`, `Operators`), but underneath they do the same things
to a resource, so the verbs are a closed set:

| Verb | Ownership |
|---|---|
| `Create<X>` | n/a |
| `Read<X>` `Update<X>` `Delete<X>` | **own records only** |
| `ReadAll<X>` `Moderate<X>` `Manage<X>` | **crosses ownership** |

Policies follow `<Noun><Tier>`: `ItemReader`, `ItemAuthor`, `ItemModerator`, `ItemAdmin`.
Real state transitions keep their business verb (`ApproveRefund`, `PublishListing`).

That split is the two-level model expressed as names: the ordinary verbs always pair
with a service-layer ownership check, and the elevated three *are* what crossing
ownership looks like as a grant.

What `scripts/seed_iam.py` ships with — the `*Item` actions are the reference
domain's, to be replaced by your own; the rest are infrastructure and worth
keeping:

| Permission | Grants |
|---|---|
| `CreateItem` `ReadItem` `UpdateItem` `DeleteItem` | the caller's own items |
| `ReadAllItem` | **read** any item, not just your own |
| `ModerateItem` | update or delete any item |
| `ReadAllUser` | read any profile, and list users at all |
| `ManageUser` | deactivate or reactivate an account — deliberately not deletion |
| `ManageIAM` | reshape authorization itself |
| `ReadAllAuditEntry` | read the administrative trail |

Bundled into `ItemAuthor`, `ItemModerator`, `UserModerator`, `UserAdmin`,
`IAMAdmin`, and `AuditReader`, and attached to three groups: `Members` (ordinary
verbs only — the baseline a signed-up user gets), `Moderators`, and `Admins`.

Two separations there are deliberate. `ReadAllUser` does not imply `ManageUser`:
seeing every account must not confer suspending one. And `ReadAllAuditEntry` is
not part of `IAMAdmin`: an auditor who can see every grant should not be able to
make one, and an IAM admin should not silently gain the power to read who has
been watching them.

Never invent a synonym. `View`, `Get`, `List`, `Edit`, `Remove` all mean one of the
seven, and a codebase holding both `ReadItem` and `ViewItem` has a 403 in it that no
functional test catches. `tests/core/test_permission_naming.py` enforces this, along with
structural checks: no orphan permissions, no policy outside a group, no permission
enforced in code but missing from the seed.

The CBPUPSIS set is derived from the PRD in the permission model task of EP-01.

### Authorization has two levels, and you need both

```python
# Level 1 — endpoint dependency: "may this user update items at all?"
@router.patch("/{item_id}")
async def update_item(
    user: CurrentUser = Depends(require_permission("UpdateItem")),
): ...


# Level 2 — service layer: "may they update THIS item?"
await _require_owner_or_permission(db, item, user_id, "ModerateItem")
```

Level 2 needs the loaded row, so it cannot live in a dependency. Enforcing only
Level 1 is how applications ship IDOR bugs: every authenticated user holding a
generic permission could edit everyone else's records by changing an id in the
URL. `cbpupsis_api_student/domains/items/service.py` shows the pattern; `tests/domains/items/test_items.py`
covers it.

### Reads are a boundary too, not just writes

**Items are private by default.** `GET /items` returns only the caller's rows;
`GET /items/{id}` returns someone else's only to a holder of `ReadAllItem`, and
otherwise answers **404 rather than 403** — a 403 would confirm the id exists and
turn the endpoint into an enumeration oracle.

`mine=true` still works, but it is now a narrowing filter for a caller who can
already see everything, rather than the only thing standing between one user's
data and another's.

The same shape appears in `users`: `GET /users/{id}` allows a self-read or
`ReadAllUser`, and `GET /users` requires `ReadAllUser` outright because a listing
is inherently a cross-ownership read with no self-service branch to fall back on.

The lesson generalizes: an ownership check on write paths only is half a model.
A list endpoint that filters by nothing is the most common place it leaks, and it
leaks with a 200.

## Why authorization is not middleware

Middleware runs before routing resolves, so it only sees a path string — it
would need a URL-pattern table mapping routes to permissions, a second source of
truth that drifts the moment someone adds an endpoint and forgets the entry, and
fails *open* when it drifts. Dependencies put the requirement in the endpoint
signature, where it is visible, hard to forget, and documented in OpenAPI.

Middleware is right for concerns that do not depend on routing: correlation ids,
CORS, request logging. That is exactly what `core/middleware/http.py` holds — and
why `core/middleware/rate_limit.py`, which is per route, is a decorator instead.

## Neon specifics

- The app uses the **pooled** URL; Alembic uses the **direct** one. Neon's
  PgBouncer runs in transaction mode, so a connection returns to the pool after
  every transaction and session state is lost — `SET`, session advisory locks,
  and temp tables, all of which Alembic uses. The app's own queries are fine
  pooled: protocol-level prepared statements (what asyncpg uses) are supported;
  it is SQL-level `PREPARE` that is not.
- `pool_pre_ping=True` survives Neon's scale-to-zero, which drops idle
  connections after ~5 minutes.
- The pool is sized explicitly (`DB_POOL_SIZE`, `DB_MAX_OVERFLOW`,
  `DB_POOL_RECYCLE_SECONDS`) because the ceiling is **per worker**:
  `workers x (pool_size + max_overflow) <= the ceiling`. Which ceiling depends
  on the URL — through the pooler it is `default_pool_size` (90% of
  `max_connections`, counted **per user per database**), and on a direct
  connection it is `max_connections` itself, minus the 7 Neon reserves. Left
  implicit, nobody does that multiplication until the extra workers start
  failing at connect time under load. `env.example` carries the worked example.

## Testing

Tests run against in-memory SQLite, so `uv run pytest` needs no database and
each test starts from a clean schema. That trade is deliberate but not free:
SQLite does not enforce every Postgres behaviour identically, so anything
dialect-specific (JSONB operators, partial indexes) deserves an integration test
against a real Neon branch.

Services are framework-agnostic, so most logic is tested without HTTP at all —
one of the payoffs of the layering.

## Docker

Fastest way to run everything, including a local Postgres:

```bash
make up              # student :8001, faculty :8002, admin :8003, database :5432
```

Compose runs the three apps as three services, a one-shot `migrate` service
that applies migrations once before any app starts, the outbox worker, and the
database. It bind-mounts the source, so an edit on the host reloads the apps.

Nine images in [`docker/`](../../docker/): `<app>/Dockerfile.dev` (reload,
source bind-mounted), `<app>/Dockerfile.staging`, and `<app>/Dockerfile.prod`
(multi-stage, non-root, no dev deps), for each of `student`, `faculty`, and
`admin`. Each contains only its own app and the kernel packages. The admin
images also carry the migrations and `scripts/`, because the `migrate` service,
the worker, and the seed scripts run from them. Staging is an identical build to
production: only configuration differs, because a staging image that differs
cannot vouch for a release.

Build from the project root, since the context needs `pyproject.toml`,
`packages/`, and `apps/`:

```bash
docker build -f docker/student/Dockerfile.prod -t api-student .
```

See [`docker/README.md`](../../docker/README.md) for deployment, environment
variables, and how migrations should run in a deploy.

**Why keep `.venv` if everything runs in Docker?** It is the *development*
environment: your editor resolves imports through it, the git hooks run
`ruff` and `pytest` through it, and the suite is fast locally versus a
build-and-run cycle. The image never copies it; `.dockerignore` excludes it and
each build creates its own.

## Conventions worth keeping

- Money is `Numeric`/`Decimal`, never `float`.
- Timestamps are timezone-aware, stored UTC.
- Soft-delete important data via `deleted_at`; services filter it out so callers
  never have to remember.
- Input schemas use `extra="forbid"`, so a client typo errors instead of being
  silently ignored — and an injected `owner_id` is rejected outright.
- Errors share one envelope: `{"detail": ..., "request_id": ...}`, plus an
  optional `code` on the few errors a client must branch on rather than display.
  Unhandled exceptions log in full and return an opaque 500.
