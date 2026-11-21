# User Stories and Acceptance Criteria: CBPUPSIS

<!--
Stories describe BEHAVIOR the user can observe. They do not name endpoints,
tables, or components; those belong in DESIGN.md.

This file is the registry of story ids for both repositories and the source of
truth for tests. Tests are written from its criteria, and `docs/tech-book/TEST-NOTES.md` links each criterion back to the test
that covers it. Keep the id scheme and the Given/When/Then form.

Stories are added module by module as each epic is refined. The US-n and
worked-example sections below are the template to copy for each new story.
-->

| | |
|---|---|
| **Status** | Draft |
| **Last updated** | 2026-09-15 |
| **Related** | [SRS.md](SRS.md) · [PRD.md](PRD.md) · [BACKLOG.md](BACKLOG.md) · [DESIGN.md](DESIGN.md) · [TEST-NOTES.md](../tech-book/TEST-NOTES.md) |

## How to read this

- **US-n** identifies a story. **AC-n.m** identifies one criterion within it.
- **Ids are ranged by epic:** the epic number times 100 plus a sequence. The first
  story of EP-05 (enrollment) is `US-501`, and its criteria are `AC-501.1`,
  `AC-501.2`. The ranges are listed in [BACKLOG.md](BACKLOG.md).
- A story implemented in both repositories keeps **one** id. The API issue and the
  client issue both carry it.
- Ids are **permanent**. When a story is dropped, mark it `Withdrawn` and keep the
  number. Reusing an id silently invalidates every test and commit that
  referenced it.
- Each criterion is written so it can **fail**. "The page works correctly" cannot
  fail; "the response contains only enrollments owned by the caller" can.

## Registry

| Story | Title | Epic | Status |
|---|---|---|---|
| US-101 | Sign in as a student with my student number and birthdate | EP-01 | In progress |
| US-102 | Sign in as a faculty member with a second factor | EP-01 | In progress |
| US-103 | Sign in as an administrator with a second factor | EP-01 | In progress |
| US-104 | Act as a superadministrator under MFA and dual custody | EP-01 | In progress |
| US-105 | Recover my account with a password reset link | EP-01 | In progress |

These five were implemented before they were registered; their criteria below
are written from the shipped behavior and its tests, and the product owner
should confirm them. Earlier ids in code and commits (`US-AUTH-001` to
`US-AUTH-004`, `AC-001.x` to `AC-005.x`) map to `US-101` to `US-105` and
`AC-101.x` to `AC-105.x` one to one.

---

## US-101: Sign in as a student with my student number and birthdate

**As a** student
**I want to** sign in with my student number, birthdate, and password
**So that** I can reach my records without anyone else being able to

**Priority:** Must
**Status:** In progress

| ID | Given | When | Then |
|---|---|---|---|
| AC-101.1 | An active, unlocked student | They sign in with correct details | They get a student session and an HttpOnly refresh cookie |
| AC-101.2 | Any caller | They submit a student number not in `YYYY-NNNNN-XX-N` form | 422 `AUTH_ID_FORMAT_INVALID`, and no password is checked |
| AC-101.3 | A student | They submit a wrong birthdate, a wrong password, or an unknown number | One byte-identical 401 `AUTH_FAILED` for all three |
| AC-101.4 | A student with 4 failures in 15 minutes | They fail a 5th time | 423 `AUTH_ACCOUNT_LOCKED` with the remaining seconds, and one security notice is sent |
| AC-101.5 | A locked student | They sign in with correct details | 423 with the remaining time, and the lock is not extended |
| AC-101.6 | Any attempt | It completes | One append-only ledger row with the identifier, address, and outcome, never the password |
| AC-101.7 | A signed-in student | Their session is idle for 15 minutes | Every endpoint answers 401 `AUTH_SESSION_EXPIRED` and the session is deleted |
| AC-101.8 | A signed-in student | They request another student's record | 404, never 403 (no student-record endpoint exists yet; see the auth report) |

## US-102: Sign in as a faculty member with a second factor

**As a** faculty member
**I want to** sign in with my employee id, password, and authenticator
**So that** a stolen password alone cannot open my students' records

**Priority:** Must
**Status:** In progress

| ID | Given | When | Then |
|---|---|---|---|
| AC-102.1 | A faculty member | They submit a correct id and password | Only a challenge that no protected route accepts; email and password elsewhere are refused |
| AC-102.2 | A faculty member not yet enrolled | They confirm a code from a new authenticator | A faculty session and ten single-use recovery codes, shown once |
| AC-102.3 | An enrolled faculty member | They submit the current code | A faculty session |
| AC-102.4 | An enrolled faculty member | They resubmit a code already used | 401 `AUTH_MFA_CODE_REUSED` |
| AC-102.5 | Any caller | The 5th password or code failure in 15 minutes | 423 `AUTH_ACCOUNT_LOCKED`, identically for known and unknown ids |
| AC-102.6 | An enrolled faculty member | They try to enroll again with only the password | 403 `AUTH_MFA_ENROLLMENT_REQUIRED`; the authenticator is unchanged |
| AC-102.7 | A signed-in faculty member | They open a section not assigned to them | 404 `RESOURCE_NOT_FOUND`, identical to a missing section |
| AC-102.8 | Anyone | They read the sign-in ledger | Only with `ReadAllAuditEntry`; nobody can edit or delete it |

## US-103: Sign in as an administrator with a second factor

**As an** administrator
**I want to** sign in with a password and an authenticator or hardware key
**So that** administrative access always needs two factors and my position's scope

**Priority:** Must
**Status:** In progress

| ID | Given | When | Then |
|---|---|---|---|
| AC-103.1 | An Admin | They submit a correct password | Only a challenge; the generic email sign-in refuses them |
| AC-103.2 | An Admin not yet enrolled | They confirm a TOTP code or a hardware key | An Admin session scoped to their position |
| AC-103.5 | An Admin | The 5th failed step in 15 minutes | 423 `AUTH_ACCOUNT_LOCKED`, and no session |
| AC-103.6 | An Admin | They replay a used TOTP code | 401 `AUTH_MFA_CODE_REUSED` |

## US-104: Act as a superadministrator under MFA and dual custody

**As a** superadministrator
**I want to** sign in with TOTP and perform audited overrides and restores
**So that** the most powerful actions are always attributable and never one person's alone

**Priority:** Must
**Status:** In progress

| ID | Given | When | Then |
|---|---|---|---|
| AC-104.1 | A Superadmin | They submit a correct password; or fail 5 times | Only a challenge; on the 5th failure 423 |
| AC-104.2 | A Superadmin enrolling | They confirm their first code | The seed is stored encrypted, and each code works once |
| AC-104.3 | A Superadmin | They apply an override | One atomic audit record with the justification |
| AC-104.4 | A Superadmin | They omit the justification | 422 `OVERRIDE_JUSTIFICATION_REQUIRED`, and nothing changes |
| AC-104.5 | An Admin holding IAM grants | They call a Superadmin endpoint | 403 |
| AC-104.6 | A restore request | Its requester approves it | 403 `DUAL_AUTH_SAME_ACTOR`; a second Superadmin can |
| AC-104.7 | A Superadmin session | It is idle for 15 minutes | 401 `AUTH_SESSION_EXPIRED`, and refresh cannot revive it |
| AC-104.8 | A Superadmin request | It reads or changes data | Exactly one audit row |

## US-105: Recover my account with a password reset link

**As a** user of any role
**I want to** reset a forgotten password through a mailed link
**So that** I can get back in without help, and an attacker cannot

**Priority:** Must
**Status:** In progress

| ID | Given | When | Then |
|---|---|---|---|
| AC-105.3 | A reset link | The new password misses a character class | 422 `AUTH_PASSWORD_TOO_WEAK` with the unmet rules |
| AC-105.6 | A valid reset link | The password is reset | 200 with `sessionsRevoked`, and every session, lock, and pending challenge ends |
| AC-105.8 | Any reset request or completion | It happens | One audit row each, without the token |

---

## US-1: <capability, in the user's words>

**As a** <user type>
**I want to** <do something>
**So that** <outcome that matters to them>

<!--
The "so that" is not decoration. If you cannot fill it in, the story may not be
worth building — or you are describing an implementation detail rather than a
capability.
-->

**Priority:** Must · Should · Could
**Status:** Draft · Ready · In progress · Done · Withdrawn

### Acceptance criteria

<!--
One row per testable behavior. Cover four categories — most gaps are in the
last three:

  1. The happy path.
  2. The rules: validation, limits, required fields.
  3. Authorization: who may NOT do this, and what they see instead.
  4. The edges: empty, one, many; the first and last valid value; concurrent use.

Given = the starting state. When = the single action. Then = the observable
result, specific enough to assert on.
-->

| ID | Given | When | Then |
|---|---|---|---|
| AC-1.1 | <starting state> | <the action> | <observable result> |
| AC-1.2 | | | |

### Notes

<!--
Anything a reader needs that does not fit a criterion: a business rule with
history, a deliberate omission, a link to a design. Optional — delete if empty.
-->

---

## US-2: <next capability>

**As a** <user type>
**I want to** <do something>
**So that** <outcome>

**Priority:**
**Status:**

### Acceptance criteria

| ID | Given | When | Then |
|---|---|---|---|
| AC-2.1 | | | |

---

## Worked example

<!--
Keep this section while the file is a template; delete it once real stories fill
the file. It shows the level of specificity the criteria need — particularly the
authorization and edge rows, which are the ones usually missing.
-->

### US-0: Cancel my own booking

**As a** customer
**I want to** cancel a booking I made
**So that** I am not charged for a trip I cannot take

**Priority:** Must
**Status:** Done

| ID | Given | When | Then |
|---|---|---|---|
| AC-0.1 | I have a confirmed booking | I cancel it | Its status becomes `cancelled` and it stops appearing in my upcoming list |
| AC-0.2 | I have a booking starting in under 24 hours | I cancel it | It is refused, and the message says cancellation closes 24 hours before departure |
| AC-0.3 | I have an already-cancelled booking | I cancel it again | Nothing changes and no second refund is issued |
| AC-0.4 | Another customer has a booking | I try to cancel it by its id | I get 404, not 403 — the response must not reveal that the booking exists |
| AC-0.5 | I have no bookings | I open my bookings list | I see an empty state explaining how to make one, not an error |

<!--
Note AC-0.4: "404 not 403" is the kind of detail that only gets tested if the
criterion says it. Note AC-0.3: idempotence. Note AC-0.5: the empty state, which
is a real requirement and the most commonly forgotten one.
-->
