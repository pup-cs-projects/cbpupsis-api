# Student grades and transcript: US-STU-004

Backend task #97, parent story #75. These reads use the existing academic
tables. They do not implement faculty submission, corrections, incomplete expiry,
prerequisite decisions, or academic standing evaluation.

Temporary validation may use synthetic producer-output records in isolated
development/test databases, as approved by the PM. Authentication, ownership,
read services, calculations and exports remain real; the production APIs never
substitute fake data. This validates consumption of stored outputs, not faculty
approval workflows or the standing engine. Integration with #102, #103 and #105
remains follow-up when those producers are available.

## HTTP contract

All paths have prefix `/api/v1` and are served only by the student app (and the
composed development app). Responses are ordinary typed JSON objects, not a new
success envelope. Errors retain the existing application envelope.

| Method | Path | Parameters / result |
|---|---|---|
| GET | `/students/me/grades` | Optional paired `year` and `semester`; one term |
| GET | `/students/me/transcript` | `page=1`, `limit=20` (1 through 100); term blocks |
| GET | `/students/me/transcript/export` | Complete in-memory PDF, unaffected by JSON pagination |
| GET | `/students/{student_number}/grades` | Identified self-read only; foreign/missing identifiers both return 404 |

Use `/students/me/...` for frontend self-service. `me` resolves to the live
authenticated student, not a client-supplied identifier. These parent-story
paths are the single public contract; the earlier, unpublished `/student/...`
paths are not retained as aliases or redirects. The identified grades path is
kept only for the parent acceptance contract and does not grant staff access or
allow another student's records to be read.

An omitted term selector means the most recent term with published final marks,
ordered by term start date, end date, then UUID descending. It does not mean the
global current term, the most recently encoded mark, or a term with only drafts.
Supplying only one selector or blank selectors returns 422. An unavailable
selection returns an explanatory empty state, not a fabricated average.

Both read permissions are seeded to Students through GradeReader and
TranscriptReader. `ReadGrade` protects grades; `ReadTranscript` protects both
transcript formats. Re-run the normal IAM seed during deployment. Permissions do
not bypass the live student-session and own-record checks. No staff grade-reading
endpoint or cross-student grant is added.

### Grades response

- `student`: `full_name`, `student_number` for the caller only.
- `term`: `id`, `academic_year`, `semester`, `start_date`, `end_date`, or null.
- `courses`: `course_id`, `enrollment_id`, `code`, `title`, `units`, `final_grade`,
  `status`, `posted_at`, `completion_deadline`.
- `gwa`: a two-place Decimal string or null; `units_earned`: Decimal string.
- `academic_standing`, `unitReduction`, `standing_state`, `standing_message`,
  `standing_evaluated_at`.
- `state`: `available` or `empty`; `message`: explanatory text for an empty state.

Final marks are `1.00`, `1.25`, `1.50`, `1.75`, `2.00`, `2.25`, `2.50`, `2.75`,
`3.00`, `5.00`, `Inc`, or `W`. Stored `INC` is normalized to `Inc`. Status is
`Passed`, `Failed`, `Incomplete`, or `Withdrawn`; it is never conveyed by color
alone. `posted_at` is the stored final submission timestamp, not the read time.

Standing is read from the selected term's existing evaluation, not inferred from
GWA. It has exactly six values: Good Standing, Warning Level 1, Warning Level 2,
Probation, College Dropping, University Dismissal. `Suspension` is invalid.
Reductions are 0, 0, 3, 6 for the first four respectively. The last two return
null reduction, not permission to enroll: #105 owns their enrollment block.
Missing evaluation returns null standing/reduction, `not_evaluated`, and an
explanation. A correction can make that stored evaluation stale until its owning
workflow recomputes it; the evaluation timestamp remains visible.

### Transcript response

`student`, `terms`, `cumulative_gwa`, `total_units_earned`, `page`, `limit`, `total`,
`nextPage`, `state`, and `message`. `total` counts terms, not courses. Term blocks
contain `term`, `courses`, `gwa`, `units_earned`, `cumulative_gwa`,
`total_units_earned`, and `completed`. Terms with no published marks are omitted.
Terms and courses have deterministic ordering; courses sort by code then
enrollment UUID. A page beyond the last returns empty terms but retains the
record's totals and `available` state when history exists.

The array runs newest first. Each block's cumulative values cover history from
the earliest term through that block, not the first page through that block.
Top-level totals cover the whole record independently of `page` and `limit`.
The PDF contains the whole record, never just one JSON page.

## Computation policies agreed for #97

- Arithmetic uses Decimal for marks, units, weighted totals, division, and
  rounding. JSON decimals are strings. Clients must not treat them as floats
  when calculating academic results; the server owns the results.
- GWA = sum(mark * units) / sum(eligible units). A lower GWA is better.
  Numeric failures (`5.00`) participate. NSTP course codes (`NSTP`, `NSTP 1`,
  `NSTP-1`, `NSTP1`, case-insensitive) and all supported nonnumeric marks are
  absent from both sides. No intermediate term average is reused in cumulative
  computation. Only returned averages round, to two places with ROUND_HALF_UP.
- All eligible finalized attempts count toward GWA; no latest/best-attempt
  replacement is applied. Every attempt remains visible. Earned credits count
  each catalog course UUID once after its first pass. Term credits deduplicate
  within the term; running/overall credits deduplicate across terms. Passing
  NSTP can earn credit even though NSTP never affects GWA.
- Failures, W and uncompleted Inc earn no credits. Inc remains neither Passed nor
  Failed. Its stored deadline is displayed; #102 must encode it as submission
  date plus 365 days and owns conversion/completion. This GET never mutates it.
- A completed term has `end_date` earlier than today's Asia/Manila date.
  Ongoing terms with finalized marks remain visible with their own GWA, but are
  excluded from cumulative GWA. Earned units include all published passes,
  including ongoing terms. Future term-close state can be integrated by #105.
- No eligible numeric units means GWA null, never `0.00`. Drafts, unlocked rows,
  missing final marks and marks without a submission timestamp are not published
  records.
- Live reads recompute from current final marks; there is no average cache.
  The posted marks are loaded once per response and reused for all its totals.
  Invalid marks, missing metadata or an Inc without its stored deadline return
  an opaque 503 instead of silently inventing academic results. PASS/FAIL
  extensions belong to the separate pass/fail story and need a contract update.

Example: 3 units at 1.25 and 1 unit at 2.75 produce 6.50 / 4 = **1.63**.
Adding 3 units of NSTP at 5.00, Inc, or W does not alter that GWA. Adding an
ordinary 3-unit 5.00 changes it to 21.50 / 7 = **3.07**, without earning more
credits. Repeating a passed 3-unit course at 1.00 changes GWA but awards no
additional cumulative credits for that course.

## Privacy and PDF

Successes and authorization/validation errors carry `Cache-Control: private,
no-store`. PDF downloads use `application/pdf`, a fixed non-identifying attachment
filename, and `X-Content-Type-Options: nosniff`. Rendering is off the event loop,
limited to two active renders per process, with a 15-second request deadline.
Canceled requests retain their render slot until the underlying thread finishes.
No PDF, mark, identity, or token is persisted by the export endpoint.

The PDF has the university header, student identity, unofficial/student-copy
label, term tables with repeated column headings, totals, and page numbers.
Long titles wrap and text is escaped before reaching ReportLab markup.
Its packaged font supports common Latin names, including accents. QA must check
any additional scripts required by the supported student population.

Application refusal logs and Superadmin request-audit paths use route templates,
not the supplied student number. Tokens, names, and marks are not logged by this
feature. Deployment must also redact identifier paths in ingress/Uvicorn access
logs and verify AES-256 encryption for database storage, backups, and replicas.
Those infrastructure controls are not established by application unit tests.

The regenerated `docs/openapi.json` is the client contract handoff. Frontend work
must preserve Decimal strings, null empty averages and unevaluated standing;
provide accessible table headers/status text, page navigation and the private
PDF download. No frontend task is created automatically by this implementation.
