# Certificate of Registration and shared assessment (#100)

## Status and boundaries

PR preparation, 2026-11-05: this is the standalone #100 review branch, not the
combined candidate. Dependency PR #209 supplies real #168/#169 endpoints but is
still open at `15d377a250d0244f88a2669cc413ad515dd4ccaa`; its routes use string
term selectors resolved by academics, whereas COR retains UUIDs. A separate
local candidate has verified #99/submit/assessment integration with synthetic
finance outputs. Those integrations and the combined migration are not in this
branch. Port them after the dependency baseline is agreed; retain the #101,
finance provisioning, policy confirmation, manual testing and CI gates.

Core implementation is independent of submit and add/drop. It is **not issue
closure**: real #169 submit-to-issue, #99 academic verdict and #101 approved
add/drop-to-supersession integration, required CI and authorized promotion remain
delivery gates. No substitute submit/approval routes exist here.

The #99 `academic_verdict.certificate_eligible` seam deliberately returns 503
until wired to the real shared prerequisite/gate/PE/waiver verdict. Never replace
it with unconditional eligibility in production. Synthetic test substitutions
prove #100 behavior, not those missing workflows.

## HTTP contract

All paths have prefix `/api/v1` and are mounted explicitly in the student app.

| Method | Path | Access | Success |
|---|---|---|---|
| POST | `/student/schedule/cor` | Live student session + `IssueCertificate` | 201 `certificateId`, `issuedAt`, `status: CURRENT` |
| GET | `/student/schedule/cor?term=<uuid>&format=pdf` | Live student session + `ReadSchedule` | 200 buffered PDF attachment, default format |
| GET | `/student/schedule/cor?term=<uuid>&format=json` | Same | 200 canonical certificate DTO |
| GET | `/student/assessment?term=<uuid>` | Live student session + `ReadAssessment` | 200 stored shared assessment |
| GET | `/public/cor/verify/{token}` | Anonymous; fixed ten checks/minute/IP | 200 exactly five permitted fields |

POST body is `{"term":"<academic-term-uuid>"}`. Extra body fields are rejected.
No route can choose a student by number or ID. Auth/session/permission errors
preserve existing behavior. The term is a typed UUID, resolved through the
academics-owned term reader. #168/#169 have no implemented selector on this
baseline: confirm/reuse their final representation during integration rather
than introducing another string parser or silently changing the wire contract.

No enrollment: 409 `COR_NOT_ENROLLED`. Unmet conditions: 409
`COR_ENROLLMENT_NOT_FINAL`, with `details.unmet` containing `HOLD`,
`PREREQUISITE_VIOLATION` and/or `NO_ASSESSMENT`. Invalid/incomplete/stale assessment
inputs fail closed. A pending enrollment with no issued certificate returns 409
on GET even when the academic integration is unavailable. GET never issues,
calculates a replacement assessment or repairs enrollment. Missing identity is
404 `RESOURCE_NOT_FOUND`; missing encryption/integration/renderer is 503
`SERVICE_UNAVAILABLE`. A failed renderer sends no partial PDF or attachment.

Certificate JSON/PDF share name, studentNumber, program, term (id/year/semester),
every registered course's code/title/sectionId/section/schedule/room/faculty/units,
totalUnits, assessment, certificateId, issuedAt, status and verificationUrl.
Meeting weekdays are Monday=0 through Sunday=6. A missing assignment is null/TBA.
All Decimal amounts and unit quantities serialize as strings.

Assessment has exactly `lines` (code/amount), `amountAssessed`, `subsidyApplied`,
`netPayable`, `fheQualified`, `fheForfeited`, `fheForfeitureReason`. It is read from
stored inputs/results, never from outstanding balance. Missing stored assessment
uses the same 409 refusals above, not an empty paid account.

Public verification has exactly `name`, `studentNumber`, `term` (year/semester,
**no database ID**), `issuedAt`, `status` (`CURRENT`/`SUPERSEDED`). Unknown/malformed
tokens are 404 `RESOURCE_NOT_FOUND`; the eleventh same-IP check is 429
`rate_limited`, even when each URL has a different token. Token hash/envelopes,
courses, assessment and contact details never appear in this response.

Authenticated and verification responses, including refusals, use
`Cache-Control: private, no-store`. PDF uses `application/pdf` and
`attachment; filename="certificate.pdf"`, without identifiers in filenames.
Application logging uses route templates and safe error types, not bodies,
monetary records, student numbers or raw verification URLs. Ingress/proxy/APM
access logs must likewise redact `/public/cor/verify/{token}` and query strings.

## Provisional finance policy: confirmation required

This is a **user-selected provisional minimum**, not finance/product/legal
approval of institutional policy. Obtain finance/product confirmation before
production use. Tuition is configured tuition-per-unit times total registered
units; configured registration, lab and miscellaneous fees are each charged
once per term. Decimal monetary lines round half-up to two places, then sum.
Qualified, non-forfeited students receive the entire subsidy and exactly
`netPayable: "0.00"`. Otherwise subsidy is zero. Read stored FHE flags/reason;
do not adjudicate residency/eligibility. No fabricated fees, late penalties or
financial-hold helper's 1250 fallback. #86 per-fee coverage must be reconciled
explicitly with this zero-net requirement, not silently substituted.

`billing.client.stage_assessment(db, student_id, term_id, courses, due_date)`
is keyword-only after db and takes section IDs/Decimal units. It reads configured
fees and the stored users-domain flags, persists reproducible fee/FHE/course
inputs and line results on StudentBillingAccount, and flushes but never commits.
The producer supplies the actual due date. `calculate_assessment` is pure and
`read_stored_assessment` validates reproducibility without writes. Existing
payment data is not replaced by assessment reads.

## Transaction and dependency integration

Initial issue ends only the HTTP authentication/IAM transaction, starts a
REPEATABLE READ business transaction, locks the student's term enrollment then
current issuance, rechecks preconditions, and stages the encrypted snapshot,
token, official transition and one audit row before one business commit.
Serialization/deadlock and current-index conflicts retry the whole business
transaction up to three times. The partial unique index prevents two CURRENT
certificates. A retry returns original metadata/token without another audit.
Official readers accept legacy `enrolled` and `OFFICIALLY_ENROLLED`, not pending.
Course registration status remains `enrolled`; do not bulk-reclassify old rows.

For #101, before any approval reads/writes call
`enrollment.client.begin_certificate_snapshot(db)` in a clean transaction (or
start an equivalent REPEATABLE READ/SERIALIZABLE transaction). Lock the parent
enrollment first, then approval/current COR and child/account rows in a consistent
order. Apply approved registration changes; call `billing.client.stage_assessment`;
pass its fresh StoredAssessment to `enrollment.client.stage_reissue(db,
student_id=..., term_id=..., actor_id=..., assessment=..., client_ip=...)`.
The hook enforces snapshot isolation, locks enrollment/current COR, supersedes
the old row, creates a fresh token/content and stages audit. It never commits or
rolls back independently. The caller MUST roll back the ENTIRE approval on any
error and retry serialization failures as a whole. Do not catch a hook error and
commit partial approval. Old snapshots/tokens and original issue dates survive.
All submit/add-drop writers must use the same parent-lock protocol.

## Deployment and key lifecycle

1. Inspect legacy issuance counts/statuses as migration owner. Migration
   `e100_cor_assessment` locks the table and refuses duplicate CURRENT rows;
   reconcile with the owner, never arbitrarily delete/supersede history. Existing
   rows retain NULL envelopes: their original tokens/content cannot be invented.
   Reads/retries/verification of these rows fail closed with 503 until an
   explicitly authorized historical reconciliation. Do not fabricate backfills.
2. Apply forward migration from actual integration head, preserving published
   histories. Current parent is `a098_user_avatars` (single existing merged head).
   New columns use existing table grants; runtime roles gain no elevated grants.
   Runtime expected head is `e100_cor_assessment`. Use migration-owner identity
   for migrations and existing restricted runtime identities for APIs.
3. Seed IAM with `scripts/seed_iam.py` as operator, granting CertificateAuthor
   and AssessmentReader to Students, retaining ScheduleReader.
4. Provision `COR_ENCRYPTION_KEY`: dedicated, stable, URL-safe base64 of **32
   random bytes**, stored in the secret manager. Never commit/log a real key or
   reuse JWT_SECRET. Missing/malformed keys fail closed. Tests use synthetic keys.
   Set `FRONTEND_BASE_URL_STUDENT` to the trusted student portal origin (no path,
   credentials/query/fragment); verification URLs use this, never request Host.
5. Set `RATE_LIMIT_ENABLED=true` and `RATE_LIMIT_STORAGE_URI` to shared supported
   storage (for example Redis with the matching limits backend installed) for
   every API instance. Memory storage is local-only, not deployment evidence.
   Only trust forwarding headers behind an ingress that overwrites them; never
   expose the application directly with user-controlled X-Forwarded-For.
6. Wire/validate the real #99/#169/#101 interfaces before enabling issuance.
   Required CI and team-authorized integration/promotion remain mandatory.

Tokens are `secrets.token_urlsafe(32)` (256 random bits), indexed by unique SHA-256
hash. Tokens and immutable content (including snapshot PII) have separate
AES-256-GCM envelopes with random nonces and version/issuance/purpose-bound AAD.
Never persist/log plaintext tokens. Later reads decrypt the same snapshot/token,
not the mutable catalogue, billing account or a rotating JWT-derived key.

**Do not rotate COR_ENCRYPTION_KEY in place.** Quiesce COR writers/readers and
coordinate a backed-up owner-only re-encryption migration: temporarily disable
the content-immutability trigger inside the owner transaction, decrypt every
nonlegacy envelope with the old key/AAD, encrypt with the new key and same AAD,
verify unchanged decoded content/hash/verification URL, restore the trigger,
commit, and deploy the new key to all instances together. On any failure roll
back everything and retain the old key. Keep a tested backup recovery path;
losing this key makes reprints and historical verification unrecoverable.

Focused tests in `tests/domains/enrollment/test_certificate.py` cover the nine
AC identifiers plus security/rollback checks. The AC .8 internal hook test is
NOT the required real add/drop approval acceptance case. A separate opt-in
PostgreSQL check exercises migration, concurrency and snapshot/trigger behavior.
It also checks rollback after business/audit flush and legacy duplicate preflight
without fabricated historical tokens/content. For PostgreSQL runs, supply
`COR_TEST_DATABASE_URL` for concurrency and a **separate fresh**
`COR_LEGACY_TEST_DATABASE_URL` for legacy migration tests. Both must be local
disposable `cor_issue100_*` databases owned by the test migration operator;
nonempty databases are refused. Without those variables the opt-in checks skip,
so an ordinary SQLite-only run is not migration/concurrency acceptance evidence.

Concurrent development note (2026-11-04): #99 is in the separate issue99 worktree,
not integrated here. Its draft `enrollment.client.evaluate_academics(db,
student_id=..., term=..., section_ids=...)` returns `AcademicVerdict.blockers`.
Confirm that contract/readiness when wiring the fail-closed adapter. Its draft
`e099_academic_waivers` and this `e100_cor_assessment` both follow
`a098_user_avatars`: reconcile actual integration heads with a forward merge
revision and update the runtime head; do not rewrite or copy unfinished work.
