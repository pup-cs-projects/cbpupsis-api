# Environments

Bruno environment files hold **no comments** — the `.bru` environment grammar
accepts only `vars` blocks — so what each variable is for is documented here.

| Variable | Purpose |
|---|---|
| `student_url`, `faculty_url`, `admin_url` | Origin of each app (ports 8001, 8002, 8003). `/health`, `/docs`, and `/openapi.json` hang off each. |
| `student_api`, `faculty_api`, `admin_api` | Each app's versioned API root (`{origin}/api/v1`). A request uses the root of the app that serves it: IAM and audit requests use `admin_api`, items use `student_api`. Endpoints every app serves (auth, users, notifications) use `student_api`. |
| `docs_expected` | `true` where the docs surface should be reachable, `false` for a production-like build. `08-contract/08-docs-surface` asserts the opposite thing depending on it — set it to `false` and it demands `/openapi.json`, `/docs`, `/redoc`, and `/scalar` all 404. |
| `e2e_password` | Password for the seeded accounts. Resolved from the `E2E_PASSWORD` process environment variable (via `bruno/.env` locally), never written in a committed file. It must match what `scripts/seed_e2e.py` seeded. |

## `local` vs `ci`

Identical today. `ci` exists as its own file so CI-specific concerns — a
different host, a longer timeout, `docs_expected: false` against a
production-like image — can diverge without touching the environment a developer
uses by hand.

## A note on rate limiting

The suite makes roughly two dozen calls across `/auth/login` and
`/auth/admin/login`, which is more than the template's default
`RATE_LIMIT_LOGIN=10/minute` allows. Against an API running that default,
folders 03 and 04 fail with 429s that have nothing to do with what they test.

Run the API with a relaxed login limit — the CI workflow sets
`RATE_LIMIT_LOGIN=200/minute` for exactly this reason. Leave `RATE_LIMIT_ENABLED`
**on**: `08-contract/99-rate-limit` still proves limiting works by driving past
that raised ceiling, so the mechanism is tested rather than switched off.
