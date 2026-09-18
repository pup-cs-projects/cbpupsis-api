# Writing issues

| | |
|---|---|
| **Last updated** | 2026-09-15 |
| **Applies to** | `cbpupsis-api` and `cbpupsis-client` |
| **Related** | [BACKLOG.md](../project-specs/BACKLOG.md) · [US-ACS.md](../project-specs/US-ACS.md) · [CONTRIBUTING.md](../../CONTRIBUTING.md) |

How the CBPUPSIS team writes and files work on GitHub: epics, user stories with Given, When,
Then acceptance criteria, tasks, bugs, and decisions; the labels; sub-issue links; and the two
project boards. Both repositories follow this page.

## Where work lives

| Repository | Board | Holds |
|---|---|---|
| `pup-cs-projects/cbpupsis-api` | org project 2, "CBPUPSIS API" | endpoints, business rules, database, jobs |
| `pup-cs-projects/cbpupsis-client` | org project 3, "CBPUPSIS Client" | screens, forms, client state, accessibility |

Both repositories use the **same epic numbers**. `EP-05` in the API repo is the backend half of
enrollment; `EP-05` in the client repo is the UI half. [BACKLOG.md](../project-specs/BACKLOG.md)
maps every epic to its SRS requirements and to both issues.

## Hierarchy

```
Epic (type: epic)
  Story (type: story)        a capability a user can observe
  Task (type: task)          technical work with no user-facing behavior
  Decision (type: decision)  a question that blocks work
Bug (bug)                    linked to the story it breaks
```

Stories, tasks, and decisions are attached to their epic as **sub-issues**, never only by a
mention in the body. The board's "Sub-issues progress" field depends on the link.

## Titles

| Kind | Format | Example |
|---|---|---|
| Epic | `EP-NN: <module>` | `EP-01: Authentication and access control` |
| Story | `US-N: <capability in the user's words>` | `US-104: Lock an account after repeated failed logins` |
| Developer setup story or task | `DEV-NN: <capability>` | `DEV-02: Run the API locally on a fresh clone` |
| Task | imperative, specific | `Verify Xendit webhook signatures` |
| Decision | `Decide: <question>` | `Decide: API compute on Lambda or ECS` |
| Bug | what is wrong, as observed | `GPA includes NSTP units` |

**Story ids are ranged by epic:** the epic number times 100 plus a sequence. The fourth story of
EP-01 is `US-104`, and its criteria are `AC-104.1`, `AC-104.2`. Parallel filers cannot collide,
and the id alone names the module. A client story and its API story share one id.

[US-ACS.md](../project-specs/US-ACS.md) in the API repo is the single registry of story ids.
Add the story there in the same change that files the issue, or right after. **Ids are
permanent**: a dropped story is marked `Withdrawn`, and its number is never reused, because tests
and commits reference it.

## Writing rules

1. **No em dashes.** Not in titles, bodies, comments, or PR descriptions. Use a colon, a comma,
   or parentheses. In Git Bash, check a body file with
   `LC_ALL=C.UTF-8 grep -nP "\x{2014}" body.md`; it must print nothing.
2. **Plain words, short sentences.** A student developer who joined this week should understand
   the issue without asking anyone.
3. **The problem and how we will know it is done, not the solution.** Stories never name
   endpoints, tables, or components. Tasks may name files when that removes ambiguity.
4. **Every acceptance criterion can fail.** "The page works" cannot fail. "A student sees only
   their own grades" can.
5. **Cite the SRS.** Functional requirement ids (`FR7`), use-case ids (`UC-STU-002`), or the
   section (`SRS 4.2`).
6. **One issue is one pull request of at most about three days.** Larger than that, split it.

## Story body

The **User story** issue form produces this shape.

```markdown
## Epic
#<epic number>

## Story
**As a** <student | faculty member | administrator | superadministrator>
**I want to** <do something>
**So that** <outcome that matters to them>

## SRS reference
FR7, UC-STU-002

## Acceptance criteria
| ID | Given | When | Then |
|---|---|---|---|
| AC-501.1 | <starting state> | <one action> | <observable result> |
| AC-501.2 | | | |

## Out of scope
- <what a reader might assume is included but is not>

## Definition of done
- [ ] Each acceptance criterion has a test that names its AC id
- [ ] New error responses are listed in docs/tech-book/ERROR-CODES.md
- [ ] Merged into dev with CI green
```

Cover four kinds of criteria. Most gaps are in the last three:

1. **Happy path.**
2. **Rules:** validation, limits (21 units, 5 failed logins), required fields, periods.
3. **Authorization:** who may not do this, and what they see instead. Another user's record
   answers 404, not 403.
4. **Edges:** empty, one, many; the first and last valid value; doing it twice.

## Task body

```markdown
## Epic
#<epic number>

## Goal
<one or two sentences: what exists when this is done>

## Context
<why it is needed now, and anything a newcomer would not know>

## Acceptance criteria
- [ ] <verifiable outcome>
- [ ] <verifiable outcome>

## References
<SRS section, docs, links>
```

## Epic body

```markdown
## Summary
<two or three sentences>

## SRS scope
<FR ids and sections this epic delivers>

## In scope
- <capability>

## Out of scope
- <capability, and which epic owns it if any>

## Depends on
- <epic or decision>

## Stories
Added as sub-issues when the module is refined.

## Counterpart
<the same epic in the other repository>

## Done when
- [ ] Every story and task in this epic is closed
- [ ] <module-level outcome>
```

## Decision body

```markdown
## Question
<one sentence>

## Context
<what the SRS says, what the code does today, and why they conflict>

## Options
| Option | For | Against |
|---|---|---|

## Recommendation
<one option and the reason>

## Needed by
<the epic or date this blocks>

## Outcome
Filled in when decided, then the issue is closed.
```

## Labels

Read the live set with `gh label list` before filing. Never invent a label.

- Exactly one `type:` label: `epic`, `story`, `task`, `decision`. Bugs use `bug`.
- One `area:` label naming the module (`area: auth`, `area: enrollment`, and so on).
- One `priority:` label. `p0` blocks other work, `p1` is needed for the current milestone, `p2` is
  normal, `p3` is nice to have.
- `status: blocked` while waiting, with a comment saying on what.

## Filing from the command line

The issue forms on GitHub are the easiest way. From a terminal, write the body to a file, check
it for em dashes, then:

```bash
gh issue create -R pup-cs-projects/cbpupsis-api \
  --title "US-104: Lock an account after repeated failed logins" \
  --body-file body.md \
  --label "type: story" --label "area: auth" --label "priority: p1"
```

Attach it to its epic as a sub-issue:

```bash
EPIC=$(gh issue view <epic> -R pup-cs-projects/cbpupsis-api --json id -q .id)
CHILD=$(gh issue view <child> -R pup-cs-projects/cbpupsis-api --json id -q .id)
gh api graphql \
  -f query='mutation($p:ID!,$c:ID!){addSubIssue(input:{issueId:$p,subIssueId:$c}){issue{number}}}' \
  -f p="$EPIC" -f c="$CHILD"
```

Add it to its board (`2` is the API board, `3` the client board):

```bash
gh project item-add 2 --owner pup-cs-projects --url <issue-url>
```

Then set Status, Priority, and Size on the board. From a terminal, read the field and option
ids with `gh project field-list 2 --owner pup-cs-projects --format json` and set them with
`gh project item-edit`.

## The boards

| Board | Number | Repository |
|---|---|---|
| CBPUPSIS API | 2 | cbpupsis-api |
| CBPUPSIS Client | 3 | cbpupsis-client |

Both boards use one Kanban flow:

| Status | Means | Moved by |
|---|---|---|
| Backlog | Filed, not yet refined or not yet scheduled | whoever files it |
| Ready | Acceptance criteria complete and dependencies closed | product owner |
| In Progress | A developer is assigned and has a branch | the developer |
| In Review | A pull request is open and linked with `Closes #N` | the developer |
| Done | The pull request is merged, or the issue is closed with a reason | automatic |

Pick work only from **Ready**, and keep at most two issues **In Progress** per person.

| Field | Values |
|---|---|
| Priority | P0 blocks other work, P1 current milestone, P2 normal, P3 nice to have (matches the `priority:` label) |
| Size | XS under half a day, S one day, M two to three days, L and XL must be split before Ready |
| Sprint | two-week iterations |

## Contract changes cross repositories

When an API change alters what the client receives (a status code, a response field, an error
`code`), file a `type: task` in the client repo under the same epic number, titled with the story
id, linking the API issue or PR. A contract change the client team learns about from a broken
screen is a process failure, not a frontend bug.

## Closing issues

Issues close through a merged PR (`Closes #N` in its description) or with a comment saying why
they are closed. Ask the product owner before closing, transferring, or deleting an issue someone
else filed.
