# Google Calendar integration (US-STU-005)

## What it does

Optional, one-way SIS-to-Google sync into calendars created by this app only.
Google sign-in never replaces the SIS student session. Primary calendars,
personal events, two-way sync and enrollment changes are out of scope.

- Select up to ten academic terms; `{}` selects the current configured term.
  Explicit selection requires both `year` and `semester` using stored values.
- Each meeting becomes a weekly series in Asia/Manila, bounded by its term and
  excluding configured SIS holidays. Changes update series without duplicates.
- The worker checks committed schedules about every five minutes by default.
  Unchanged background checks make no Google calls. Explicit `/sync` or reconnect
  forces reconciliation and restores Google-side edits to SIS-managed fields.
- Same-account reconnect reuses known calendars; a different verified account
  gets fresh mappings. Disconnect retains calendars/events in Google, immediately
  fences further local writes and queues token revocation. An in-flight call may
  finish. Repeated disconnect is safe; sync is refused until reconnect.
- Status exposes `enabled`, `connected`, `disconnecting`, `last_sync_at`,
  `next_sync_at`, `last_error` and `targets` (year, semester, provisioned).
  Tokens, Google identities and calendar IDs are never returned.

## API contract

Base: `/api/v1/student/schedule/google-calendar`.

| Method | Suffix | Access | Result |
| --- | --- | --- | --- |
| GET | (none) | Student session + ReadSchedule | 200 status |
| POST | `/authorization` | Student session + ReadSchedule | 200 consent URL |
| POST | `/callback` | Student session + ReadSchedule | 200 connected; sync queued |
| POST | `/sync` | Student session + ReadSchedule | 202 queued |
| DELETE | (none) | Student session only | 202 disconnected; revocation queued |

Missing sessions return 401; missing permission returns 403, except disconnect
still works without ReadSchedule. Responses/errors are private and non-cacheable.

## Google Cloud setup

1. In a dedicated test Google Cloud project, enable **Google Calendar API**.
2. Configure OAuth branding and audience. For an external app in Testing, add
   explicitly permitted test accounts to the audience. Do not use personal data
   or external-account tests without explicit permission.
3. Request only `openid` and
   `https://www.googleapis.com/auth/calendar.app.created`; never broaden scopes
   to troubleshoot access failures.
4. Create an OAuth **Web application** client. Register the exact frontend
   redirect URI, for example `http://localhost:3000/calendar/google/callback`.
   Scheme, host, port, path and trailing slash must match configuration.
   Use HTTPS outside local development. The backend POST callback is NOT the
   registered browser redirect. Configure API CORS for the frontend separately.
5. Store the client ID/secret privately on the server. The application reads
   environment settings, not a downloaded client JSON pathname. Never put the
   secret or encryption key in browser/public environment variables or Git.

## Environment variables

| Variable | Value / requirement |
| --- | --- |
| `GOOGLE_CALENDAR_ENABLED` | `false` by default; `true` enables connect/sync/worker. |
| `GOOGLE_CALENDAR_CLIENT_ID` | OAuth web client's ID. |
| `GOOGLE_CALENDAR_CLIENT_SECRET` | Same web client's secret; server-side only. |
| `GOOGLE_CALENDAR_REDIRECT_URI` | Exact registered frontend redirect URI. |
| `GOOGLE_CALENDAR_ENCRYPTION_KEY` | Stable URL-safe base64 encoding of 32 random bytes. |
| `GOOGLE_CALENDAR_SYNC_SECONDS` | Default 300; allowed 60-86400 seconds. |
| `DEBUG` | Must be `false` when integration is enabled. |

Enabled startup fails closed if required configuration is missing/invalid.
Generate the encryption key once in a private terminal and store it securely:

```powershell
uv run python -c "import base64,secrets; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())"
```

API and calendar worker need the same stable key and Google configuration.
Do not regenerate the key at restart: stored grants would become unreadable.
With the feature disabled, schedule reads and PDF/ICS exports need no Google
credentials. Status reports `enabled: false`; authorization/sync return 503.
Disconnect remains available locally; remote revocation needs an enabled worker.

## Running locally

Use only an explicitly verified local/disposable database with the expected
schema and restricted runtime grants. Do not run migrations against an unknown
`DATABASE_URL` or `DIRECT_DATABASE_URL`. Load server settings into each process's
environment (the shared default env file is `env/admin/env.dev`).

```powershell
uv sync --frozen
uv run uvicorn cbpupsis_api_student.main:app --host 127.0.0.1 --port 8001
# In a separate terminal with the same server configuration:
uv run python -m cbpupsis_api_student.calendar_worker
# Scheduler alternative, not an additional continuous worker:
uv run python -m cbpupsis_api_student.calendar_worker --once --max-accounts 50
```

The shared outbox worker is not the calendar worker. Local Scalar is
`http://localhost:8001/scalar` in development. Use synthetic SIS records and a
permitted Google test account only after external testing is authorized.

1. Sign in as a student with ReadSchedule; POST `/authorization` with `{}`.
2. Open `authorization_url` privately. Accept Google's app-created-calendar scope.
3. The frontend receives the redirect and forwards `{ "state": "...", "code":
   "..." }` to POST `/callback` with the **same SIS session**, within 600 seconds.
   State is single-use, session-bound and protected by PKCE/OIDC nonce checks.
4. Poll status, wait for worker completion, then inspect the permitted calendar.
   Callback 200 and sync 202 mean queued, not delivered. Never share callback
   URLs, codes, state, bearer tokens or Google IDs in logs/screenshots/reports.
5. DELETE to disconnect; revocation finishes when `disconnecting` becomes false.

## Troubleshooting

| Symptom | Action |
| --- | --- |
| 400 `GOOGLE_CALENDAR_INVALID_STATE` | Expired/replayed/superseded/foreign-session attempt. Begin fresh consent. |
| 409 `GOOGLE_CALENDAR_CONFLICT` | Check status: already connected, revocation pending, sync running, or ten-term limit. |
| 503 `GOOGLE_CALENDAR_UNAVAILABLE` | Disabled feature or consent exchange/identity failure/25-second deadline. Retry-After is 30 seconds; failed callbacks need fresh consent. |
| `redirect_uri_mismatch` | Match the web client's registered frontend URI exactly; not the backend POST URL. |
| Connected but no events | Verify worker/runtime gate, selected term, committed enrollment and status timestamps. |
| `providerUnavailable`, `quotaLimited`, `syncTimedOut`, `sourceUnavailable` | Inspect sanitized status and backed-off next attempt; verify provider/source health. |
| `reconnectRequired`, `permissionDenied`, `calendarMissing` | Resolve grant/calendar access and reconnect as needed; these stop ordinary sync retries. |
| `calendarCreationUncertain` | Operator reconciliation required; never blindly create duplicate calendars. |
| Unexpected worker failure | Investigate privately and restart after fixing it; unexpected exceptions propagate, not sourceUnavailable. |

See [ERROR-CODES.md](ERROR-CODES.md) for HTTP error contracts. Local synthetic
tests do not establish Google delivery, deployed route readiness or CI acceptance.
