# Student account and payment history (#106)

Target: `fr006`. Shared billing remains the assessment owner; this feature does
not add fee administration, payment processing, or a second fee calculator.

## HTTP contract

- `GET /api/v1/student/account`: live student session and `ReadAssessment`.
  Returns the current term's `amountAssessed`, `subsidyApplied`, `netPayable`,
  `paymentStatus`, `holds`, `lines`, `fheQualified`, `fheForfeited`, and
  `fheForfeitureReason`. Each line has `description`, `amount`, `coveredByFhe`.
- `netPayable` is the remaining assessment plus any recorded late fine, after
  subsidy and persisted `amount_paid`, floored at zero. `amountAssessed` includes
  the fine and matches the sum of the displayed lines.
  Payments are not processed or inferred from gateway attempts here.
- `paymentStatus`: `PAID` for zero remaining payable; otherwise `OVERDUE` after
  the stored due date in UTC+8, `PARTIAL` when some payment is recorded, or `DUE`.
- `GET /api/v1/student/payments`: live student session and `ReadPayment`.
  Returns an array of `date`, `amount`, `reference`, `status`, across the caller's
  terms. Ordering is effective payment date descending (`paid_at`, falling back
  to `created_at`), then creation date and UUID descending for deterministic
  ties. No payments returns `[]`.
- All money fields are two-place decimal strings. Non-student sessions return
  404 `RESOURCE_NOT_FOUND` before the operation's permission check. Expired
  sessions retain 401 `AUTH_SESSION_EXPIRED`. Both routes are private/no-store.
- A missing current term/account returns 404. Financial data that cannot be
  verified, or a computed account exceeding the existing Numeric(10,2) storage
  range, returns 503 `SERVICE_UNAVAILABLE`, with `Retry-After: 30`. No partial
  financial mutation or guessed assessment/eligibility decision is returned.

## Approved project decisions (2026-11-16)

- **D1: Registrar verdict.** Billing consumes and audits the existing approved
  `fhe_qualified`, `fhe_forfeited`, and `fhe_forfeiture_reason` values. It does not
  calculate residency or add academic fields, a counter, or Registrar UI.
  User reaffirmed this approach instead of a separate scholars table. Generic
  scholarship membership and completed curriculum units are not substitutes
  for an FHE verdict or applicable benefit-duration conditions. Proposed
  drop/GPA/missed-subject forfeiture triggers are not approved project rules
  and are not implemented.
- **D2: fixed late fine.** Once per student term billing account, after its due
  date in UTC+8, apply PHP 100.00 if gross minus subsidy minus payments is
  positive. The fine is not recurring, percentage-based, or FHE-covered. A fine
  already applied remains payable if FHE is later restored; no waiver is inferred.
  The versioned rule is `FIXED_100_ONCE_PER_TERM_V1` in shared billing constants.
  #108's `late_fee_rate` remains unchanged and does not control this fixed rule.
- **D3: default coverage.** Empty/NULL coverage means all four supported base
  fee codes: TUITION, LAB, REGISTRATION, MISC, for qualified non-forfeited
  students. A nonempty list covers only its selected codes. Fines are excluded.
- These are user-relayed PM approvals for this project. The PUP Graduate School
  [fees page](https://www.pup.edu.ph/gs/Fees) lists a PHP 100 late-payment fine;
  it is not independent confirmation of an institution-wide undergraduate rule.

## Shared state and transactions

- Reuse #108's saved fee schedule and normalized covered-fee codes, including
  the approved empty/NULL default. No historical COR is reinterpreted. Explicit
  non-covered lines remain payable.
- New assessments use the configured fee due date when present, otherwise the
  enrollment owner's supplied due date. Existing assessment due dates are kept.
- Only the current term reconciles approved users-domain FHE flags into its
  stored billing assessment. Historical term decisions and issued COR JSON/PDF
  snapshots remain unchanged. No residency inference uses `year_level` or a
  student-number prefix.
- Financial holds use positive billing-account balances, not stale profile
  flags or payment-status labels. Credits in another term do not silently erase
  a term's debt. Administrative/academic holds remain independent. The legacy
  `has_financial_hold` argument remains accepted for caller compatibility.
- An actual subsidy amount transition stages one audit row, with before/after
  monetary state, eligibility state, coverage rule, fee/term identifiers, and a
  UTC+8 timestamp. Producer staging never commits the enrollment transaction.
- Account reads lock the billing account while reconciling subsidy, staging
  the fine/audit, and staging an overdue notice. `late_penalty_amount`,
  `late_penalty_applied_at`, `overdue_notified_at`, audit and outbox commit
  together; failures roll everything back. Repeated/concurrent reads do not
  reapply the fine or re-stage the notice. An old notice does not waive a fine.
  FHE decisions are refreshed after the account lock, so a queued old read cannot
  overwrite a newer approved decision or create a false audit reversal.
  `billing.account_overdue` uses the existing email/in-app delivery receipts.
- Shared hold evaluation also stages current-term overdue processing in its
  caller's transaction. Rejected enrollment rolls that staging back. There is
  no historical repricing or overdue batch job in this issue.
- Forward migrations: `c191_student_account_overdue` after #108's `c190`, then
  `c192_student_account_late_fine`. Existing table privileges are retained.
  c191 recovers notice markers from durable outbox events on re-upgrade.
  c192 stores the fine separately from the immutable base assessment and makes
  generated net/balance columns include it. It refuses downgrade whenever any
  recorded fine exists, paid or unpaid; explicit owner reconciliation is
  required rather than silently destroying financial history. Zero-fine
  downgrade/re-upgrade preserves balances, payments and base snapshots.

## Acceptance status and deferred integration

- **Revised AC-FIN-001.4/.5:** user confirmed PM approval of the verdict-
  consumption wording below. Billing consumption is tested; automatic
  four-/five-year boundary evaluation belongs upstream and is not claimed.
  User explicitly deferred Registrar workflow identification/verification for
  now. No authoritative update workflow was found in the inspected Admin/users
  code. This is a deferred production integration risk, not a remaining blocker
  to the revised local implementation. The live issue has not been edited.
- D2/D3 approvals supersede the earlier penalty and empty-coverage policy gates.
  Configurable percentage/recurring penalties remain outside the approved rule.
- Local tests cannot establish CI, deployment readiness, or a merge into
  `fr006`. Publication, merge and issue closure require separate authorization.

## Registrar handoff and next acceptance steps

No additional Billing schema or evaluator is needed for the approved approach.
PM approved the revised acceptance boundary; user deferred establishing the
authoritative producer for now. Do not create a second eligibility source or
report the deferred integration as verified.

| Owner | Required next evidence |
| --- | --- |
| BA/PM | Complete: user relayed PM approval of the AC4/5 wording below. Live issue publication is not authorized. |
| Registrar / upstream module owner | Deferred by user: identify the authorized workflow that writes the three existing FHE fields, when evaluation occurs, and how an approved correction is recorded. |
| Registrar / BA | Confirm applicable duration, academic-retention and exception rules. Clarify drop scope, GPA comparison/period, and missed-subject exceptions before any future automation. |
| Upstream owner / QA | Deferred with workflow verification: demonstrate a real authorized verdict change and Billing reflecting subsidy, payable and holds, including restoration without erasing an existing fine or issued COR. |
| Delivery owner | Review migration c192, obtain CI evidence and separately authorize publication/merge into fr006. |

PM-approved AC4/5 scope wording, confirmed by user (not a live issue edit):

> Given a Registrar-approved FHE verdict, Billing applies subsidy only when
> qualified and not forfeited; otherwise it applies no subsidy and returns the
> stored forfeiture reason. Changes reconcile the current-term account and
> audit actual subsidy transitions, while preserving issued CORs. The Registrar
> domain owns residency-duration evaluation and its boundary tests.

This approval assigns boundary evaluation upstream; it does not establish that
the producer exists or is correct. Original four-year/year-five versus year-six
and five-year/year-six versus year-seven boundary tests have not passed in
Billing. They are outside the revised local acceptance scope, not satisfied by
tests that simply set a flag and label it a residency calculation.

Important existing-data limitation: the profile defaults `fhe_qualified=True`
and `fhe_forfeited=False`, and has no explicit reviewed/pending marker. Those
defaults do not prove Registrar evaluation. Before production, the upstream
owner must establish the validity of the stored decisions or propose a
separately approved review-state contract. #106 does not implement a new
pending state, silently classify missing decisions, or charge students based
on scholarship membership alone. FHE eligibility also does not bypass
academic/enrollment checks, non-covered balances, fines, or other holds.

## Focused checks

Use the isolated worktree's locked virtual environment:

```powershell
rtk proxy .venv\Scripts\python.exe -m pytest tests/domains/billing tests/domains/enrollment/test_submit.py tests/domains/enrollment/test_certificate.py -q
```

`test_ac_fin_001_postgres.py` is opt-in via `ISSUE106_TEST_DATABASE_URL` and
`ISSUE106_MIGRATION_DATABASE_URL`. It accepts only the verified disposable
`issue106_synthetic` and `issue106_synthetic_migration` databases, respectively,
on `127.0.0.1:55446`, with a separately provisioned restricted runtime role.
It checks rollback, concurrent processing, runtime ACLs, refusal to discard
recorded fines, and a zero-fine migration roundtrip. Never substitute a
shared/external database.
