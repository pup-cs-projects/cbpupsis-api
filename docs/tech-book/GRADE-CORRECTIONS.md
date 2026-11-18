# Correction of Entry (#103)

## HTTP contract

| Method | Path (prefix `/api/v1`) | Permission |
| --- | --- | --- |
| POST | `/faculty/sections/{section_id}/grade-corrections` | `CreateGradeCorrection` |
| GET | `/faculty/grade-corrections` | `ReadGradeCorrection` |
| GET | `/faculty/grade-corrections/{correction_id}` | `ReadGradeCorrection` |
| GET | `/admin/grade-corrections` | `ApproveGradeCorrection` |
| GET | `/admin/grade-corrections/{correction_id}` | `ApproveGradeCorrection` |
| PATCH | `/admin/grade-corrections/{correction_id}` | `ApproveGradeCorrection` |

POST is multipart with `student_id` (student user UUID), `proposed_mark`, `reason`,
and `grading_sheet`. Marks use the existing finalization scale (1.00-3.00 in
0.25 increments, 5.00, or INC). Reasons must be nonblank and at most 4000
characters. The section must belong to the faculty member and the enrollment
must have a locked, published final grade. Filing returns 201 and does not alter
the grade or GWA. Filers can read only their own requests.

The sheet is at most 10 MiB (10 * 1024 * 1024 bytes). MIME claims and filenames
are ignored: PDFs are structurally parsed; JPEG/PNG are verified and decoded.
Malformed, encrypted, empty-page PDFs and unsupported image formats are refused.
Documents remain private in the grading-owned database record, following the
existing binary-storage pattern. Public DTOs contain an opaque file identifier,
detected content type, byte count, and SHA-256, never document bytes or public
URLs. No document-download endpoint is added by this issue.

List responses contain `items`, `total`, `limit` (default 50, maximum 100), and
`offset`. Detail and list items use camelCase. Details include the current/proposed
mark, state/version, actors/timestamps for approvals, and rejection remarks.
Successful responses are private/no-store.

## Window and release contract

Release is the existing #177 publication instant: `StudentGrade.submitted_at`,
written together with `SectionGradeSubmission.finalized_at` when finalization
makes grades visible. Corrections never rewrite that release timestamp.

`GRADE_CORRECTION_WINDOW_REGULAR_SEMESTERS` defaults to 1 (range 1-10). The
deadline is the inclusive end of that many *actual regular semesters starting
after release*, using the academic calendar and Asia/Manila dates. Summer/midyear
terms do not count. Filing exactly at the deadline is permitted; later filing
returns 403 `CORRECTION_WINDOW_CLOSED` with `windowClosedAt`. The selected deadline
is saved on the request. A missing/unclassified future calendar fails closed
with 503 `GRADE_RECORD_UNAVAILABLE`; there is no fixed-day fallback.
Missing catalog department/college scope likewise refuses filing rather than
inventing an approver scope or saving an unroutable request. Populate catalog
scope and the configured number of future regular terms before deployment.

## Decisions, authorization, and atomicity

PATCH requires `{ "action": "ENDORSE|COMMIT|REJECT", "version": 1 }` and optional
`remarks`. Only the server-derived administrative position determines a step:

- Chairperson: own department, `PENDING_CHAIR` -> `PENDING_DEAN`.
- Dean: own college, `PENDING_DEAN` -> `PENDING_REGISTRAR`.
- Registrar: all corrections, `PENDING_REGISTRAR` -> `COMMITTED`.
- Finance: no seeded permission and explicitly denied even if accidentally granted.

Current course/section scope is resolved through the curriculum and sections
owners before pagination and again before decisions. Foreign details/PATCH return
the same 404 as nonexistent records. Only the current approver can reject;
nonblank remarks are required. Rejection preserves grades/GWA. The filer and any
previous actor may not act on another step, including after changing positions.
Session/MFA checks use the existing role dependencies, not client role claims.

Decisions lock the student's account, then the fresh correction, then its grade.
This serializes concurrent correction-driven GWA updates even without a cached
GWA row. Every decision checks the correction version; Registrar also checks the
filed source mark and its update timestamp (including same-mark/ABA changes).
There is no last-write-wins retry or double application.

Registrar changes the mark, recomputes GWA using existing Decimal grading math,
updates existing term/cumulative cache rows (including later evaluations), and
stages one protected audit row in the same transaction. Any failure rolls all of
that back. Student grade/transcript reads already derive GWA directly from posted
marks, so an absent cached evaluation does not require a fake Good Standing row.
Undefined averages remain null, never zero. Existing academic-standing decisions
and their evaluation timestamps are preserved; standing evaluation belongs to
#105, not this issue. Student login deactivation does not prevent an authorized
faculty/approver correcting an existing record; student self-read guards are
unchanged. A correction
to INC reuses the grading owner's configured `INC_COMPLETION_DAYS` from the
original publication instant and records `PENDING_COMPLETION` for the INC workflow.

Each successful filing/decision writes exactly one audit row with old/new marks,
actor, role/position, client IP, and UTC+8 presentation of its UTC instant. Free
text and student personal fields are masked in audit payloads. Refused decisions
produce no successful-step row. No replacement for #226's logging work is added.

## Migration and deployment

Run owner-managed migrations to `f103_grade_corrections` and seed IAM. The chain is
`f177_section_grade_submissions` -> `f178_inc_lifecycle` -> `f103_grade_corrections`;
#103 preserves the INC tables/indexes and does not replay or replace #178.
The migration
adds private document metadata/content, source-version evidence, scope identity,
and rejection resolution metadata to the existing correction table. It backfills
only section/student relational identity for legacy rows; it does not invent
documents, release deadlines, source versions, or audit history. A legacy pending
request without source-version evidence cannot be committed silently.

Cached GWA columns become nullable for wholly nonnumeric/excluded records.
Downgrading refuses if null averages remain, rather than replacing them with
invented zeroes. Downgrade also removes newly stored document evidence; operational
rollback requires an approved retention/backup decision before using it on data.

Targeted tests are `tests/domains/grading/test_grade_corrections.py` and
`test_grade_corrections_postgres.py`. The latter refuses any URL except the
explicit disposable loopback database and tests real migrations, restricted-role
audit UPDATE/DELETE denial, transaction rollback, and concurrent decisions.
Local evidence is not a claim of remote CI or authorized merge/issue closure.
