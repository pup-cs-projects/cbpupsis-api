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
| | | | |

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
