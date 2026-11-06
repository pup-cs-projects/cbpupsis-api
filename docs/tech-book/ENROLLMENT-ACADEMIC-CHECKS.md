# Academic enrollment checks and prerequisite waivers (#99)

## Delivery boundary

PR #209 supplies the real available-courses and submit endpoints on fr003.
This change wires the shared academic evaluator into both and reconciles the
migration chain. It does not implement certificate/assessment changes or
replace the existing submit workflow. Local verification is not CI, reviewer
approval, production-data readiness, or comprehensive QA acceptance.

## Endpoints

All paths below start with `/api/v1` and use the existing error envelope.

| Method/path | Authorization | Response |
| --- | --- | --- |
| GET `/student/enrollment/available-courses?term=...` | Live student session + ReadEnrollment | Existing section list with academic verdict fields |
| POST `/student/enrollment/submit` | Live student session + CreateEnrollment | Existing submission response, or academic refusal before allocation |
| POST `/student/enrollment/waiver` | Live student session + CreateWaiverRequest, session-owned filing only | 202 `{waiverRequestId,state:"PENDING_CHAIR"}` |
| GET `/admin/waiver-requests?limit=50&offset=0` | Live MFA-completed Admin session + ReadWaiverRequest | `{items,total,limit,offset}`, max limit 200 |
| PATCH `/admin/waiver-requests/{waiverRequestId}` | Live MFA-completed Admin session + ApproveWaiverRequest | Updated waiver DTO |

Filing JSON: `{sectionId,prerequisiteCourseCode,condition,justification}`.
`condition` is REPEATED_PREREQUISITE, SUPERIOR_STANDING or GRADUATING.
Justification/course code must be nonblank. Extra body fields are rejected;
neither an identity nor an eligibility assertion can replace server evidence.
The section resolves the term, and must belong to the student's curriculum.
Only a real, still-unmet prerequisite edge can be waived. Duplicate live filings
are refused; already-approved duplicates use REQUEST_ALREADY_DECIDED rather
than pretending approval is still pending. A rejected edge may be filed again
if eligibility still holds.

Decision JSON: `{decision:"APPROVE"|"REJECT",remarks?,vpaaReference?}`.
Remarks are required for rejection. Nonblank `vpaaReference` (max 128 characters)
is required for final approval. This is an external approval reference, **not an
authentication credential**; legacy `vpaa_token` is only its storage column.
Read DTOs also include `studentId`, `sectionId`, `prerequisiteCourseCode`,
`condition`, `justification`, `reviewRemarks`, `vpaaReference` and `createdAt`.

| Position | Visible students | Assigned state -> approved next state |
| --- | --- | --- |
| chairperson | Explicit program department matches own department | PENDING_CHAIR -> PENDING_DEAN |
| dean | Explicit program college matches own college | PENDING_DEAN -> PENDING_VPAA |
| registrar | Institution-wide | PENDING_VPAA -> APPROVED |

Any pending step may REJECT, terminally. Scope-filtered PATCH returns 404
RESOURCE_NOT_FOUND for outside-scope or nonexistent targets. Terminal requests
are refused before testing the caller's step. Finance and all other positions
are refused even if they hold a broad permission. No VPAA account is introduced.
Student sessions cannot read the admin list. A token with MFA claims but no live
session cannot act. Permissions alone never establish role, scope or step.

The locked transition, reviewer metadata, one canonical audit row and one
transactional outbox notification commit together. Failed writes roll all of
these back. Concurrent/replayed decisions cannot repeat a successful transition.
The notification worker uses its existing delivery receipt; there is deliberately
no additional audit subscriber for the decision event. Filing stages one audit
row and has account locking plus a partial unique live-edge index.

## Shared evaluator and real enrollment consumers

Use `enrollment.client.evaluate_academics(db, student_id=authenticated.id,
term=resolved_academic_term, section_ids=proposed_final_section_ids)`.
The term must be an academics-owned `AcademicTermRead`.
It returns `AcademicVerdict`, does not enroll/allocate seats, and never commits.
It batches owner-domain reads through clients; no SQL belongs in the consumer.

- Available-courses batches academic facts once and checks each candidate
  against the student's active registrations independently. Visible PE choices
  are alternatives. It omits off-curriculum candidates, retains the existing
  section/seat/conflict fields, and fails closed on genuine missing metadata.
- Submit recomputes and requires the verdict after schedule conflicts and
  before the existing section lock and seat allocation. It never trusts a
  preview, client blockers, student tags, or waiver state in a body.
- #100/#101: use the same client precondition; certificate/assessment/add-drop
  implementation is not part of #99. Active registrations recognize existing
  `enrolled` plus ENROLLED_PENDING/OFFICIALLY_ENROLLED; dropped rows do not count.
  Any later registration-state change must update all owner readers together.
- Current selection is combined with active registrations, deduplicated by
  course for the PE rule. A future drop workflow must resolve its true final
  active set explicitly; this is not authorization to implement drop here.

Per-section output: `prerequisitesSatisfied`, textual `blockReason`, `blocks`,
`waiverAvailable`. Blocks contain `rule`, `courseCode`, `blockingMark`,
`requiredRange`, and rule-specific `details`. Stable academic ordering is
prerequisite/pending-waiver failures (course-code/section order), third-year
gate, then PE. The first block is raised; prerequisites also return
`details.unmet` with course/mark/requiredRange. Gate details are
peCompleted/peRequired (4), nstpCompleted/nstpRequired (6). Decimal unit counts
are JSON strings, matching grading conventions.

## Academic policy and authoritative data

- Only published, locked, nondraft final marks with a submission timestamp
  qualify. Decimal 1.00-3.00 inclusive passes. 5.00, Inc, W and unfinished
  attempts block and name the mark. An additional unfinished attempt never
  erases an existing pass. No prerequisite produces no prerequisite blocker.
- Advanced (curriculum year >= 3) courses require PE sequence 1-4 plus six
  earned NSTP units. Courses count once, not once per attempt. Use the student's
  curriculum placement, never year-level alone, course-code prefixes or titles.
- More than one PE in a REGULAR term is refused unless both server-owned
  `is_graduating_senior` and `graduating_final_term_id == term.id` hold.
- An approved waiver bypasses only its named section/prerequisite edge. It
  cannot bypass another edge, the gate, PE, holds, unit limits or outer checks.
- REPEATED_PREREQUISITE: finalized 5.00 plus the same prerequisite selected
  in the proposed final selection or actively retaken in the requested term.
  A finalized failed enrollment itself is not a retake. Filing has no client
  selections body, so this condition requires an actual unfinalized active
  retake registration at filing time.
- SUPERIOR_STANDING: grading's canonical Decimal cumulative GWA <= 1.75,
  calculated from finalized completed-term transcript data, not a fabricated
  default or stale standing summary. Missing GWA is ineligible.
- GRADUATING: the server-owned final-term metadata above, not a student claim.

**Academic/product confirmation required:** the repeated-prerequisite definition
is the user's provisional implementation default, not confirmed institutional
policy. Obtain confirmation before treating it as final handbook policy.

Migration `e099_academic_waivers` follows e170_program_college on the linear
chain a098_user_avatars -> e167_enrollment -> e170_program_college -> e099.
It reuses the existing prerequisite, waiver, enrollment, grade, audit and outbox
tables. It adds nullable, explicit Course.pe_sequence/Course.is_nstp,
AcademicTerm.term_type, StudentProfile.graduating_final_term_id and the
Registrar reviewer field; e170 owns Program.college and its CCIS backfill.
Downgrading e099 does not remove e170 data.
`is_nstp = NULL` means unreviewed classification; false + null sequence means
reviewed ordinary course. PE sequence must be 1-4 and not NSTP.

Stored term type is authoritative. Without it, explicit recognized semester
labels (first/second, First/Second Semester, Summer/Summer Term/Summer Semester)
resolve the type. Unknown labels are not defaulted to REGULAR.

Before deployment, catalogue owners must supply a reviewed JSON manifest
of exact course codes. For example, using synthetic codes only:

```json
[
  {"course_code": "DEMO-ORDINARY", "is_nstp": false, "pe_sequence": null},
  {"course_code": "DEMO-PE-1", "is_nstp": false, "pe_sequence": 1},
  {"course_code": "DEMO-NSTP", "is_nstp": true, "pe_sequence": null}
]
```

Use `python -m scripts.seed_course_classifications <reviewed-manifest.json>`
to validate without writing, then `--apply` only after the owner approves the
target database and manifest. The import rejects unknown/duplicate codes and
contradictory classifications; unspecified courses are not defaulted.
Verify coverage for active curricula before enabling enrollment. Academics and
users owners supply any nonstandard term types and applicable graduating final
terms. Missing academic data returns
ENROLL_ACADEMIC_DATA_UNAVAILABLE; missing department/college metadata grants
no scoped reviewer access. Synthetic test mappings do not prove production
readiness. A read-only dev Neon audit at e170 found 53 courses and no explicit
classification columns; a reviewed mapping is still a rollout blocker.
Existing duplicate live waiver edges require operator review before
the unique index can be applied. No automatic rejection/deletion is performed.

Seed/grant the three permissions using `scripts.seed_iam`; Admins get review
permissions, Students filing permission, with separate endpoint role/position
guards. Existing runtime table ACLs cover the new columns; runtime expected
head is updated. On downgrade IAM data is retained to avoid deleting independent
operator grants. Reconcile a later #100 migration head without rewriting history.

## Guarded catalogue writes

`curriculum.client.stage_replace_prerequisites(db, course_id=...,
prerequisite_course_ids=...)` replaces one course's edges without committing.
It rejects self/multicourse cycles before mutation, preserves retained edge
metadata, and uses default grade/corequisite semantics only for new edges.
All catalogue writers must use this operation. PostgreSQL graph-changing
transactions use READ COMMITTED and one transaction-scoped advisory lock held
through commit/rollback, so concurrent writers cannot each add half a cycle.
No catalogue administration route is introduced.

## Focused validation

`tests/domains/enrollment/test_prerequisites.py` and `test_waiver.py` map rules
to AC-ENRL-002.1-.10. They cover owner reads, exact pass boundary, blocking
marks, unique-credit gates, PE/final-term evidence, waiver lifecycle/scope,
live MFA and permission enforcement, effects and rollback. The affected
available-courses/submit tests also exercise independent PE choices, off-curriculum
isolation, stage-7 refusal before seat locking, and the same real submit
succeeding after final waiver approval.

Opt-in PostgreSQL checks use `ISSUE99_POSTGRES_URL` pointing **only to a migrated
disposable database**: graph writer contention in curriculum, and duplicate
waiver-decision contention in `test_waiver_postgres.py`. The latter leaves
append-only audit evidence in that disposable DB; discard the entire test DB
afterward, never delete ledger rows. Full QA acceptance, frontend coverage and
parent load campaigns remain with QA; required CI is not waived.
