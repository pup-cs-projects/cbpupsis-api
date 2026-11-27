# Issue #156: ICS interoperability record

Status: needs-follow-up (updated 2026-11-17). #147 integration verified locally.
Original-file Google evidence covers initial timing, holiday and term bounds.
Duplicates are visible after a confirmed mixed-set import (original initial file,
refreshed unchanged file). Controlled integrated-file retry G7-G9 shows no
duplicates after unchanged import, correct term end and lecture removal after
drop in `156-clean`. Current-run exact end-time details/context gaps and all
Outlook checks remain outstanding.
Operator requested deferring Outlook on 2026-11-17. It is unverified, not passed;
the original two-client acceptance criteria are unchanged.
Scope: [issue #156](https://github.com/pup-cs-projects/cbpupsis-api/issues/156),
AC-STU-003.6, .8 and .13 only. No subscription or Google sync testing.

## Provenance and dependency gate

- Prepared 2026-11-11 on branch `api-us-stu-003-156`, target `fr004`.
- Delivery branch: `api-fr004-schedule-ics`, renamed for the repository's branch
  naming check; target remains `fr004`. This is partial issue work, not closure.
- Original base: `a545af0d4ffd05bde849024b98d6ec9b4c126a96` after an isolated
  `git pull --ff-only origin fr004` (already up to date).
- Fixture-generation integrated base: `48fb0b32cf6ad79270a6897368e2d993ec83b18d`.
  [PR #227](https://github.com/pup-cs-projects/cbpupsis-api/pull/227) merged into
  `fr004` on 2026-11-12; live PR status and fetched code both confirm #147's
  implementation landed. Issues #147 and #156 still show open with no comments;
  issue closure is not the integration criterion and was not changed here.
- Local commit preparation on 2026-11-17 rebased the issue-only change onto
  `fr004` at `d4bb2fa5a59a40c8e1f32b3f16e40729fb95eb6e` (#229). The saved
  fixture files keep their original bytes/run identities; they were not
  regenerated or mixed. No push or PR publication was performed.
- Our branch was fast-forwarded to that commit without any commit or stash.
  Issue-only tracked edits were preserved in an external Git-generated patch,
  reapplied, and left unstaged. An adjacent renamed-test conflict was resolved
  retaining both our new regression and #147's migrated test.
- **#147 integration now passes locally.** The authenticated regression exports
  through `/api/v1/student/schedule/export?format=ics`. Existing tests verify
  removed paths return 404, no plural student routes remain, and permissions
  remain unchanged. No route implementation was added by #156.
- Original 2026-11-11 files remain historical baseline evidence. Current files
  are separately preserved in [fr004-48fb0b3](evidence/issue156/fr004-48fb0b3/).
  Do not combine these runs in a calendar: their synthetic identities differ.
- The HTTP test uses the real composed app, student login/session, IAM grant,
  service/repository and ICS renderer. Only `get_db` is overridden to a fresh
  in-memory SQLite session. No database migrations or deployed services run.
- Drop is a committed synthetic registration-status change, not an invocation
  or verification of an approved add/drop workflow.

## Fixture and exact expectations

The regression `test_ac_stu_003_6_calendar_client_fixture_tzid_until_exdate_sequence`
in `tests/domains/schedule/test_schedule.py` adapts `seed_schedule` in `helpers.py`.
All people, rooms and courses are synthetic. The student number is never in ICS
content or UIDs. Two different course registrations permit dropping the lecture
while retaining the lab.

| Property | Value |
| --- | --- |
| Academic term | first, 2026-2027; Tuesday 2026-08-04 through Friday 2026-12-18 inclusive |
| CS001 lecture | Algorithms & <Practice>; Monday 08:00-10:00 Asia/Manila; R101 |
| CS002 lab | Course 2; Wednesday 13:00-16:00 Asia/Manila; LAB2 |
| Configured holiday | Single `holiday` record, 2026-08-17 only |
| First lecture / lab | 2026-08-10 / 2026-08-05; none before term start |
| Last lecture / lab | 2026-12-14 / 2026-12-16; none after term end |
| Holiday check | No lecture 2026-08-17; lecture returns 2026-08-24; lab on 2026-08-19 stays |
| Term end wire value | `UNTIL=20261218T155959Z` (23:59:59 Manila) |
| Holiday wire value | `EXDATE;TZID=Asia/Manila:20260817T080000` on lecture only |
| Before / unchanged | Two CONFIRMED series, SEQUENCE 1; stable opaque UIDs |
| After CS001 drop | Same two UIDs, SEQUENCE 2; CS001 CANCELLED, CS002 CONFIRMED |

The first lecture is the first Monday on/after term start, not the Tuesday start
itself. Term end is a Friday: last occurrences need not fall on that date.
Expected expanded counts before dropping are 18 lecture and 20 lab occurrences;
after dropping, zero active lecture and the same 20 lab occurrences.

## Reproduce files and automated checks

From this issue's worktree, with Python 3.12 and uv installed:

```powershell
uv sync --frozen
$env:PATH = "$PWD\.venv\Scripts;$env:PATH"
New-Item -ItemType Directory -Path .pytest_cache -Force | Out-Null
rtk pytest tests/domains/schedule/test_schedule.py -q --basetemp=.pytest_cache/issue156
.\.venv\Scripts\ruff.exe check tests/domains/schedule/test_schedule.py
.\.venv\Scripts\ruff.exe format --check tests/domains/schedule/test_schedule.py
git diff --check
```

The new test's `tmp_path` contains these **actual HTTP response bytes** (use
`-s` to print the directory, or locate it beneath the chosen `--basetemp`):

1. `01-before-drop.ics`: initial two-series export.
2. `02-unchanged.ics`: unchanged re-export, same UIDs/revision/event content.
3. `03-after-drop.ics`: committed CS001 drop followed by export.

If the RTK pytest launcher stalls on Windows, use
`rtk proxy .\.venv\Scripts\python.exe -m pytest` with the same test arguments.

Keep all three files from the **same run**. IDs are random per run; combining
files from separate runs invalidates update/deduplication testing. Pytest's
`--basetemp` clears that test-output directory on subsequent runs. Copy the
successful run's files before rerunning, and record SHA-256 hashes with
`Get-FileHash -Algorithm SHA256 -LiteralPath <file>` alongside the client evidence.

The regression asserts exact TZIDs, UTC UNTIL and first/last bounds,
exact EXDATE (not just an allowed subset), stable unchanged event content, stable
UIDs, incremented SEQUENCE and cancellation/retention after the drop.

### Actual local results (2026-11-11)

- Windows host; Python 3.12.0, pytest 9.1.1, icalendar 7.3.0, Ruff 0.16.5,
  locked dependencies installed in this worktree's own `.venv` with `uv sync --frozen`.
- Initial Ruff check found a `time` import collision; fixed using `local_time`.
  Final affected-file Ruff check and format check both passed.
- Sandboxed/RTK pytest launcher attempts stalled and were interrupted; no results
  claimed. Explicit `rtk proxy .\.venv\Scripts\python.exe -m pytest` ran outside
  the sandbox against fresh in-memory SQLite only.
- First completed module run: 42 passed, 2 setup errors because the new worktree
  lacked `.pytest_cache`, the parent of the explicit `--basetemp` directory.
  Created that directory, then reran only the two affected tests: **2 passed**,
  including the new regression and the existing PDF test. All 44 module tests
  therefore passed across these runs; no single all-green module run is claimed.
- `git diff --check` passed. No CI or external-client verification performed.
- Preserved successful-run response bytes in [evidence/issue156](evidence/issue156/).
  Same-run SHA-256 values (do not edit the ICS files):

| File | SHA-256 |
| --- | --- |
| [01-before-drop.ics](evidence/issue156/01-before-drop.ics) | `5B144861528FD4644F1720657DB0121240892D675CE77A083B87BD6F4C2DDAE6` |
| [02-unchanged.ics](evidence/issue156/02-unchanged.ics) | `5B144861528FD4644F1720657DB0121240892D675CE77A083B87BD6F4C2DDAE6` |
| [03-after-drop.ics](evidence/issue156/03-after-drop.ics) | `35A764072EAA8F2883071EB8166F320271885F74C44A485AE1AEAF7C3FAD43EA` |

Identical before/unchanged hashes confirm byte-for-byte stable re-export for this
run. They do not demonstrate either client's deduplication behavior.

### Integrated local results (2026-11-12)

No locked dependencies changed in PR #227. The existing isolated `.venv` uses
this worktree's sources. Commands run against the integrated base:

```powershell
rtk proxy .\.venv\Scripts\python.exe -m pytest tests/domains/schedule/test_schedule.py tests/apps/test_student_route_migration.py::test_every_removed_student_path_returns_404 -q --basetemp=.pytest_cache/issue156-fr004-48fb0b3
rtk proxy .\.venv\Scripts\python.exe -m pytest tests/apps/test_entrypoints.py::test_student_route_inventory_has_no_plural_paths tests/apps/test_entrypoints.py::test_migrated_student_route_permissions_are_unchanged -q
```

- First command: **38 passed in 26.64s**, including this issue's TZID, UNTIL,
  EXDATE and SEQUENCE regression through the new endpoint, plus old-path 404s
  with/without authentication in both composed and student apps.
- Second command: **3 passed in 0.33s** (inventory parametrized over both apps,
  plus unchanged permissions). These are local results, not CI or deployment QA.
- Affected-file Ruff lint and format checks passed. Issue-only diff contains
  no route, exporter, service, repository or migration changes.
- Preserved actual same-run HTTP response bytes below. `.gitattributes` in the
  evidence directory disables line-ending conversion for ICS so downloaded
  CRLF/folding and these byte hashes survive any later Git checkout.

| Current file | SHA-256 |
| --- | --- |
| [01-before-drop.ics](evidence/issue156/fr004-48fb0b3/01-before-drop.ics) | `FB3091C24FF334316E4BF91F6791E18C02F93815D275EA0C17FAEA4F93964E19` |
| [02-unchanged.ics](evidence/issue156/fr004-48fb0b3/02-unchanged.ics) | `FB3091C24FF334316E4BF91F6791E18C02F93815D275EA0C17FAEA4F93964E19` |
| [03-after-drop.ics](evidence/issue156/fr004-48fb0b3/03-after-drop.ics) | `34FBF770159C6B7C9717C1672593F5A3F8CBE5B7A0C92033E8FB02565B9BBA9E` |

Structured icalendar comparison of each original/current file pair found event
properties identical except UID, DTSTAMP and LAST-MODIFIED. That explains the
unchanged fixture expectations but does not prove real-client import behavior
for the current run. Old G1 observations stay labeled as baseline evidence.

### Commit-preparation validation (2026-11-17)

After the rebase onto `d4bb2fa`, one focused run passed **41 tests in 38.13s**:

```powershell
rtk proxy .\.venv\Scripts\python.exe -m pytest tests/domains/schedule/test_schedule.py tests/apps/test_student_route_migration.py::test_every_removed_student_path_returns_404 tests/apps/test_entrypoints.py::test_student_route_inventory_has_no_plural_paths tests/apps/test_entrypoints.py::test_migrated_student_route_permissions_are_unchanged -q --basetemp=.pytest_cache/issue156-commit-d4bb2fa
```

Affected-file Ruff lint/format checks and installed local commit hooks passed.
The six staged ICS blobs matched their documented SHA-256 values; the scoped
attributes preserve CRLF and recognize it during whitespace checking. No CI,
Outlook verification, or screenshot redaction/publication is claimed.

## Safe manual procedure (user-assisted)

Use only test accounts/calendars the operator explicitly authorizes. Do not use
an existing personal calendar, invite guests, publish calendars, or enable sync.
The user has offered to perform both imports; no account access is delegated to
the agent. For the integrated files, use a fresh empty disposable calendar for
each client. `CBPUPSIS-156-48fb0b3` is a suggested name, not a requirement; `156`
is also fine. Use only the three files linked in the current file table above.
Do not mix them into a calendar containing the older file set; new UIDs would
invalidate duplicate/update checks. The calendar's contents, not its name,
determine whether it is a clean test target.
Display only the new test calendar while checking duplicates.
Set its display timezone to Asia/Manila (UTC+08:00); record any timezone setting
changes for restoration. Never delete events to make a re-import check pass.

Record before starting: actual date/time, OS/version, browser/version, calendar
product/variant (Google Calendar web, Outlook.com, Outlook web, new Outlook or
classic Outlook), displayed build/version if available, timezone and test-calendar
name. Web clients without a visible build must say "build not exposed" and
include browser/version and test timestamp; never invent a version.

### Google Calendar web

Use Settings > Import & export > Import, select `01-before-drop.ics`, and explicitly
choose the disposable calendar before importing. See Google's
[official import instructions](https://support.google.com/calendar/answer/37118?hl=en).
If labels differ, record the actual import path rather than assuming success.

### Outlook.com / Outlook on the web

In Calendar, choose Add calendar > Upload from file, browse to
`01-before-drop.ics`, choose the disposable calendar and import. This is a file
upload, not Subscribe from web. See Microsoft's
[official upload instructions](https://support.microsoft.com/en-us/outlook/import-or-subscribe-to-a-calendar-in-outlook-com-or-outlook-on-the-web).
For desktop Outlook, record the actual product and import flow; do not label
Outlook.com observations as desktop verification.

### Perform the same checks in both clients

1. Import **only `01-before-drop.ics`**. Record the import message, including
   imported/skipped/error counts. Confirm two recurring series and the expected
   08:00-10:00 Monday lecture and 13:00-16:00 Wednesday lab in Manila time.
2. Inspect the weeks of 2026-08-03 and 2026-07-27: lab starts August 5, lecture
   August 10; no August 3 lecture or any July occurrence. Inspect recurrence
   details if offered. Check an ordinary later week to confirm weekly times.
3. Inspect August 17-23: no Monday lecture, Wednesday August 19 lab remains.
   Inspect August 24 to confirm lecture resumes. This is not deleting an event.
4. Inspect December 14-20 and December 21-27: last lecture December 14 and last
   lab December 16; no December 21/23 occurrences. Capture boundary evidence.
5. Import **`02-unchanged.ics` into the SAME calendar**, without clearing it.
   Record the import message. Check an ordinary week still has one lecture and
   one lab, not duplicate overlapping entries. Record skipped imports honestly.
6. Import **`03-after-drop.ics` into that SAME calendar**, again without clearing
   or editing events. Record the import message. Lecture must be removed or
   visibly cancelled (not an active class); lab must remain at 13:00-16:00 with
   no duplicate. Inspect both an early and later week plus series details.
7. Capture real screenshots of import messages, timezone/time details, first and
   last weeks, holiday week, unchanged re-import and after-drop state. Crop/redact
   account email/avatar and unrelated calendars/notifications. Keep synthetic
   titles, dates, times, target-calendar name and relevant statuses visible.
   Store under a local evidence folder and reference exact filenames below.
8. Report PASS/FAIL per matrix cell with screenshot references. An import error,
   stale active lecture or duplicate is a failure, not a reason to change files,
   delete the calendar or switch to a sync integration mid-test. Leave evidence
   intact until captured; afterward only the operator may remove the disposable
   calendar and restore timezone settings.

## Two-client evidence matrix

PENDING means not observed, not passed. PARTIAL means only the stated portion
has evidence; it does not pass the whole row.

### G1: user-supplied initial Google Calendar screenshot

Received 2026-11-11 following the initial-import instructions and the user's
"yes" response. Google Calendar web is visible in August 2026 month view, with
the purple calendar named `156` selected. This is the original baseline run,
not the newly generated integrated run. The screenshot shows:

- CS001 at 8am on August 10, 24 and 31; no CS001 on August 3 or 17.
- CS002 at 1pm on August 5, 12, 19 and 26 (also September 2).
- Thus the displayed first occurrences, weekly start times and August holiday
  exclusion/resumption match the fixture. Other green holiday-calendar events
  are also visible and are not part of the synthetic export.
- It does NOT show event end times, the timezone setting, browser/OS version,
  an import-result message, December bounds, or any re-import/drop result.
  The image alone cannot prove how many recurring series exist in storage.
- User reports initial import/time visibility with "yes"; exact import message
  and product/version context were not supplied. Capture time is unknown;
  received date must not be represented as actual execution timestamp.

Original screenshot preserved unchanged as a local-only artifact outside the
repository (includes unrelated sidebar labels; do not publish without redaction):
`google-calendar-156-august-initial.png`. Operator-held source location is
recorded in the local session handoff, not in this distributable document.

### G2-G4: user-supplied August view and event details

Received 2026-11-13. All three screenshots show Google Calendar web and calendar
`156`. Calendar naming is arbitrary and is not an acceptance requirement.

- **G2, month view:** lecture at 8am August 10/24/31; lab at 1pm August
  5/12/19/26 and September 2. No lecture August 17, with the lab August 19 and
  lecture August 24 visible. No purple CS001/CS002 occurrences appear in the
  displayed July 26-August 3 cells. Other green calendar events are unrelated.
- **G3, lecture detail:** `CS001: Algorithms & <Practice> (lecture)`, Monday
  August 10, **8:00-10:00am**, "Weekly on Monday, until Dec 18, 2026", room R101,
  section BSCS 1-1 and Synthetic Faculty. Expected duration and recurrence limit
  are visible; actual last occurrence in December is not.
- **G4, lab detail:** `CS002: Course 2 (laboratory)`, Wednesday August 5,
  **1:00-4:00pm**, "Weekly on Wednesday, until Dec 18, 2026", room LAB2,
  section BSCS 1-2 and Synthetic Faculty. Expected duration and recurrence limit
  are visible; actual last occurrence in December is not.
- No timezone setting, browser/OS/version, exact import message, unchanged
  re-import or after-drop re-import is shown. Screenshot receipt date is known;
  actual execution timestamp is not supplied. No new client discrepancy is
  established by these images.

**Operator clarification (2026-11-13):** "i imported your given ics, i just named
the calendar 156" and "yes its asia/manila using brave browser". Thus the
timezone is operator-confirmed Asia/Manila and the browser is Brave; neither
numeric Brave version nor OS/version was supplied. The operator reports using
the supplied file. The agent initially inferred the latest integrated link was
used; the subsequent G6 clarification below corrects that inference: the operator
used the original files. G2-G5 must not be attributed to the integrated set.

Originals are preserved unchanged, local-only and outside the repository because
unrelated sidebar/account labels remain visible. Do not publish without redaction.
Source directory is retained in the local session handoff. Filenames and hashes
below identify the operator-held originals; they are not bundled in this commit.

| Evidence | Local filename | SHA-256 |
| --- | --- | --- |
| G2 | `google-calendar-156-20261113-august.png` | `83E378DEEEF0083848A85887542E1D3A020A01643C35C864C8230DDA49BCD3F7` |
| G3 | `google-calendar-156-20261113-lecture-details.png` | `0743E06A1E734AC7578A0F447213D7A059363972392BD56FA20DA9E776B5572A` |
| G4 | `google-calendar-156-20261113-lab-details.png` | `E8AF75C2BF460B6BF2547FF0FC9DA7358A87E510771EF65A26EFB96BA95CE91F` |

Validation for this update: original screenshots visually inspected and preserved
with SHA-256 hashes; automated tests skipped because no code/fixture changed.

### G5: December occurrence boundary

Received 2026-11-13. Google Calendar web, calendar `156` selected, December 2026
month view. Lecture at 8am appears November 30 and December 7/14; lab at 1pm
appears December 2/9/16. No class appears December 21/23/28/30 or the displayed
January 1/2 cells. This confirms the expected last lecture/lab dates and lack
of post-term continuation in the displayed weeks, supplementing G3/G4's
recurrence-until details. Green holiday-calendar events are unrelated.

Original preserved unchanged, local-only, in the same external directory as G2-G4:
`google-calendar-156-20261113-december.png`. Exact execution time, import toast,
browser numeric version and OS are not shown. The later G6 clarification
attributes this initial-import sequence to the original file set.
Screenshot SHA-256:
`E29B69892A0E9512778FD35F7749004E1A223CC45304AEA9F5D362D25A308FE8`.

Browser/version context helps reproduce discrepancies across environments; it is
not a separate functional pass/fail check. Brave with numeric version unreported
is recorded honestly, without halting the remaining import tests or inventing a
Google Calendar build number. Original screenshots and operator reports are the
evidence for the observed calendar behavior, not parser tests.

### G6: duplicate entries and corrected provenance

Received 2026-11-13. Operator says "i used the original files not the refereshed
ones causing duplicates". This corrects the agent's earlier working attribution:
G2-G5 are original-file evidence, not integrated-file acceptance. The screenshot
shows two entries at the same start time for each displayed lecture/lab occurrence
in calendar `156` (including August 5/10/12/19/24/26/31 and September 2).
Lecture is still absent August 17. Original image retained unchanged, local-only,
in the same external directory as G2-G5:
`google-calendar-156-20261113-duplicates.png`.
SHA-256: `EA19765A4CBCD7AEC0696A02AF49064442CAE7FA059C4BF98F9700C3620AB4E1`.

**Sequence clarified by the operator:** "the unchanged.ics is from the
refereshed". Initial import was the original file; subsequent unchanged import
was integrated `fr004-48fb0b3/02-unchanged.ics` most recently linked by the agent.
Thus this was a mixed-set import, not original 01 -> original 02 or integrated
01 -> integrated 02. The agent's earlier file-set assumption and link switch
contributed to this test-procedure mismatch. It is not evidence of a same-run
exporter/client bug; do not publish it as one.

Local structured comparison of the preserved files confirms:

- Original 01/02 share the same two UIDs.
- Integrated 01/02 share the same two UIDs.
- Original vs integrated UID intersection is **empty** (zero shared UIDs), despite
  identical course titles/times. Cross-set import would introduce different event
  identities and is not a valid unchanged re-import test.

Leave calendar `156` and this evidence intact. A controlled retry must use a
separate initially empty disposable
calendar and all files from one identified run, without manually deleting or
editing events between imports. If same-run imports duplicate or fail to cancel,
prepare a bug report under #74 and request permission before publishing it.

### G7: controlled integrated-file unchanged re-import

Received 2026-11-17 in response to the instruction to use a new empty test calendar
and import integrated 01 followed by integrated 02 without deletion. The user
explicitly identifies
`docs/tech-book/evidence/issue156/fr004-48fb0b3/02-unchanged.ics` as the file just
imported. Google Calendar web shows `156-clean` selected, old `156` unchecked,
and the following August 2026 view:

- One lab at 1pm August 5/12/19/26 and September 2, not duplicate entries.
- One lecture at 8am August 10/24/31, not duplicate entries.
- No class in the displayed pre-term cells; no lecture August 17, while the
  August 19 lab and August 24 lecture remain.
- Thus unchanged re-import, first-date alignment and holiday behavior pass for
  the integrated run in the displayed weeks, based on the operator-guided retry.
  Underlying stored event counts and import/skipped toast were not supplied.
- Exact end times, current-run December occurrences and dropped-class import are
  not shown. Historical G3-G5 remain separately attributed to the original set.
  Do not infer those remaining integrated-run observations from G7.

Original preserved unchanged outside the repository, local-only with unrelated
sidebar labels, in the same external directory as G1-G6:
`google-calendar-156-clean-20261117-unchanged.png`.
SHA-256: `59F8B546CCF355DCA7BBD908527FB28F57041F5860C4DCEF635CBA755C2DA9B0`.
Automated tests skipped for this screenshot/document-only update.

G8 below supplies the current-run December pre-drop view. Next import integrated
03 into that SAME
calendar without deleting/editing any events. Capture August after-drop behavior
and import result: lecture removed or visibly cancelled, lab retained once,
no duplicates. A stale active lecture or duplicate is a real candidate failure
when this same-run sequence is followed; do not clear the calendar to hide it.

### G8: integrated-run December before-drop view

Received 2026-11-17, with the operator explicitly stating "here before i import
03". Google Calendar web shows December 2026 with `156-clean` selected and old
`156` unchecked. One lecture at 8am appears November 30 and December 7/14;
one lab at 1pm appears December 2/9/16. No classes appear December 21/23/28/30
or in the displayed January cells. The expected current-run last-occurrence
dates and lack of post-term continuation in displayed weeks therefore pass.
Green holiday-calendar entries are unrelated. This is not after-drop evidence.

Original retained unchanged, local-only outside the repository in the same
external directory as G1-G7:
`google-calendar-156-clean-20261117-december-before-drop.png`.
SHA-256: `0A2E321978B2D33CBD9951AD741214BE54316F682E0FA21D9D6858DCFBE86077`.
No code/fixture changes; automated tests skipped for this evidence-only update.

### G9: integrated-run after-drop import

Received 2026-11-17 with the operator explicitly stating "here after 03 after
drop", following the instruction to import current
`fr004-48fb0b3/03-after-drop.ics` into the same `156-clean` calendar without
deleting/editing events. Google Calendar web, August 2026 month view, shows
`156-clean` selected and old `156` unchecked:

- Lecture entries previously visible August 10/24/31 in G7 are now absent.
- One lab remains at 1pm August 5/12/19/26 and September 2, without duplicates.
- Thus dropped-class removal/update without duplicates passes in the displayed
  weeks for the controlled integrated 01 -> 02 -> 03 import sequence.
- Exact import message, full-term stored counts and post-drop December view are
  not shown. Do not infer an Outlook result or all client versions from this view.

Original saved unchanged, local-only outside the repository in the same external
directory as G1-G8:
`google-calendar-156-clean-20261117-after-drop.png`.
SHA-256: `0BC2E456D37810A8C718EF4045E6D5FBE73842C53A00AE7F55D600BB0F1C7C26`.
Automated tests skipped for this screenshot/document-only update. No client
interop bug established in this controlled Google sequence; G6 remains a
separate mixed-set test-procedure incident, not a bug to publish.

| Required observation | Google Calendar | Outlook |
| --- | --- | --- |
| Actual application/build, OS, browser/version, timestamp | PARTIAL: Google web visible; operator reports Brave; numeric version/OS/timestamp PENDING | PENDING |
| Initial import accepted; two weekly series at Manila times | PARTIAL current run: 8am/1pm weekly starts visible in G7, Asia/Manila operator-confirmed; exact ends verified only in baseline G3/G4 | PENDING |
| First occurrences August 5/10; nothing before August 4 | PASS current run G7 (also original G2-G4) | PENDING |
| Last occurrences December 14/16; nothing after December 18 | PASS current run G8 (also original G5) | PENDING |
| August 17 lecture excluded; August 19 lab and August 24 lecture present | PASS current run G7 (also original G2) | PENDING |
| Unchanged re-import has no duplicates | PASS current-run visible occurrences G7 after controlled retry; G6 excluded as mixed-set | PENDING |
| Drop re-import removes/cancels lecture; one unchanged lab, no duplicates | PASS current-run visible occurrences G9 | PENDING |
| Actual screenshots and import messages referenced | PARTIAL: G1-G9 preserved locally; import message not supplied | PENDING |

PASS is qualified by the run and screenshot in each cell. G6 is excluded from
same-run judgments because it mixed identities; G7 is the controlled current-run
observation; G8 verifies term end and G9 verifies the drop result. Timezone is
operator-reported. Current-run exact end-time screenshots and environment
metadata gaps remain disclosed; no overall issue or Outlook pass is claimed.

## Differences / draft bug report

G6 is a confirmed test-procedure mismatch (different UID sets), not a verified
same-run interoperability failure. For each confirmed client failure, prepare a
local report with parent #74, reference #156 and the affected AC, client context,
artifact hashes, exact import sequence, expected/actual result, import messages
and redacted screenshots. Ask for authorization before publishing a GitHub issue
or comment. Client file-import limitations are not permission for a broad exporter
redesign or replacement of file imports with subscriptions/sync.

## Remaining acceptance gaps

- Screenshot packaging: originals are operator-held local artifacts, not included
  in the Git change. Attach redacted screenshots identified above before final
  evidence review. Local filenames/hashes alone do not let a reviewer inspect
  the actual images; do not describe this commit as a portable screenshot bundle.
- Outlook deferred at the user's request on 2026-11-17. Every Outlook matrix
  cell remains unverified; no real Outlook import or screenshot is claimed.
  Resume its manual checklist only when the user chooses to continue.
- Current-run Google Calendar observations, version context and screenshots.
  G1-G5 cover original-file initial imports (operator-corrected attribution);
  G6 shows duplicates from a confirmed mixed-set sequence. G7 now verifies
  current-run unchanged re-import without visible duplicates, first dates and
  holiday behavior in `156-clean`; G8 verifies current December before-drop
  occurrences; G9 verifies same-run drop removal with one retained lab and no
  visible duplicates. Current-run exact end-time details remain unshown outside
  baseline G3/G4; do not conflate those runs or change the dropped calendar to
  manufacture missing evidence.
  Asia/Manila and Brave operator-confirmed; OS/import context and numeric browser
  version unreported. Current integrated Google verification is partial, not absent.
  All Outlook evidence outstanding. #147's local integration gate is satisfied.
- Evidence-backed bug publication if differences are found, only with permission.
- Automated checks are local, not CI or deployed/end-to-end acceptance.

Do not mark issue #156 or its goal complete while either client's required
observations remain unverified.
