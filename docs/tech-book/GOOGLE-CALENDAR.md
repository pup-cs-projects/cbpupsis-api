# Optional Google Calendar connection

This is the user-approved extension to the student schedule branch, separate
from its original file-download contract. It is disabled by default. Existing
PDF/ICS downloads continue to work without Google configuration or an account
connection. No Google sign-in replaces the SIS student session.

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

1. Create a Google Cloud project and enable the **Google Calendar API**.
2. In **Google Auth Platform**, configure branding, audience, contact/privacy
   details and data access. For local testing with personal accounts, choose the
   appropriate external testing audience and explicitly add the synthetic test
   account as a test user. Do not request organizational domain-wide delegation.
3. Create an **OAuth client ID**, application type **Web application**. Register
   the exact student-frontend callback URI, for example
   `http://localhost:3000/calendar/google/callback` in development. Scheme, host,
   port and path must match exactly. Production/staging require HTTPS.
4. Put the client ID/secret and redirect URI in the student API/worker environment
   only. Never put the client secret, refresh token or encryption key in browser
   code, chat, Git, screenshots or access logs.
5. Generate an independent encryption key locally, and store it in the secret
   manager for both the student API and worker:

   ```powershell
   uv run python -c "import base64,secrets; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())"
   ```

   ```text
   GOOGLE_CALENDAR_ENABLED=true
   GOOGLE_CALENDAR_CLIENT_ID=<web-client-id>
   GOOGLE_CALENDAR_CLIENT_SECRET=<server-only-client-secret>
   GOOGLE_CALENDAR_REDIRECT_URI=http://localhost:3000/calendar/google/callback
   GOOGLE_CALENDAR_ENCRYPTION_KEY=<generated-base64-key>
   GOOGLE_CALENDAR_SYNC_SECONDS=300
   DEBUG=false
   ```

6. Apply `g096_google_calendar` after `d096_schedule_export`, using the migration
   owner on an approved database. Runtime processes must use the restricted role,
   never the owner. Existing `ReadSchedule` seeding remains sufficient.
7. Configure the student frontend callback as described below and start the
   worker with the same Google settings and database as the student API:

   ```powershell
   uv run python -m cbpupsis_api_student.calendar_worker
   # Or a bounded scheduler invocation:
   uv run python -m cbpupsis_api_student.calendar_worker --once --max-accounts 50
   # Local compose, only after configuring env/student/env.dev:
   docker compose -f docker/docker-compose.yml --profile google-calendar up -d google-calendar-worker
   ```

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
  access to its secret/encryption key. Use the deployment's Scalar URL; the
  isolated development preview used `http://localhost:8002/scalar`.
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

### QA acceptance checks and safe evidence

- Repeat unchanged `/sync`: no extra calendar or series. Reconnect the same
  Google account: reuse the known calendar; do not adopt one by display name.
- Have an authorized fixture operator change one synthetic meeting's day/time/
  room, then synchronize. The original slot must disappear, the updated meeting
  appear once, and other meetings remain unchanged. These Google endpoints do
  not edit SIS enrollment or implement approval workflows.
- Drop and re-add one synthetic course through an approved fixture workflow.
  Its lecture/lab series disappear, then return once; other courses are retained.
  Re-added series intentionally get new remote incarnations after deletion.
- Check the fixture's term start/end and configured SIS holiday dates. Google's
  separate national-holiday overlay is not an additional exclusion source.
- Report only sanitized HTTP/error/status fields, fixture step and non-sensitive
  observations. Hide bearer values, authorization/redirect URLs, code/state,
  Google account identity, calendar IDs and personal events in QA screenshots.
  Record queued 202 separately from completed sync and Google Calendar UI proof.

External/testing grants for these scopes normally have seven-day refresh-token
expiry ([Google OAuth guidance](https://developers.google.com/identity/protocols/oauth2#expiration)).
Complete Google's audience,
consent-screen, app-verification and data-policy requirements before production.
Do not assume permanent access based on a successful test grant. Check current
Google quota/billing terms; rate limits and hosting costs still apply.

## Frontend callback and privacy contract

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
worker scheduling/alerts and quotas separately. Keep the PR draft until review
and these external checks are resolved. No shared-database tests are permitted.

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
