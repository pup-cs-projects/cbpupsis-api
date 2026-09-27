# End-to-end API tests

HTTP tests that drive a **running** API the way a client does — over the network,
through the real middleware stack, with real tokens. Written as a
[Bruno](https://usebruno.com) collection: plain-text `.bru` files that live in
git and diff like code.

## How this relates to `pytest`

Two suites, two jobs.

| | `tests/` (pytest) | `bruno/` (this) |
|---|---|---|
| Runs against | the ASGI app in-process | a running server over HTTP |
| Needs a database | no (in-memory SQLite) | yes |
| Speed | seconds | tens of seconds |
| Owns | units, rules, edge-case matrices | end-to-end journeys, and the transport contract |

`pytest` is still where a rule's full truth table belongs — it is faster, it needs
no database, and it can call a service function directly. These tests assert that
the pieces work **together over real HTTP**: that a token minted by `/login` is
accepted by `/items`, that middleware puts `X-Request-ID` on the wire, that CORS
preflight succeeds, that a 429 carries `Retry-After`.

Some overlap is deliberate. When a journey re-asserts a rule (ownership, for
instance), it asserts the *outcome a client sees*, not the rule's every branch.
Keep new edge cases in `pytest`; add to a journey only when the HTTP round trip
is the thing under test.

## Setup

Once:

```bash
npm install --prefix bruno          # installs the Bruno CLI
cp bruno/env.example bruno/.env     # the test-account password
```

Then, with the API running and its database migrated:

```bash
uv run python -m scripts.seed_e2e
```

That seeds three accounts the suite logs in as. It is idempotent — re-run it
whenever you reset the database.

Why a script rather than the suite registering its own users? Two things cannot
be done over HTTP:

- **Email verification.** Tokens go only to the console email backend, and
  `/login` refuses an unverified account with `403 email_not_verified`.
- **`ManageIAM`.** The endpoints that would grant it are themselves gated behind
  it, so the first admin has to come from outside the API.

The suite *does* register a throwaway account each run — see `01-auth-journey` —
because registration and the unverified-login rejection can only be observed on
an account whose history the run controls.

## Running

```bash
cd bruno
npm test              # everything, against localhost:8000
npm run test:items    # one folder (smoke runs first, for the tokens)
```

Or drive it directly:

```bash
bru run --env local                  # all folders
bru run 03-authorization --env local # note: needs 00-smoke first for tokens
```

Open the collection in the Bruno desktop app for the same requests with a UI —
pick the `local` environment from the top-right selector.

## Layout

```
00-smoke/                  liveness, and the logins that mint every principal's token
01-auth-journey/           register -> duplicate -> unverified login -> refresh rotation
02-items-journey/          create -> read -> list -> update -> delete, as the owner
03-authorization/          every access-control guarantee, in one reviewable place
04-iam-journey/            permission -> policy -> group -> attach -> add user
05-notifications-journey/  badge -> read-all -> preferences, and route ordering
06-users-journey/          own profile, PATCH partial semantics, input validation
07-session-lifecycle/      login -> use -> logout -> revocation, in one sequence
08-contract/               request ids, error envelope, CORS, docs gating, rate limits
```

Folders run in name order, and requests within a folder in `seq` order. Ordering
is load-bearing: `00-smoke` establishes the tokens everything else uses, and
`08-contract` runs last because its rate-limit test deliberately trips the login
limiter — which is also why that test is `seq: 99` inside its own folder, leaving
room to add contract requests ahead of it without renumbering.

## Conventions

**Authentication is centralized.** No request file contains a token. A request
opts in with `auth: inherit` and names its principal:

```
headers {
  x-e2e-actor: owner    # owner | other | admin
}
```

`collection.bru`'s pre-request script turns that into an `Authorization` header
and strips `x-e2e-actor` before the request is sent.

**The transport contract is asserted once.** `collection.bru`'s post-response
script checks `X-Request-ID` and the error envelope on *every* response, so an
individual request asserts only its own business outcome.

**Three principals**, all seeded:

| Actor | Account | Purpose |
|---|---|---|
| `owner` | `e2e-owner@example.com` | ordinary member; creates and owns records |
| `other` | `e2e-other@example.com` | second member; proves cross-user access is refused |
| `admin` | `e2e-admin@example.com` | holds `ManageIAM`; drives the IAM folder |

**Test data is unique per run.** Registrations, permissions, policies, and groups
are suffixed with a timestamp, so the suite is re-runnable against a database
that already holds a previous run's records without a reset.

## Known limits

- **The IAM grant is not verified end to end.** No endpoint returns a caller's
  effective permissions, so `04-iam-journey` proves the chain completes, not that
  the permission landed. `tests/domains/iam/test_iam.py` covers that directly. If such an
  endpoint is ever added, `09-grant-took-effect.bru` is where the real assertion
  belongs.
- **Records accumulate.** The suite creates a user per run and IAM records that
  are never deleted. That is fine for a disposable development or CI database.
  Do not point this at a database you care about. The accounts use `example.com`,
  reserved by RFC 2606 and never deliverable, so a stray verification email
  cannot reach a real inbox. (`.test` would be the more natural choice, but
  Pydantic's `EmailStr` rejects it as a special-use name — the API returns 422
  for any address in that TLD.)
- **Notification *delivery* is asserted for one event only, and needs the worker.**
  Delivery is performed by a separate process (`app/worker.py`) consuming the
  outbox — it runs in `docker/docker-compose.yml` and in CI, but NOT when you
  start the API alone with `uvicorn`. Two consequences worth knowing before
  adding to `05-notifications-journey`:

  Only `auth.password_changed` is staged durably (`publish_transactional`), so
  it is the one event whose arrival a test can wait for. `item.created` is
  published on the fire-and-forget in-process bus with no outbox handler, so a
  notification for it may never arrive — asserting one would fail for a reason
  that is not a defect.

  The current folder therefore asserts the endpoints and the preference rules,
  which hold with or without a worker. A delivery assertion is possible via the
  change-password flow, but it must poll (delivery is asynchronous) and it
  rotates the account's password mid-run — which is why it is not there yet.
  The mechanics are covered in `tests/core/test_outbox.py` and
  `tests/domains/notifications/`, where the worker is driven directly.
- **Rate-limit state is shared.** With the default `memory://` storage the
  counters live in the API process and persist for the window. Two consequences:
  the API must run with a raised `RATE_LIMIT_LOGIN` (see
  `environments/README.md`), and running the suite repeatedly in quick
  succession can still exhaust even that raised budget. `docker compose restart
  api` clears the counters instantly, since they die with the process.
