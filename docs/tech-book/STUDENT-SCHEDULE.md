# Student course schedule: US-STU-003 (#96)

## Client contract

The Student API owns these authenticated GET routes beneath `/api/v1`:

| Route | Purpose |
|---|---|
| `/students/me/schedule` | Caller-owned course list, meetings, and totals |
| `/students/me/schedule/export?format=pdf` | Complete printable schedule |
| `/students/me/schedule/export?format=ics` | Complete recurring calendar |
| `/students/{student_number}/schedule` | Explicit self-identity check; foreign and absent identifiers are identical 404 `RESOURCE_NOT_FOUND` |

The parent-story `/students/me/...` routes are canonical for clients and QA.
The earlier draft's deprecated `/student/schedule` and `/student/schedule/export`
aliases have been removed, not merely hidden from Scalar. They return 404; update
clients and QA requests to the canonical URLs before publishing this revision.
The literal `me` is resolved from the session, never as a student number.

A live student session and `ReadSchedule` are required. Run the idempotent IAM
seed after deploying: Students receive `ScheduleReader`. No new read-all grant
is introduced. All successful responses use `Cache-Control: private, no-store`.

`semester` and `year` are optional but must be supplied together. `year` is the
stored academic-year string, for example `2026-2027`, not an integer. Both absent
select the single current term. Missing/ambiguous configuration gives an
explanatory empty response, never an ownership 404. Read responses accept `q`
(maximum 200 characters), matching code, title, or faculty name case-insensitively.
Whitespace-only `q` restores the complete list. Exports always ignore view filters.

The response uses snake_case to match existing student/faculty APIs and the
parent-story naming convention. This user-approved contract change supersedes
the earlier draft's camelCase choice. Data is returned directly at the JSON root;
no `status`/`data` success wrapper is added.

- `student_name`, `student_number`, and `term` (`id`, `year`, `semester`,
  `start_date`, `end_date`), or null when the term is unavailable.
- `courses`: `code`, `title`, `section`, `section_id`, `room`, `faculty_name`,
  `units`, `lecture_hours`, `lab_hours`, `status`, and `meetings`.
- Meetings carry `id`, `day_of_week` (Monday=0 through Sunday=6), local `start_time`
  and `end_time`, nullable `room`, and `type` (`lecture` or `laboratory`). Different
  meeting rooms are preserved; course-level `room` joins the distinct rooms.
- `total_credits`, `total_lecture_hours`, and `total_lab_hours` sum the returned rows.
  All Decimal quantities serialize as strings, consistent with Pydantic's exact
  decimal representation. Contact hours are weekly timetable durations, not
  term-wide totals and not inferred from credits.
- `state`: `ready`, `notEnrolled`, `noMatches`, or `termUnavailable`; empty
  results include a human-readable `message`, empty `courses`, and zero totals.

### Migrating from the earlier draft

This is a breaking JSON property-name change on the canonical self and ownership
read URLs. Update clients and QA assertions before publishing this revision;
there is no dual-spelling response. The earlier deprecated singular `/student/`
schedule routes are removed; migrate those URLs to `/students/me/` as well.

| Earlier draft property | Current property |
| --- | --- |
| `studentName`, `studentNumber` | `student_name`, `student_number` |
| `sectionId`, `facultyName` | `section_id`, `faculty_name` |
| `lectureHours`, `labHours` | `lecture_hours`, `lab_hours` |
| `totalCredits` | `total_credits` |
| `totalLectureHours`, `totalLabHours` | `total_lecture_hours`, `total_lab_hours` |
| Term `startDate`, `endDate` | `start_date`, `end_date` |
| Meeting `dayOfWeek` | `day_of_week` |
| Meeting `startTime`, `endTime` | `start_time`, `end_time` |

Only property names change. Existing `state` values (`notEnrolled`, `noMatches`,
`termUnavailable`), decimal strings, HTTP/error codes, guards, selectors and
filtering semantics stay unchanged. PDF/ICS attachments are not JSON and retain
their existing formats, UIDs and revision behavior. Other domains' contracts are
not renamed by this feature.

Only officially `enrolled` term/course records appear. Pending or dropped courses
are excluded. COR download is not required. Approval workflows are separate;
reads reflect committed enrollment changes without caching.

## Documents and revision semantics

PDF returns `application/pdf`; ICS returns `text/calendar`. Both have attachment
disposition and are generated in memory. No storage service or background job is
required. CPU work is offloaded to a thread. The ten-second export deadline
returns 503 `SERVICE_UNAVAILABLE` with `Retry-After: 30`, never a partial file.
Resource bounds are 100 courses, 1,000 meetings, a 731-day date span, 1,000 holiday
records, and 10,000 historical meeting identities per student/term export stream.

The user approved **one VEVENT per meeting**, clarifying #96's literal one-per-course
wording: lecture and laboratory slots with different times/durations must not be
collapsed into an incorrect recurrence. Events use Asia/Manila, weekly RRULEs
bounded by the academic term, and EXDATEs for inclusive `academic_events` holiday
ranges (`event_type`, case-insensitive, equals `holiday`). Weekday convention is
Monday=0. Timetable writers must follow the same convention.

Stable opaque UIDs identify student/term/section/meeting combinations. Persistent
export state compares canonical schedule/calendar content: unchanged exports keep
their SEQUENCE; changed exports increment it. Removed meetings remain as CANCELLED
tombstones, including older drops, so earlier exports can be superseded. Re-adding
the same meeting restores its UID with a higher revision. PostgreSQL snapshot
transactions and row locks protect simultaneous exports; serialization conflicts
retry within the same deadline. No approved add/drop API is introduced here.

Standard ICS import behavior varies by calendar application: a file alone cannot
force a client to honor updates or cancellations. A real-client import/re-import
test is a release gate, not replaced by parser tests. Automated recurring-update
delivery or subscription feeds are outside this issue.

## Rollout and verification

Apply the new Alembic migration before serving calendar exports. Its explicit
grants mirror named roles already holding ordinary enrollment CRUD privileges;
no PUBLIC/default grants or audit-ledger permissions change. Seed IAM, publish
the generated OpenAPI, and notify the counterpart client story of this contract.
Retain the export-state table during a production code rollback: downgrading
the migration deletes revision history and can reset clients' SEQUENCE values.

Use synthetic fixtures only. Run schedule and affected faculty/auth/IAM tests,
architecture checks, and opt-in isolated PostgreSQL snapshot/concurrency checks.
Verify a printable twelve-course PDF in under three seconds and inspect rendered
pages. Before release, import/re-import in a real calendar client and run the
5,000-student load scenario on an isolated deployment; report either as unverified
until actually executed. Never run migration or load tests against shared data.
