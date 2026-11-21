# Writing issues

| | |
|---|---|
| **Last updated** | 2026-10-24 |
| **Applies to** | `cbpupsis-api` and `cbpupsis-client` |
| **Related** | [BACKLOG.md](../project-specs/BACKLOG.md) · [US-ACS.md](../project-specs/US-ACS.md) · [CONTRIBUTING.md](../../CONTRIBUTING.md) |

How the CBPUPSIS team writes and files work on GitHub: functional requirements, user stories
with Given, When, Then acceptance criteria, the `[API]` and `[WEB]` tasks that deliver them,
decisions, and bugs; the labels; sub-issue links; and the one project board. Both repositories
follow this page.

## Where work lives

All work in both repositories is tracked on **one board**, the org project
[**CBPUPSIS** (project 4)](https://github.com/orgs/pup-cs-projects/projects/4). The old
per-repository boards (projects 2 and 3) were retired on 2026-10-24.

| Repository | Holds |
|---|---|
| `pup-cs-projects/cbpupsis-api` | FR parents, stories, `[API]` tasks, decisions: endpoints, business rules, database, jobs |
| `pup-cs-projects/cbpupsis-client` | `[WEB]` tasks: screens, forms, client state, accessibility |

A story lives once, in the API repository. Its backend half is an `[API]` task in the API
repository and its frontend half is a `[WEB]` task in the client repository, both under the
same story id.

## Hierarchy

```
EPIC-001: CBPUPSIS Platform (type: epic)            the one epic
  FR-00N: <module> (type: fr)                        one per functional module, one sprint each
    US-<MODULE>-<NNN>: <capability> (type: story)    what a user can observe
      [API] US-<MODULE>-<NNN>: ... (type: task)      backend half, cbpupsis-api
      [WEB] US-<MODULE>-<NNN>: ... (type: task)      frontend half, cbpupsis-client
    <imperative task> (type: task)                   technical work with no story of its own
    Decide: <question> (type: decision)              a question that blocks work
Bug (bug)                                            linked to the story it breaks
```

Every child is attached to its parent as a **sub-issue**, never only by a mention in the body.
The board's "Parent issue" and "Sub-issues progress" fields depend on the link.

## Titles

| Kind | Format | Example |
|---|---|---|
| Functional requirement | `FR-00N: <module>` | `FR-002: Student Module` |
| Story | `US-<MODULE>-<NNN>: <capability>` | `US-STU-003: View Course Schedule` |
| Backend half of a story | `[API] US-<MODULE>-<NNN>: <capability or fix>` | `[API] US-STU-004: Add tests for every grades and transcript criterion` |
| Frontend half of a story | `[WEB] US-<MODULE>-<NNN>: <capability or fix>` | `[WEB] US-STU-002: Manage emergency contacts` |
| Other task | imperative, specific | `Publish the session idle timeout and add a keep-alive` |
| Decision | `Decide: <question>` | `Decide: where profile avatars are stored` |
| Bug | what is wrong, as observed | `GPA includes NSTP units` |

`<MODULE>` is one of the module codes in the labels below (`AUTH`, `STU`, `ENRL`, `FAC`, `ACAD`,
`FIN`, `FORM`, `ADM`, `RPT`). `ENRL` stories carry the `module: ACAD` label.
Story numbers run per module from 001 and are **permanent**: a dropped story is closed as not
planned, and its number is never reused, because tests and commits reference it.

### Acceptance criterion ids

Criterion ids come from the story id: `AC-<MODULE>-<NNN>.<n>`. The criteria of `US-STU-001`
are `AC-STU-001.1`, `AC-STU-001.2`, and so on; the criteria of `US-AUTH-002` are
`AC-AUTH-002.1` onward. A test names the id it covers, for example
`test_ac_stu_004_3_gwa_excludes_nstp`.

Do not use the older numeric ids (`AC-101.3`): they collided between modules. The old registry in
[US-ACS.md](../project-specs/US-ACS.md) maps them to the new ones.

## Writing rules

1. **No em dashes.** Not in titles, bodies, comments, or PR descriptions. Use a colon, a comma,
   or parentheses. In Git Bash, check a body file with
   `LC_ALL=C.UTF-8 grep -nP "\x{2014}" body.md`; it must print nothing.
2. **Plain words, short sentences.** A student developer who joined this week should understand
   the issue without asking anyone.
3. **The problem and how we will know it is done, not the solution.** Stories describe what a user
   observes. Tasks may name files, endpoints, and tables when that removes ambiguity.
4. **Every acceptance criterion can fail.** "The page works" cannot fail. "A student sees only
   their own grades" can.
5. **Cite the SRS, and only what exists.** Functional requirement ids (`FR7`), use-case ids
   (`UC-STU-002`), or sections (`SRS 4.2`). Never cite a document or a rule id that is not in the
   repository.
6. **Sample JSON is an illustration, never a default.** Label every request or response sample
   "Example response. Values are illustrative; the server never substitutes them as defaults."
   Write the empty and failure behavior as rules beside it: what the response holds when there
   is no data, and what happens when a read fails. The server never fills a missing value from a
   sample.
7. **Every `[API]` task names its permissions.** List each endpoint with the permission it
   requires, using the fixed vocabulary in [CONVENTIONS.md](CONVENTIONS.md)
   (`Create/Read/Update/Delete<X>`, `ReadAll/Moderate/Manage<X>`, business verbs for real
   transitions) and the names already in `scripts/seed_iam.py`. Mark new names as new. A public
   endpoint says so ("none: public sign-in, rate limited").
8. **Student endpoints are `/api/v1/student/...`.** Singular, no `/me`, and no student number in
   the path: the student is always the caller.
9. **One issue is one pull request of at most about three days.** Larger than that, split it.

## Story body

Stories carry their test cases, so a reviewer can check a pull request against them.

```markdown
## FR
FR-00N: <module> (#<FR issue>)

## 1. User Story
As a <student | faculty member | administrator | superadministrator>, I want to <do something>,
so that <outcome that matters to them>.

## 2. Acceptance Criteria
| ID | Given | When | Then |
| :-- | :-- | :-- | :-- |
| AC-STU-001.1 | <starting state> | <one action> | <observable result> |

## 3. FR Test Cases
| AC ID | Test Step | Description | Expected Result |
| :-- | :-- | :-- | :-- |
| AC-STU-001.1 | <request or action> | <why it matters> | <status code, error code, data> |

## 4. NON-FR Test Cases
| Category | Test Step | Description | Expected Result |
| :-- | :-- | :-- | :-- |

## 5. Business Rule Justification
| What it requires | Source |
| :-- | :-- |
| <rule in plain words> | <SRS id, Handbook section, or law> |

## 6. Constraint
| Type | Constraint binding this story |
| :-- | :-- |
```

Cover four kinds of criteria. Most gaps are in the last three:

1. **Happy path.**
2. **Rules:** validation, limits (21 units, 5 failed logins), required fields, periods.
3. **Authorization:** who may not do this, and what they see instead. Another user's record
   answers 404, not 403.
4. **Edges:** empty, one, many; the first and last valid value; doing it twice; a failed read.

## `[API]` and `[WEB]` task body

```markdown
## Parent story
#<story number>

## Side
Backend | Frontend

## Scope
- <what this half builds>

## Acceptance criteria this delivers
AC-STU-001.1, AC-STU-001.2

## Endpoints owned by this story            ([API] only)
| Method | Endpoint | Purpose |

## Permissions                              ([API] only)
| Endpoint or action | Permission |

## Definition of done
- [ ] Every acceptance criterion named above has a test that cites its AC id
- [ ] New error responses are listed in docs/tech-book/ERROR-CODES.md
- [ ] The OpenAPI schema is updated and published for the client team
- [ ] Merged into dev with CI green

## Counterpart
The other half of this story lives in the other repository under the same story id.
```

## Follow-up task body

For fixes found in review, and for tasks with no story of their own.

```markdown
## Story
#<story or FR number>

## Goal
<one or two sentences: what exists when this is done>

## Context
<why it is needed now, and anything a newcomer would not know>

## Acceptance criteria
- [ ] <verifiable outcome>

## References
<SRS id, issues and PRs, file paths>
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
<the story, sprint, or date this blocks>

## Outcome
Filled in when decided, then the issue is closed.
```

Link a decision to the work it blocks with GitHub's "blocked by" relationship, and give the
blocked issue the `status: blocked` label when all of it waits on the decision.

## Labels

Read the live set with `gh label list -L 200` before filing. Never invent a label.

| Group | Labels | Rule |
|---|---|---|
| Type | `type: epic`, `type: fr`, `type: story`, `type: task`, `type: decision`, `bug` | exactly one |
| Module | `module: AUTH`, `module: STU`, `module: FAC`, `module: ACAD`, `module: FIN`, `module: FORM`, `module: ADM`, `module: RPT` | exactly one (the epic has none) |
| MoSCoW | `must-have`, `should-have`, `could-have`, `wont-have` | exactly one |
| Status | `status: blocked` | while waiting, with a comment naming the blocker |

The older `area:`, `priority:`, and `fr: NN` labels belong to the retired boards. Do not add them
to new issues.

## Filing from the command line

The issue forms on GitHub are the easiest way. From a terminal, write the body to a file, check
it for em dashes, then:

```bash
gh issue create -R pup-cs-projects/cbpupsis-api \
  --title "[API] US-STU-001: Remove invented data from the dashboard" \
  --body-file body.md \
  --label "type: task" --label "module: STU" --label "must-have"
```

Attach it to its parent as a sub-issue (this works across the two repositories):

```bash
PARENT=$(gh issue view <parent> -R pup-cs-projects/cbpupsis-api --json id -q .id)
CHILD=$(gh issue view <child> -R pup-cs-projects/cbpupsis-api --json id -q .id)
gh api graphql \
  -f query='mutation($p:ID!,$c:ID!){addSubIssue(input:{issueId:$p,subIssueId:$c}){issue{number}}}' \
  -f p="$PARENT" -f c="$CHILD"
```

Add it to the board:

```bash
gh project item-add 4 --owner pup-cs-projects --url <issue-url>
```

Then set its fields. Read the field and option ids with
`gh project field-list 4 --owner pup-cs-projects --format json`, and set them with
`gh project item-edit` (`--single-select-option-id` for Status, Sprint, and MoSCoW; `--text` for
FR and Module).

## The board

[CBPUPSIS, project 4](https://github.com/orgs/pup-cs-projects/projects/4), holds every issue of
both repositories.

| Field | Values |
|---|---|
| Status | Backlog, Ready, In Progress, In Review, Done |
| Sprint | one per FR: `S1 FR-001 Auth`, `S2 FR-002 Student`, `S3 FR-003 Enrollment`, `S4 FR-004 Faculty`, `S5 FR-005 Academic`, `S6 FR-006 Financial`, `S7 FR-007 Forms`, `S8 FR-008 Operations` |
| FR | the parent requirement, as text: `FR-002` |
| MoSCoW | Must Have, Should Have, Could Have, Won't Have (matches the label) |
| Module | the module code, as text: `STU` |
| Start date, Target date | the planned window for the issue |

Status follows one Kanban flow:

| Status | Means | Moved by |
|---|---|---|
| Backlog | Filed, not yet refined or not yet scheduled | whoever files it |
| Ready | Acceptance criteria complete and dependencies closed | product owner |
| In Progress | A developer is assigned and has a branch | the developer |
| In Review | A pull request is open and linked with `Closes #N`, or the work is merged into an integration branch but not yet into `dev` | the developer |
| Done | The pull request is merged into `dev`, or the issue is closed with a reason | automatic |

Pick work only from **Ready**, and keep at most two issues **In Progress** per person.

## Contract changes cross repositories

When an API change alters what the client receives (a path, a status code, a response field, an
error `code`), file a `[WEB]` task in the client repo under the story's `[WEB]` task, linking the
API issue or PR. A contract change the client team learns about from a broken screen is a process
failure, not a frontend bug.

## Closing issues

Issues close through a merged PR (`Closes #N` in its description) or with a comment saying why
they are closed. Ask the product owner before closing, transferring, or deleting an issue someone
else filed.
