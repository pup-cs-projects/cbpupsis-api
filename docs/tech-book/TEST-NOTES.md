# Test notes and traceability

<!--
Two jobs:

  1. Trace every acceptance criterion in US-ACS.md to the test that covers it, so
     "is this built?" has an answer that is not someone's memory.
  2. Record the conventions and known gaps of the suite, so the next person adds
     tests the same way.

Whoever adds a test updates the traceability table. Keep it honest: a
row claiming coverage that does not exist is worse than a blank row, because it
stops anyone looking.
-->

| | |
|---|---|
| **Last updated** | 2026-08-29 |
| **Suite** | `uv run pytest` — 455 tests, plus ~50 HTTP requests in `bruno/` |
| **Related** | [US-ACS.md](../project-specs/US-ACS.md) · [ERROR-CODES.md](ERROR-CODES.md) |

The template's own suite, by area — a starting point to replace as your
criteria land, not a target to preserve:

| Area | Tests | Holds |
|---|---|---|
| `tests/core/` | 173 | architecture guards, permission naming, logging, docs gate, probes |
| `tests/domains/auth/` | 96 | credentials, token rotation and reuse, email token lifecycle |
| `tests/domains/users/` | 76 | profile, self-service, and the admin surface |
| `tests/domains/iam/` | 62 | permission resolution, the management API, endpoint gates |
| `tests/domains/items/` | 35 | the reference domain: both authorization levels |
| `tests/domains/audit/` | 13 | the trail, and that it is actually wired to the bus |

## Traceability

<!--
One row per criterion. Status is what is actually true right now:

  Covered   — a test asserts this and passes
  Partial   — asserted, but not the edges or the failure path
  Gap       — no test
  Deferred  — deliberately not covered; say why in Notes
-->

| Criterion | Test | Status | Notes |
|---|---|---|---|
| AC-1.1 | `tests/domains/<x>/test_<x>.py::TestThing::test_<behavior>` | Covered | |
| AC-1.2 | | Gap | |

## How the suite is set up

Tests run against **in-memory SQLite**, so the suite needs no database and each
test starts from a clean schema. That trade is deliberate but not free: SQLite
does not enforce every Postgres behaviour identically. Anything genuinely
dialect-specific — JSONB operators, partial indexes, `SELECT ... FOR UPDATE` —
needs an integration test against a real Postgres branch, and belongs in the
**Known gaps** section below until it has one.

Key fixtures (`tests/conftest.py`):

| Fixture | Gives you |
|---|---|
| `db` | An `AsyncSession` on a fresh in-memory database |
| `client` | An `AsyncClient` bound to the app, sharing `db`'s session |
| `registered_user` | A registered account plus its credentials and id |
| `auth_headers` | `Authorization` headers for that user |
| `grant` | `await grant(user_id, "Action")` — grants one permission through the real IAM path |
| `make_user` | A second account, registered through the real auth path |
| `sent_emails` | Captures outbound mail instead of sending it; the only place a one-time token is readable |

`grant` deliberately walks permission → policy → group → user rather than
inserting a shortcut row, so a break in resolution fails the tests that rely on
it.

## Conventions

- **Name tests for the behavior**, not the function:
  `test_non_owner_cannot_edit_item`, not `test_update_2`.
- **One reason to fail per test.** A test asserting six unrelated things reports
  one and hides the rest.
- **Test at the lowest level that can catch the failure.** A business rule at the
  service layer; a contract at the API layer. An authorization rule tested only
  through HTTP is slow and vague.
- **Say what breaks** in the docstring. A failure message that means nothing to
  the next person is a test that gets deleted.
- **Confirm a new test can fail.** Break the thing it covers and watch it go red,
  then restore. A test that always passes is decoration, and it is the most
  common defect in a suite. Two traps, both seen in this repo:
  - **Sabotage the whole rule, not one spelling of it.** Removing
    `dependencies=[_manage],` left five endpoints declaring the guard on a single
    line; those tests passed and looked verified. They were not.
  - **Remove every level.** A rule enforced in both the router and the service
    stays green when you break only one, so the test proves less than it appears
    to.
- **Drive the mechanism that owns the side-effect.** Drain the event bus for
  best-effort events. For durable side effects, claim and dispatch the outbox
  (the shared `drain_outbox` fixture does this); `publish_transactional` only
  stages work and intentionally does not run a handler inline.

## Always worth testing

These get skipped because their absence is invisible, not because they are hard:

- **Object-level authorization** — the caller holds the permission but not the
  record. This is the IDOR case, and a permission-only check does not catch it.
- **Read boundaries, not just write ones** — a list endpoint that filters by
  nothing leaks every user's rows and returns a perfectly healthy 200. Assert
  that a caller without the `ReadAll<X>` grant sees only their own.
- **Enumeration** — unknown account and wrong password return identical
  responses; another user's resource returns 404, not 403.
- **Revocation actually revokes** — a detach endpoint returning 204 without
  removing the grant is indistinguishable from one that works. Assert the
  effective permission afterwards, never the status code alone.
- **Secret leakage** — passwords, tokens, and hashes appear in no log and no
  response.
- **Production gates** — docs and `/openapi.json` unreachable outside
  development.
- **Idempotence** — doing it twice does not charge twice.
- **Empty states** — zero rows renders an empty state, not an error.

## Known gaps

<!--
Honest list of what the suite does not cover, so nobody mistakes green for
complete. A gap recorded here is a decision; a gap nobody wrote down is an
accident.
-->

| Gap | Why | Mitigation |
|---|---|---|
| Postgres-specific behaviour | Suite runs on SQLite, which does not enforce every constraint identically | Integration tests against a real Neon branch; `bruno/` exercises a real Postgres end to end |
| `GET /ready` querying a real database | The probe uses the application engine, which points at Postgres; the suite runs on in-memory SQLite | The in-process test stubs the check and asserts the 200/503 mapping only. The genuine round trip is `bruno/00-smoke/ready.bru` |
| The audit row's INSERT from the event path | The subscriber opens its own session against the application engine, for the same reason | `record()` is tested directly against the test session; the bus path asserts the handler is invoked with the right payload |
| Rate limiting under Redis | Needs a running Redis; the suite uses `memory://` | Configured in `docker/docker-compose.staging.yml`; `bruno/` drives a real 429 |
| Multi-worker pool sizing | `workers x (pool_size + max_overflow)` only bites under a real deployment | Documented arithmetic in `env.example`; verify against the database's connection limit before scaling out |

## Running

```bash
uv run pytest                      # everything
uv run pytest tests/domains/x -q      # one domain, while working
uv run pytest -k "authorization"   # by name
```

```bash
cd bruno && npm test               # the HTTP suite; needs a RUNNING API
```

Run the tests covering **what you changed**, by path. Reserve the full suite for
before a PR. When reporting results, say plainly what you ran and what you did
not — never imply coverage you did not execute.

The two suites own different things and neither replaces the other: `pytest`
owns each rule and its edge cases, `bruno/` owns journeys and the transport
contract. A new edge case belongs in `pytest` unless the HTTP round trip is
itself the thing under test.
