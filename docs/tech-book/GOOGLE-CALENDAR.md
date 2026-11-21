# Google Calendar integration: QA, frontend and backend guide

This is the user-approved extension to the student schedule branch, separate
from its original file-download contract. It is disabled by default. Existing
PDF/ICS downloads continue to work without Google configuration or an account
connection. No Google sign-in replaces the SIS student session.

## Start here

| Role | What you need | Where to start |
| --- | --- | --- |
| QA | A ready QA deployment, synthetic SIS login, allowed Google test account and fixture expectations | [Scalar/browser walkthrough](#qa-workflow-using-scalar-and-a-browser) |
| Frontend | The student API URL, existing SIS session/auth adapter and registered frontend callback URI | [Frontend implementation](#frontend-callback-and-privacy-contract) |
| Backend/operator | An approved disposable/local database, environment configuration and Google Cloud project access | [Cloud setup](#google-cloud-setup-no-credentials-required-to-use-manual-exports), then [backend startup](#backend-local-startup-and-fixture-handoff) |

**QA does not need PowerShell, the downloaded client-secret JSON, an encryption
key, or their own Google Cloud project when using a configured team deployment.**
Backend operators perform setup once per environment. Frontend developers may
use that deployment or configure their own local backend by following this guide.
No frontend screens are implemented by this API repository.

### Agree on these URLs before testing

The following are examples for the repository's **standard local Compose stack**,
not requirements for a shared QA deployment:

| Item | Example | Meaning |
| --- | --- | --- |
| Student API origin | `http://localhost:8001` | Receives SIS authenticated requests |
| Student Scalar | `http://localhost:8001/scalar` | Interactive API client, development only |
| Student frontend origin | `http://localhost:3000` | Browser UI; separate client repository |
| Google redirect URI | `http://localhost:3000/calendar/google/callback` | Browser destination registered with Google |
| Backend callback API | `http://localhost:8001/api/v1/students/me/schedule/google-calendar/callback` | Authenticated POST receiving code/state from the browser/client |

The frontend redirect and backend callback are **different endpoints**. Do not
register the backend POST URL or `/scalar` as Google's redirect URI. In standard
Compose, port 8002 belongs to the faculty API; the earlier isolated Google test
preview happened to expose the student API on 8002. Use the URL supplied by your
operator. `localhost` always means the machine running your browser; remote QA
must use its deployment URL, not a teammate's localhost example.

### Operator-to-team handoff

Share the following through the team's approved channels before QA starts:

- Build/branch, student API/Scalar URL, frontend URL and exact redirect URI.
- Whether this is backend-only testing or an implemented client callback page.
- Synthetic SIS account credentials privately, with student role/ReadSchedule.
- Confirmation that the Google test account was added to the OAuth audience.
- Stored year/semester, term dates, expected courses/meeting slots, rooms and SIS
  holiday exclusions. Indicate which fixture operator can change/drop/re-add data.
- Worker availability, normal scheduling delay, and who handles backend failures.

Do not share server secrets or ask QA to export their bearer token, callback URL,
Google account identity or calendar IDs in a test report.

## Behavior and boundaries

- One-way SIS-to-Google synchronization, only into calendars created by this app.
  Never read/write the student's primary calendar or arbitrary supplied calendar
  identifiers. No personal-event import, invitations, or two-way editing.
- Google authorization asks for `openid` and
  `https://www.googleapis.com/auth/calendar.app.created`, not broad calendar access.
  OIDC subject verification binds reconnects to their Google account; no Google
  name or email is needed. Changing accounts does not reuse the prior account's
  calendar/event mappings.
- A student may explicitly select up to ten pinned academic terms. Omitting both
  selectors selects the current configured term; supplying one alone is rejected.
  Adding another term uses `/sync`, not another authorization. There is no silent
  rollover that overwrites last semester's calendar. Disconnect hides active
  targets but retains private reconnect mappings; it does not delete calendars.
- Each calendar has one recurring series per meeting, Manila local times,
  term-bounded weekly recurrence, and configured SIS holiday exclusions. Google
  Calendar's national-holiday overlay does not change the SIS academic calendar.
- A separate student worker reconciles committed enrollment, meeting and holiday
  data. This does not implement approval APIs. Default recheck interval is five
  minutes, plus worker scheduling/backlog and transient retry delay, not an
  immediate-delivery SLA. Run the worker or schedule its bounded `--once` mode.
- Unchanged source content normally makes no Google API calls. `/sync` forces
  reconciliation, including missing/edited Google entries. SIS-managed fields may
  overwrite Google-side edits. SIS enrollment is never changed by Google edits.

## APIs

Base: `/api/v1/students/me/schedule/google-calendar`. All requests require a live
student bearer session. All responses, including validation/auth errors, are
private/no-store. Except consent withdrawal, routes also require `ReadSchedule`.
There are no student-number inputs or read-all routes.

| Method | Path | Body | Result |
| --- | --- | --- | --- |
| GET | base | None | 200 connection/status, no credentials or Google account identity |
| POST | `/authorization` | `{}` or `{"year":"2026-2027","semester":"first"}` | 200 `authorization_url`, `expires_in` (600 seconds) |
| POST | `/callback` | `{"state":"...","code":"..."}` | 200 connected status; initial synchronization queued |
| POST | `/sync` | Same selectors as authorization | 202 queued status, not a claim events were already delivered |
| DELETE | base | None | 202 stopped locally; Google token revocation queued |

Status uses snake_case: `enabled`, `connected`, `disconnecting`, `last_sync_at`,
`next_sync_at`, `last_error`, and `targets` (`year`, `semester`, `provisioned`).
`last_sync_at` records successful reconciliation, including unchanged-source checks,
not necessarily a Google write. Inspect `last_error` rather than treating 202 as
remote success. `reconnectRequired` means a new consent flow is necessary.

Responses are direct JSON objects, without a `status`/`data` success wrapper.
Earlier local drafts used `authorizationUrl`, `expiresIn`, `lastSyncAt`,
`nextSyncAt` and `lastError`; clients must use the snake_case properties above.
Error/status values such as `reconnectRequired` are unchanged. Google's own API
payload fields (for example `dateTime` and `timeZone`) are provider protocol, not
SIS response fields, and retain Google's spelling. Durable internal mappings and
event identities also remain unchanged.

The authorization request stores a hashed, single-use state bound to the SIS
student and session. PKCE verifier and OIDC nonce are stored encrypted. Expired,
foreign, superseded, disconnected, or replayed callbacks cannot attach an account.
Starting authorization again invalidates the previous attempt. State expires
after ten minutes; the same SIS session must still be live when completing it.

Google may grant only some requested permissions. The Calendar permission must
be selected on the consent screen; a code-exchange HTTP 200 alone does not prove
that permission, a refresh credential or a valid ID token was returned. Callback
503 responses remain generic. Operators can inspect the static
`calendar_oauth.exchange_rejected.<stage>` warning for scope, required-token,
signing-key or identity validation failures; it contains no provider values.
Never log token responses/claims to diagnose them. After a failed exchange the
attempt is consumed: start fresh consent, never replay the old callback URL.

## Google Cloud setup (no credentials required to use manual exports)

This section is for the backend/environment owner, not every QA tester.
Use a separate test project/account rather than production credentials.

### 1. Enable the Calendar API

In [Google Cloud Console](https://console.cloud.google.com/), select the intended
project in the top project selector. Open **APIs & Services > Library**, search
for **Google Calendar API**, and enable it. Confirm its details page says Enabled
in the same project that will own the OAuth client. Creating OAuth credentials
alone does not enable Calendar calls.

### 2. Configure Google Auth Platform

Open **Google Auth Platform > Branding**. If it is not configured, select
**Get started**, enter the app name, support/contact information, choose the
appropriate audience, and complete setup. For personal Google test accounts,
use **External**, keep the app in **Testing**, then open **Audience > Test users >
Add users** and add each approved test account. Internal is only appropriate for
an eligible Workspace organization, not a substitute for adding external testers.

Under **Data Access > Add or remove scopes**, configure only what this backend
requests: `openid` and `https://www.googleapis.com/auth/calendar.app.created`.
Do not broaden access to all calendars to troubleshoot a setup error. See
[Google's consent setup](https://developers.google.com/workspace/guides/configure-oauth-consent)
and [scope definitions](https://developers.google.com/workspace/calendar/api/auth).

### 3. Create a web OAuth client

Open **Clients > Create client**, choose **Web application**, give it an
environment-specific name, and add the exact frontend URI under **Authorized
redirect URIs**. For example:

```text
http://localhost:3000/calendar/google/callback
```

Scheme, hostname, port, path and trailing slash must match the backend setting.
`localhost` and `127.0.0.1` are not interchangeable registrations. Use HTTPS for
staging/production. This backend exchanges the code server-side; an API key,
desktop client or service account is not a replacement. Authorized JavaScript
origins do not replace redirect registration or the SIS API's CORS configuration.
See [Google's web-server OAuth requirements](https://developers.google.com/identity/protocols/oauth2/web-server).

### 4. Map the downloaded JSON to server configuration

Download/store the credential JSON privately outside the repository. Inspect it
locally; do not paste it into chat or a PR. A web client normally has a top-level
`web` object:

| JSON field | Backend setting |
| --- | --- |
| `web.client_id` | `GOOGLE_CALENDAR_CLIENT_ID` |
| `web.client_secret` | `GOOGLE_CALENDAR_CLIENT_SECRET` |
| The chosen entry in `web.redirect_uris` | `GOOGLE_CALENDAR_REDIRECT_URI` |

The application reads environment variables, **not** this JSON file's pathname.
The JSON does not provide the application's encryption key. Never put the secret
or key in a client-side/public environment variable, request body or image layer.

Generate a separate 32-byte encryption key once in a private local terminal:

```powershell
uv run python -c "import base64,secrets; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())"
```

Store the output privately and use the **same stable value** for the student API
and calendar worker. Do not regenerate it on each start; existing encrypted
grants would become unreadable. Use managed secrets for deployed environments.

### 5. Configure the student API and worker

For local Compose, edit the gitignored `env/student/env.dev`; both services read
it. Preserve the rest of the base application configuration.

```text
GOOGLE_CALENDAR_ENABLED=true
GOOGLE_CALENDAR_CLIENT_ID=<web-client-id>
GOOGLE_CALENDAR_CLIENT_SECRET=<server-only-client-secret>
GOOGLE_CALENDAR_REDIRECT_URI=http://localhost:3000/calendar/google/callback
GOOGLE_CALENDAR_ENCRYPTION_KEY=<generated-base64-key>
GOOGLE_CALENDAR_SYNC_SECONDS=300
DEBUG=false
```

| Setting | Rule |
| --- | --- |
| `GOOGLE_CALENDAR_ENABLED` | Defaults to false; enable only after setup |
| Client ID/secret | Same OAuth web client in API and worker |
| Redirect URI | Same exact registered frontend URI in API and worker |
| Encryption key | URL-safe base64 encoding of exactly 32 bytes; shared and stable |
| Sync seconds | Default 300; allowed range 60-86400; not a delivery SLA |
| `DEBUG` | Must be false when integration is enabled; SQL echo must stay off |
| `ENVIRONMENT` | Development permits HTTP localhost callbacks; other environments require HTTPS |

The API refuses enabled-but-incomplete/unsafe settings at startup. Setting
`GOOGLE_CALENDAR_ENABLED=false` disables Google setup requirements, not ordinary
schedule/PDF/ICS access. Restart/recreate both API and worker after environment
changes; Compose `restart` alone does not reload changed `env_file` values.

## Backend local startup and fixture handoff

These commands are for an **owned local/disposable** environment from the API
repository root. Do not point them at a shared or production database. Existing
local projects/ports may conflict; agree on the environment before starting.
See [Docker setup](../../docker/README.md) for the base stack and runtime roles.

### Prepare the base environment

Install the repository's Python 3.12/uv and Docker prerequisites. If env files
do not exist, copy the template without overwriting existing secrets:

```powershell
uv sync --frozen --all-packages
foreach ($app in @('student', 'faculty', 'admin')) {
    New-Item -ItemType Directory -Path "env/$app" -Force | Out-Null
    if (-not (Test-Path "env/$app/env.dev")) {
        Copy-Item env.example "env/$app/env.dev"
    }
}
```

Configure the local database URLs and one shared strong `JWT_SECRET` in those
files according to the Docker guide. For a **fresh standard local Compose DB**:

| Process | Database configuration |
| --- | --- |
| API/worker | `DATABASE_URL=postgresql://app_runtime:app_runtime_local_only@db:5432/app`, `AUDIT_RUNTIME_ROLE=app_runtime` |
| Migration service | `DIRECT_DATABASE_URL=postgresql://postgres:postgres@db:5432/app` in the admin env file only |

These passwords are the repository's disposable local defaults, not deployment
credentials. If overridden, keep role/password settings consistent. Never use
the owner as API/worker runtime or weaken grant checks to make startup pass.
Existing DB volumes do not rerun init SQL; follow the runtime-role migration
instructions for upgrades rather than deleting someone else's volume.

### Apply migrations, seed IAM and start services

Complete the Google settings above in the student env file first, then run:

```powershell
docker compose -f docker/docker-compose.yml up -d db
docker compose -f docker/docker-compose.yml run --rm --build migrate
docker compose -f docker/docker-compose.yml run --rm --no-deps migrate python -m scripts.seed_iam
docker compose -f docker/docker-compose.yml --profile google-calendar up -d --build api-student google-calendar-worker
docker compose -f docker/docker-compose.yml --profile google-calendar ps
```

The migration runs `alembic upgrade head`: `g096_google_calendar` follows
`d096_schedule_export`. Do not generate another migration. For an already
configured stack, recreate API/worker after env changes:

```powershell
docker compose -f docker/docker-compose.yml --profile google-calendar up -d --force-recreate api-student google-calendar-worker
```

Do not run `down -v` as a troubleshooting shortcut.

Confirm the migration exited successfully, API is running, and the dedicated
`google-calendar-worker` is running without restart loops. The shared `worker`
is the outbox relay, **not** the Google worker. Open the student Scalar URL and
log in; GET Google status must show `enabled: true`. API `/health` alone does
not prove a running worker or successful Google synchronization.

Without Docker, use the repository's host settings/database workflow and
explicitly inject the **student** environment before running:

```powershell
uv run uvicorn cbpupsis_api_student.main:app --host 127.0.0.1 --port 8001
# Separate terminal, same student settings and database:
uv run python -m cbpupsis_api_student.calendar_worker
# Scheduler alternative, not an additional continuously running worker:
uv run python -m cbpupsis_api_student.calendar_worker --once --max-accounts 50
```

Host commands do not automatically load `env/student/env.dev`; the settings
default env file is the admin file. Do not accidentally run the student worker
with admin/owner credentials. Container DB hostname `db` is not a host-machine
hostname; use the approved host-access URL for host processes.

### Prepare synthetic SIS fixtures

`seed_iam` supplies permissions/groups, **not** student profiles, academic terms,
course sections or enrollments. Running pytest does not seed the running API.
There is no Google-specific HTTP fixture/provisioning endpoint or standalone
live schedule seed CLI in this change. The backend fixture owner must use an
approved local fixture/import workflow and commit the data before QA connects.
Do not improvise real enrollment writes or expose a test-only public route.

The synthetic reference is [schedule test fixtures](../../tests/domains/schedule/helpers.py);
its account/role setup is completed by the consuming tests, so calling that
helper alone is not a supported live-account provisioning command. A usable
handoff needs:

- An active synthetic student, valid login profile/credentials and student IAM
  membership granting `ReadSchedule`; a generic email user is not sufficient.
- A configured academic term/current-term selection and enrolled course sections
  with committed lecture/lab meetings, course data and optional faculty names.
- Known SIS holiday exclusions and expected term boundaries.
- Authorization for one fixture operator to modify a meeting or drop/re-add a
  course, then restore the baseline. These Google routes cannot make those edits.

Example baseline, only if your fixture owner creates it:

| Item | Expected value |
| --- | --- |
| Year/semester | `2026-2027` / `first` |
| Term dates | 2026-08-03 through 2026-12-18 |
| CS001 lecture | Monday 08:00-10:00, R101 |
| CS002 lecture | Monday 09:00-11:00, R101 |
| One lab per course | Wednesday 13:00-16:00, LAB2 |
| SIS holiday exclusion | 2026-08-10 through 2026-08-12 |

This intentionally synthetic baseline has four meeting series, including
overlapping labs. It is not a production timetable. Confirm it through GET
schedule before OAuth; replace examples with your deployment's actual fixture.

The frontend Connect/Disconnect button and callback page belong to the client
repository; this backend change does not implement those screens. For local
backend-only verification, open the returned authorization URL in a browser,
then use the redirected `state` and `code` immediately in Scalar's authenticated
POST `/callback` request. Even a missing local callback page may leave the values
in the address bar; never paste that URL or either credential into chat/logs.
Use a synthetic account and dedicated calendars. Do not use API-created mocks as
proof that actual Google consent or synchronization works.

## QA workflow using Scalar and a browser

PowerShell is optional. Local helpers used during development are not product
dependencies or required QA tooling. Scalar sends the authenticated API requests;
the browser handles Google's consent screen. Until the client callback page is
implemented, QA manually forwards its returned code/state through Scalar.

### Prerequisites

- A dedicated QA deployment with the migration applied, Google integration
  enabled, stable API/worker settings and the worker running. Operators configure
  credentials once; individual QAs do not need a new OAuth project/client or
  access to its secret/encryption key. Use the deployment's student Scalar URL;
  standard local Compose uses `http://localhost:8001/scalar`.
- A synthetic SIS student with a live login, ReadSchedule permission, enrolled
  course meetings and a configured academic term. Do not alter real enrollments.
- A dedicated Google test account allowed by the OAuth app's testing audience,
  with Calendar API enabled in the matching Cloud project. Keep unrelated
  calendars hidden when checking duplicates and recurrence.
- The exact registered frontend redirect URI. The backend POST callback is not
  a replacement redirect URI; do not change Google settings for each test run.

### Connect, synchronize and disconnect

All paths below are under `/api/v1`; Google base path is
`/students/me/schedule/google-calendar`.

In Scalar, expand the Google Calendar operation, select **Test Request**, set
the **HTTPBearer** authentication value, edit the JSON body, and select **Send**.
Do not leave Scalar's generated empty strings for optional term fields. Use
`{}` to select the current term. Login bodies/credentials come from the fixture
owner; only the returned SIS `access_token` belongs in HTTPBearer, not its refresh
token and not a Google credential. Keep private values out of shared collections.

1. In Scalar, POST `/auth/student/login` with the synthetic student's credentials.
   Set HTTPBearer to the returned `access_token` value only, without a second
   `Bearer` prefix. Keep that same SIS session for authorization and callback;
   do not log in again between those two requests.
2. GET `/students/me/schedule` and verify the expected enrolled meetings. GET the
   Google base path: `enabled` must be true. If already connected, test `/sync`
   rather than starting another authorization. To test reconnect, first follow
   the disconnect step and wait until revocation completes.
3. POST Google `/authorization` with `{}` for the current configured term, or a
   valid pair such as `{"year":"2026-2027","semester":"first"}`. Replace these
   example selectors with the QA deployment's actual values. Do not send blank
   strings or only one selector. Expect 200 with `authorization_url` and
   `expires_in: 600`.
4. Open that new URL in the browser. Choose the dedicated test account and select
   the requested app-created Calendar permission before continuing. Consent is
   an interactive browser step; Scalar does not replace Google's login screen.
5. From the new redirect URL, extract the `state` and `code` query parameters,
   URL-decoding each value exactly once locally. Do not use an online decoder,
   share/screenshot the URL, or paste the entire URL into either JSON field.
   A deliberately unserved localhost callback page may show connection refused
   during backend-only testing; the address bar can still contain the redirect.
6. Within ten minutes, POST Google `/callback` using the same Scalar bearer
   session and JSON body `{"state":"<returned state>","code":"<returned code>"}`.
   Expect 200 and `connected: true`. This queues sync; it is not delivery proof.
   If the attempt fails/expires, start at authorization with a fresh URL; do not
   replay the consumed code/state. Never attach these values to a bug report.
7. Poll GET Google base until `last_error` is null, `last_sync_at` reflects the
   completed request, and the selected target is `provisioned: true`. Allow for
   worker polling/backlog. Inspect the app-created PUP Course Schedule calendar
   and compare meetings, rooms, Manila times, recurrence and SIS exclusions.
8. POST Google `/sync` with `{}` or both selectors. Expect 202, then poll status
   again and verify no additional calendar/meeting series appears. `last_sync_at`
   can also advance for unchanged-source checks; it is not a Google-write count.
9. DELETE Google base. Expect 202 and `connected: false`; poll until
   `disconnecting: false` and `next_sync_at: null`. Existing Google calendar/events
   must remain. POST `/sync` must now return 409. Reconnect only after revocation
   finishes, repeating authorization/consent/callback with the same Google test
   account; its known calendar and unchanged series should be reused.

### Request and response examples

These bodies are illustrative, not exact timestamp/target assertions. A reused
account may retain an older `last_sync_at`; record status before requesting sync
and compare afterward. UTC `Z` timestamps differ from Manila event display times.

**Before the first connection**, GET Google base should return 200 similar to:

```json
{
  "enabled": true,
  "connected": false,
  "disconnecting": false,
  "last_sync_at": null,
  "next_sync_at": null,
  "last_error": null,
  "targets": []
}
```

**Begin authorization**, POST `/authorization` with one of these, not both:

```json
{}
```

```json
{"year": "2026-2027", "semester": "first"}
```

Expect 200 with `authorization_url` and `expires_in: 600`. Open the **actual**
returned URL, not a sample URL from documentation. Do not submit it as another
Scalar request. A new authorization invalidates an earlier uncompleted attempt.

**Complete authorization**, POST `/callback` with values from that new redirect:

```json
{
  "state": "<exact returned state, decoded once>",
  "code": "<exact returned code, decoded once>"
}
```

Placeholders are not valid credentials. Do not send `iss`, `scope`, the entire
redirect URL, `year`, or `semester` in this body; extra fields are forbidden.
If Google returns `error=access_denied` instead of a code, consent was cancelled:
do not call the backend callback; start fresh authorization when ready.

**After the worker succeeds**, GET base should show a status similar to:

```json
{
  "enabled": true,
  "connected": true,
  "disconnecting": false,
  "last_sync_at": "2026-10-23T08:32:26Z",
  "next_sync_at": "2026-10-23T08:37:26Z",
  "last_error": null,
  "targets": [
    {"year": "2026-2027", "semester": "first", "provisioned": true}
  ]
}
```

Allow for the worker's 30-second polling cycle, queue/backlog and provider retry
delay. For manual QA, poll at a modest interval such as 5-10 seconds; do not spam
`/sync` or assume a fixed completion deadline. `provisioned` alone is not a pass:
check the completed status **and the actual Google calendar**. A second POST
`/sync` returns 202 with a status body; follow it with GET polling, not another
authorization. It may return 409 while a worker job is active.

**After DELETE**, expect 202, `connected: false`, and `targets: []`. Revocation
may already be complete or may show `disconnecting: true`. Wait until it is
false and `next_sync_at` is null before reconnecting. Existing events must stay
visible; disconnect is not a calendar-delete endpoint.

### Extract code/state without PowerShell

For a missing local frontend, connection refused at the registered redirect does
not necessarily mean OAuth failed. Copy the new URL from the address bar
privately, provided it contains `code` and `state`. The backend callback has not
run yet. Do not reload/reuse an already consumed URL.

If you need an offline decoder, this optional **Chromium/Brave DevTools** snippet
uses the DevTools-only `copy()` utility to put callback JSON on your clipboard
without printing the URL or values. Run only code you have inspected, in your
own trusted local/QA Scalar page, not Google's page or an online decoder. Do not
disable browser security protections or paste warnings to use it. Replace the
expected URI with the operator's exact registered frontend URI first:

```javascript
(() => {
  const expected = new URL("http://localhost:3000/calendar/google/callback");
  const raw = window.prompt("Paste the NEW callback URL privately");
  if (!raw) return;
  const url = new URL(raw);
  if (url.origin !== expected.origin || url.pathname !== expected.pathname) {
    throw new Error("Unexpected callback destination");
  }
  if (url.searchParams.has("error")) {
    throw new Error("Consent was not completed; start fresh authorization");
  }
  const state = url.searchParams.get("state");
  const code = url.searchParams.get("code");
  if (!state || !code) throw new Error("Missing callback values");
  copy(JSON.stringify({ state, code }));
})();
```

`URL.searchParams` already decodes query values; **do not** run
`decodeURIComponent` again. Paste the clipboard JSON directly into Scalar's
callback body in the same SIS session and send it promptly. Clear the clipboard
and private request values afterward; close private test tabs when finished.
The snippet is a manual QA aid, not the product callback implementation.

### QA acceptance checks and safe evidence

Use a dedicated Google account and show only the app-created calendar while
counting meetings. A lecture and lab are separate series, and different courses
may legitimately share a time. Old manual ICS imports can look like duplicates;
hide them rather than deleting personal data. Count meeting series, not every
weekly occurrence. The four-series example applies only to the baseline above.

| ID | Action | Pass condition |
| --- | --- | --- |
| G01 | Complete consent with Calendar permission selected | Callback 200; completed worker status and correct meetings in Google |
| G02 | Force unchanged `/sync` twice, waiting between runs | No extra term calendar or meeting series |
| G03 | Operator moves CS001 lecture to Tuesday 10:00-12:00, QA-R202, preserving the meeting record | Old slot gone, new slot once, other three series unchanged |
| G04 | Operator drops one course, then syncs | Only that course's lecture/lab series disappear; other courses remain |
| G05 | Operator re-adds it, then syncs | Series return once; new remote incarnations after deletion are intentional |
| G06 | Inspect first/last term weeks and SIS exclusion dates | No out-of-term occurrences; configured holiday dates omitted |
| G07 | Edit an SIS-managed field in Google, then force `/sync` | SIS value restored; Google edits never change SIS data |
| G08 | DELETE, then wait for revocation | Disconnected/empty targets, sync 409, calendars/events retained |
| G09 | Reconnect the same Google account after G08 | Known calendar/unchanged series reused, changes while disconnected reconciled |
| G10 | Reconnect a different dedicated Google account after disconnect | Fresh isolated mappings; previous account's calendars untouched |
| G11 | Add a second existing academic term through `/sync` with both selectors | Separate pinned term calendar; first term retained, no extra consent flow |
| G12 | Send empty strings, one selector only, or extra body fields | Blank/extra schema values 422; incomplete selector pair 400; no grant/term change |
| G13 | Replay a successful callback or submit it from another SIS session | 400 `GOOGLE_CALENDAR_INVALID_STATE`; no second connection attached |
| G14 | Cancel Google consent, or let the attempt expire | No successful attachment; a fresh authorization is required |
| G15 | Omit bearer or use a non-student/unauthorized session | 401/403 as applicable, no data leak; disconnect still requires a live student session |

Run destructive/negative cases only on the dedicated fixture. G13 consumes or
invalidates test attempts: do not interleave it with a happy-path attempt. G10
retains only the last account mapping; switching back can create new calendars
in the first account, so agree on cleanup before running that case. Restoring
the SIS fixture does not automatically restore Google until reconciliation.
Google's national-holiday overlay is not an additional exclusion source.

Report only sanitized HTTP/error/status fields, fixture step and non-sensitive
observations. Hide bearer values, authorization/redirect URLs, code/state,
Google account identity, calendar IDs and personal events in QA screenshots.
Record queued 202 separately from completed sync and Google Calendar UI proof.

Suggested report: build/environment, case ID, synthetic fixture label/term,
sanitized HTTP status/error code, redacted `request_id` as allowed by team policy,
before/after series counts, expected versus observed times and pass/fail. Keep
account credentials and provider identifiers out. Do not attach a raw HAR,
request export, database row dump or browser console capture containing secrets.

External/testing grants for these scopes normally have seven-day refresh-token
expiry ([Google OAuth guidance](https://developers.google.com/identity/protocols/oauth2#expiration)).
Complete Google's audience,
consent-screen, app-verification and data-policy requirements before production.
Do not assume permanent access based on a successful test grant. Check current
Google quota/billing terms; rate limits and hosting costs still apply.

## Frontend callback and privacy contract

This section is an implementation contract for `cbpupsis-client`, not a claim
that a Connect button or callback page already exists. First complete the QA
happy path against the chosen backend. Do not add a second Google SDK login or
exchange Google codes in browser code; this backend owns OAuth state, PKCE,
nonce, code exchange, credential encryption and background Google requests.

### Configuration and session handling

- Point the client's existing API adapter at the **student** API, with the same
  CORS-approved frontend origin. CORS is configured on the SIS API through
  `CORS_ORIGINS`, for example `["http://localhost:3000"]` for local development.
- Implement the exact registered frontend callback path. The configured URI
  must be agreed with backend/Cloud owners; do not accept `redirect_uri`, a
  Google calendar ID, student ID or arbitrary return URL from user input.
- Use the existing authenticated SIS request adapter for all five operations.
  Only a SIS access token goes in `Authorization: Bearer ...`. Preserve that
  session across full-page Google navigation using the client's approved auth
  mechanism; an in-memory-only session lost on navigation cannot finish consent.
- A refreshed access token is not inherently a new session, but a new login can
  be. Do not deliberately log in again between authorization and callback. If
  the session is lost/expired, restore SIS access and start **fresh** authorization
  instead of replaying the old callback in the new session.
- Keep the client secret and encryption key on the backend. No Google credential
  belongs in localStorage, client telemetry, frontend env bundles or SIS login.

### Connect and callback sequence

```text
Student frontend -- POST /authorization + SIS bearer --> student API
Student API      -- authorization_url ----------------> frontend
Browser          -- navigate, sign in, consent --------> Google
Google           -- redirect with code/state ---------> frontend callback URI
Frontend         -- POST /callback + same SIS session -> student API
Student API      -- encrypted grant + queued sync ----> database
Calendar worker  -- reconcile committed SIS schedule -> app-created calendar
Frontend         -- GET status -----------------------> student API
```

1. Load GET connection status when the schedule settings screen opens. Keep
   ordinary schedule/download controls usable if Google is disabled/unavailable.
2. On an explicit Connect action, POST `/authorization` with `{}` or the selected
   year/semester pair. Disable the button while pending; do not create attempts
   automatically on render, hover, polling or repeated clicks.
3. Navigate to the returned `authorization_url` with browser navigation. Do not
   `fetch()` it, send the SIS bearer to Google, reconstruct its query or persist
   it in analytics. Only navigate to the expected Google authorization origin
   through the trusted backend result; do not use an untrusted return URL.
4. At the callback, capture the query in memory and **immediately remove it from
   history before awaiting network calls or rendering third-party content**.
   Check Google `error`, `code` and `state`; a denial is cancellation, not a
   callback with empty fields.
5. POST the decoded code/state once through the existing SIS adapter. Do not
   automatically retry this POST after a timeout, network error, page reload or
   framework development double mount: it is single-use. Use one guarded
   completion operation, not a new call from each render/effect.
6. On success, discard callback values and navigate to a fixed internal schedule
   route. Poll GET status; show connection saved/synchronization pending, then
   the completed status. On failure, discard values and offer a fresh Connect
   attempt after checking status. A lost response may have saved the grant;
   inspect GET before telling the user to repeat consent.

Framework-neutral sketch for the callback's **query handling only**:

```javascript
const url = new URL(window.location.href);
const googleError = url.searchParams.get("error");
const state = url.searchParams.get("state");
const code = url.searchParams.get("code");
window.history.replaceState(window.history.state, "", url.pathname);

// In the client's guarded, single-use completion controller:
// - If googleError exists, show cancellation without calling the API.
// - If code/state is missing, show a restart action.
// - Otherwise send { state, code } through the existing authenticated adapter.
// URLSearchParams already decoded the values; do not decode them again.
```

This is not a standalone callback page or session-storage implementation. Wire
it into the client's existing auth/router conventions. Do not console-log these
variables, append them to a follow-up URL, or include them in exception telemetry.

### Status, Sync and Disconnect UI

| Backend state | Suggested UI/behavior |
| --- | --- |
| `enabled: false` | Explain integration unavailable; leave PDF/ICS available |
| Not connected, not disconnecting | Offer Connect |
| Connected, initial sync incomplete | Show saved/pending, not "schedule synced" |
| Connected, successful recent reconciliation | Show last checked time and pinned terms; not a Google-write count |
| Sync 202 | Show queued; poll status; avoid repeated submissions |
| `disconnecting: true` | Show disconnect/revocation pending; disable Connect until complete |
| `last_error: reconnectRequired` | Explain access must be renewed; offer fresh consent when revocation is not pending |
| Other terminal error | Explain operator review required; do not silently make another calendar |
| Transient provider error | Show delayed/retrying state; avoid tight client retries |

POST `/sync` with both stored selectors to add another term while connected;
do not use authorization for that. At most ten terms are supported, and there is
no public remove-one-term endpoint. A Sync click does not change enrollment.

DELETE base has no body. Stop presenting the connection as active immediately
on its 202 result, but explain that remote revocation may still be pending and
existing calendars/events remain. Poll GET only while the student retains
ReadSchedule; DELETE remains allowed without that permission but GET does not.
Handle 403 by stopping polling and asking the operator to verify revocation.
Use bounded/adaptive polling while the page is visible; stop when logged out,
unmounted or forbidden. Do not store a successful 202 as a permanent sync success.

Frontend acceptance should cover same-session navigation, decoded values,
cancel/expired/replayed attempts, double mount/reload, lost callback response,
queued versus completed UI, revocation pending, and no code/state in history,
referrers, access logs, analytics or error reporting. Browser/network QA evidence
must be redacted. Mocks validate UI behavior, not real Google delivery.

### Privacy controls shared with backend operators

Navigate the browser to `authorization_url`; do not fetch it as a cross-origin API.
Google redirects to the single configured frontend URI. That page must avoid
third-party assets/analytics, set `Referrer-Policy: no-referrer`, immediately remove
the query from browser history, and forward code/state in an authenticated JSON
POST to the backend callback. Keep the same SIS session; don't expose Google
credentials as SIS login tokens. Handle Google denial as cancellation and allow
a new authorization attempt. Do not accept a caller-supplied redirect URI.

Redact query strings on the frontend callback in gateway/proxy access logs. The
backend POST callback avoids putting codes in backend request URLs and redacts
validation inputs, including malformed JSON. SQL echo must remain disabled.
Application logs must never contain tokens, names, student numbers or event
contents. Refresh credentials use AES-256-GCM bound to student and purpose, with
a separate required key and random nonce. Access tokens are not persisted.
Keep the key stable across restarts; key rotation requires controlled re-encryption
or reconnect, not silently swapping the configured value.

## Troubleshooting and backend verification

### Triage in this order

1. Confirm the correct student API/build and exact frontend redirect URI.
2. Confirm a valid SIS student login and GET schedule with the expected term and
   committed meetings. Fix term/fixture/permission problems before Google consent.
3. Read GET Google status. Record sanitized status/error category, not secrets.
4. Distinguish a request failure (HTTP response) from a background failure
   (`last_error` after a queued 200/202). Inspect worker scheduling and liveness.
5. Have the operator inspect only approved sanitized logs and outbound network
   access. Never print full settings, environment, token responses or SQL rows.

| Symptom | Meaning/check | Next action |
| --- | --- | --- |
| Google endpoints absent in Scalar | Wrong app, old build or stale schema | Open student Scalar, confirm build, refresh; production disables Scalar by design |
| `enabled: false` | Deployment flag disabled | Operator configures settings and recreates API/worker; not a QA consent problem |
| Startup rejects Google settings | Missing secret/key, invalid URI/key or DEBUG enabled | Correct server settings; never bypass validation or generate a replacement key for existing grants |
| Google `redirect_uri_mismatch` | Registration and configured frontend URI differ | Compare exact scheme/host/port/path/trailing slash in the matching Cloud project/client |
| Google blocks test account | Audience/test user or Workspace policy | Operator checks allowed test users and organization policy; do not bypass account restrictions |
| Callback page says connection refused | Local frontend callback not running | Backend-only QA may privately forward values through Scalar; full-client testing must fix the frontend route |
| 422 with `string_too_short` | Scalar sent `""` for optional fields | Use `{}` or a nonempty valid pair; extra fields also fail schema validation |
| 400 for incomplete selector pair | Only year or semester supplied | Supply both stored selectors or neither |
| 503 `SERVICE_UNAVAILABLE` during term selection | No usable configured term/schedule snapshot | Verify GET schedule term/fixture; this is not automatically a Google failure |
| 400 `GOOGLE_CALENDAR_INVALID_STATE` | Expired, consumed, superseded or foreign-session state | Begin fresh authorization in the intended live SIS session; never replay |
| 409 `GOOGLE_CALENDAR_CONFLICT` | Already connected/revoking, active worker lease, missing grant or ten-term limit | Inspect GET; use sync when connected, wait for work/revocation, or resolve term selection |
| 503 `GOOGLE_CALENDAR_UNAVAILABLE` | Disabled integration, failed OAuth exchange/verification or callback timeout | Check configuration/Google permission/network; observe Retry-After 30; a new callback attempt needs fresh consent |
| 401/403 | Invalid/expired SIS session, wrong role or missing ReadSchedule | Fix SIS access; Google sign-in does not grant SIS access |
| 202 persists without completion | Queued, not delivered | Check dedicated calendar worker, due time/backoff and `last_error`; do not spam sync |
| Calendar visible but no meetings | Provisioning is not delivery; possibly empty source or wrong date range | Compare GET schedule and chosen term, worker status and actual term weeks |
| Extra-looking events | Multiple enabled calendars, ICS imports or legitimate slots | Hide unrelated calendars and compare the fixture's series count; do not delete data blindly |
| `disconnecting` stays true | Remote revocation not finished | Keep worker/settings available and investigate retries; do not claim token erased remotely |

Examples of background categories:

| `last_error` | Recovery |
| --- | --- |
| `providerUnavailable`, `quotaLimited` | Worker retries with backoff; check outbound connectivity/quota and scheduling |
| `reconnectRequired` | Fresh consent is needed; avoid retrying the old callback |
| `permissionDenied` | Check Calendar API enabled in the OAuth client's project, granted Calendar permission and policy; after correction, explicitly queue sync or reconnect as appropriate |
| `calendarMissing` | Known calendar deleted/inaccessible; stop and involve operator/user rather than create a duplicate |
| `calendarCreationUncertain` | Creation response was ambiguous; operator recovery required, not repeated sync/reconnect to bypass safety |

Terminal synchronization categories stop automatic attempts. A corrected setup
can be explicitly retried with `/sync` when still connected; this does not remove
the fail-closed rules for missing/uncertain calendars. Pending **revocation** keeps
retrying independently of terminal sync errors. See [durability and recovery](#durability-failures-and-disconnect)
before operating on those states.

Request error `code` and background `last_error` are different contracts. Do not
convert their case or assume every error uses a success wrapper. For example, a
consumed state returns HTTP 400 with an application error body like:

```json
{
  "status": "error",
  "message": "Authorization expired or is invalid. Start connecting again.",
  "detail": "Authorization expired or is invalid. Start connecting again.",
  "request_id": "<correlation-id>",
  "code": "GOOGLE_CALENDAR_INVALID_STATE"
}
```

Branch on HTTP status and stable `code` when present, not the prose message.
Schema validation 422 responses instead contain a `detail` list; they do not
have to carry that application error code. Callback validation inputs are
redacted, so never enable raw request logging to recover rejected private values.

### Operator diagnostics and network access

On the owned local stack, inspect service status first:

```powershell
docker compose -f docker/docker-compose.yml --profile google-calendar ps
docker compose -f docker/docker-compose.yml logs --tail 50 google-calendar-worker
docker compose -f docker/docker-compose.yml logs --tail 50 api-student
```

Review logs privately and redact before sharing. Never use `docker inspect`,
`docker compose config`, environment dumps, raw request logging or token payloads
as a QA attachment: these can reveal injected credentials. Keep DEBUG/SQL echo
off. Worker `calendar_sync.tick` only counts processed attempts; it is not proof
that a specific user's events arrived. Use status plus actual Google inspection.

The API needs HTTPS access for token exchange and Google signing-key lookup; the
worker needs token refresh/revocation and Calendar API access. Verify DNS, TLS,
system clock and approved egress to Google's configured endpoints. Provider HTTP
clients use `trust_env=False`, so merely setting HTTP_PROXY/HTTPS_PROXY is not a
supported proxy setup. Do not disable TLS verification or log provider payloads.

Callback diagnostics use static `calendar_oauth.exchange_rejected.<stage>` labels
for `scope`, `access_token`, `refresh_token`, `id_token`, `signing_keys`, `identity`
and `identity_binding`. These are operator categories, not a reason to expose
Google response bodies. An HTTP 200 from Google's token endpoint alone does not
prove the callback passed required scope/refresh/identity verification.

### Automated checks for backend contributors

From an isolated development checkout with the frozen workspace installed:

```powershell
uv run pytest tests/domains/google_calendar tests/domains/schedule tests/core/test_logging.py -q
uv run pytest tests/domains/student_auth tests/core/test_runtime_head.py tests/core/test_architecture.py tests/apps/test_entrypoints.py -q
```

Tests use synthetic/mocked provider boundaries and do not need your real Google
credentials. They are not a substitute for G01-G10 real-account checks. Do not
run a separate worker against the test suite's database or assume tests provision
the running API's fixtures.

The destructive opt-in test `tests/integration/test_schedule_postgres.py` requires
an owner URL in `SCHEDULE_POSTGRES_URL` pointing to an **owned disposable local**
PostgreSQL 16 database named `cbpupsis_schedule_test*`. It creates roles/fixtures
and runs migrations, including downgrade/upgrade. Never use a shared database or
set this variable to the API's normal DATABASE_URL. After the operator provisions
that isolated database and injects its URL privately:

```powershell
uv run pytest tests/integration/test_schedule_postgres.py -q
```

Unset `SCHEDULE_POSTGRES_URL` after the isolated run and remove only resources you
own. Run live Google tests with synthetic fixture changes separately; retain
sanitized counts/statuses and restore the SIS baseline. Disable Google only
after pending revocations are complete; do not stop the worker first and strand
credentials awaiting revocation.

## Durability, failures and disconnect

Accounts are claimed using committed 120-second database leases and row locking.
Jobs have a 90-second deadline, bounded HTTP timeouts, durable per-event intent and
content fingerprints, and lease-fenced checkpoints. No database transaction is
held while making Google requests. Event IDs are deterministic per meeting and
incarnation, so a lost insert response retries without duplicate series; a dropped
series is deleted and a re-added series uses a new incarnation to avoid Google
deletion tombstones. An unchanged course keeps its remote event ID.
Deletion intent is saved before sending a delete; a lost response followed by
re-enrollment finishes that intent and restores a new series on the next retry.

Transient failures retry with exponential backoff and jitter, capped near one
hour. `reconnectRequired`, `permissionDenied`, `calendarMissing`, or
`calendarCreationUncertain` stop automatic attempts until the connection is
addressed. Calendar creation has no client-supplied idempotency key: intent is
persisted before creation. A lost creation response/crash must not blindly create
another calendar. Inspect/hide any resulting SIS-only calendar, disconnect and
wait for revocation to finish, and have the operator resolve the uncertain target
with the user's approval before retrying. Reconnect deliberately preserves that
uncertainty; it is not a shortcut around creation idempotency. Deleted/inaccessible
calendars similarly stop with calendarMissing rather than silently duplicating a
term calendar. Do not delete personal calendars.

Disconnect immediately invalidates OAuth attempts/worker leases and removes the
local active refresh credential. The encrypted pending-revocation credential is
kept only until the worker successfully revokes Google authorization, then erased.
An already-invalid token counts as revoked; other revocation failures retain the
encrypted credential and schedule backoff retries rather than silently declaring
success. Terminal synchronization errors do not disable revocation retries;
operators must monitor/resolve persistent revocation failures.
An already in-flight Google request may finish; no subsequent checkpoint or batch
may continue under the invalidated lease. Google calendars are left intact so
disconnect never deletes the student's data. Revocation can affect other grants
for the same Google OAuth app; reconnect if needed. Keep the worker running until
pending revocations complete before disabling the integration, or revoke the app
directly in the Google account. Account deactivation/deletion or `ReadSchedule`
withdrawal also stops synchronization and queues credential revocation.

### Reconnect and retained metadata

Disconnect and permission withdrawal retain only the last verified subject hash
and bounded app-created calendar/event mappings, not usable credentials or OAuth
attempts. Disconnected responses expose `targets: []`, and no worker job is
claimable after revocation completes. Retained mappings carry term selectors,
calendar/event IDs, fingerprints and incarnation/deletion state so fresh consent
does not create duplicate calendars. They are private application metadata, not
access grants; apply the SIS account's data-retention/erasure policy to them.
There is no separate provider-calendar discovery or arbitrary calendar-adoption
API, and no broad CalendarList scope is requested.

After pending revocation finishes, reconnect requires a new consent flow using
the same live SIS session for authorization/callback. Only a matching, verified
OIDC subject can reuse the mappings. All retained terms are force-reconciled to
repair managed fields/series changed while disconnected. Normal unchanged jobs
still avoid Google calls. A different Google account starts fresh mappings and
does not read or modify the old account's calendars; switching away and later
back does not retain a multi-account history. Old account calendars remain
visible until the user hides/removes them through Google Calendar.

Old development builds that erased mappings on disconnect cannot recover them
from hashes. Do not identify calendars by their display name or silently create
another one. Resolve known test metadata privately with explicit ownership and
identity checks, or explain the retained-calendar cleanup/new-calendar choice
to the user. This is not an automatic production migration or permission to
adopt a caller-supplied calendar ID.

## Verification and release gaps

Focused tests use a fake Google boundary for OAuth ownership, PKCE/state expiry,
encryption, secret redaction, stable event IDs, committed room/time changes,
drop/re-add, lost insert responses, creation ambiguity, retries and lease fencing.
Provider tests use signed synthetic OIDC tokens and mocked HTTP to verify audience,
authorized presenter, issuer, nonce, expiry/signature and scope checks. Reconnect
regressions cover same-account reuse/forced repair, different-account isolation,
permission withdrawal, revocation retries and request-boundary lease fencing.
These automated provider tests are not live Google tests.
The opt-in disposable PostgreSQL test covers grants and concurrent worker claims.

Before enabling outside a synthetic environment, run the migration round trip on
disposable PostgreSQL, complete real Google OAuth/refresh/revocation, run the
worker against synthetic committed changes (including time rescheduling), and
inspect duplicate/cancellation/holiday/boundary behavior in the actual account.
The previous manual ICS import experiment does not prove these new API paths.
Check deployment key management, access-log redaction, consent verification,
worker scheduling/alerts and quotas separately. Backend review readiness does
not authorize production enablement; complete review and deployment-specific
checks before rollout. No shared-database tests are permitted.

Live synthetic API evidence on 2026-10-23 separately covers actual Google consent,
token refresh, app-created calendar/four-series provisioning, unchanged forced
sync without duplicates, CS001 weekday/time/room changes, drop/re-add with new
incarnations and stable unaffected IDs, sampled August holiday/term-start and
December term-end UI checks, and revocation with rejected old credentials plus
retained events visible in Google Calendar. These are sampled test-account
observations, not production capacity, all-client, exhaustive occurrence or
frontend UI acceptance. A separate fresh-consent reconnect after revocation
also passed real Google reads: the same known app-created calendar and all four
series IDs, exactly four current series, and managed content/recurrence matching
SIS source. The old test build's erased mapping was recovered using the privately
supplied calendar ID verified against pre-disconnect hashes; this is distinct
from automated regressions proving the new disconnect retains mappings itself.
No broad discovery scope or public calendar-adoption endpoint was introduced.

Release checklist (backend readiness is not production enablement):

- Run focused Google/security/auth/architecture tests and synthetic regressions;
  apply/reverse/reapply migrations and test role grants/leases on disposable PG16.
- Complete same-account live reconnect: same calendar, four current series, no
  duplicate IDs; repair fields changed while disconnected. Never paste credentials.
- In the client repository, implement Connect, status/pending/error displays,
  Disconnect and the private callback page, with same-session forwarding,
  cancellation, history cleanup and query-log redaction. No client UI is supplied
  by this backend repository or by a local PowerShell test helper.
- Configure stable shared API/worker encryption keys, managed secrets, separate
  runtime/migration roles, migrations before rollout and a single scheduled worker
  or lease-aware replicas. Alert on revocation backlog, stale syncs and terminal
  errors; verify quota/backoff and operational recovery without logging payloads.
- Finish Google's production consent/privacy/verification requirements and the
  project's migration/client rollout review before enabling the default-off flag.

Primary references:
- [Web-server OAuth](https://developers.google.com/identity/protocols/oauth2/web-server)
- [Google OIDC claims](https://developers.google.com/identity/openid-connect/openid-connect)
- [Calendar scopes](https://developers.google.com/workspace/calendar/api/auth)
- [Calendar creation](https://developers.google.com/workspace/calendar/api/v3/reference/calendars/insert)
- [Calendar usage limits](https://developers.google.com/workspace/calendar/api/guides/quota)
