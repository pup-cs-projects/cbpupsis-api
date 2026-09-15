# Design: <Product or Feature Name>

<!--
DESIGN.md answers HOW. The PRD answers what and why; do not restate it here
beyond a one-line reference.

Write this before implementing anything non-trivial. Its value is that a
reviewer can reject an approach while it is still a paragraph rather than a
branch.

Delete every instruction comment as you fill this in.
-->

| | |
|---|---|
| **Status** | Draft · In review · Approved · Implemented |
| **Author** | <name> |
| **Last updated** | <YYYY-MM-DD> |
| **Related** | [PRD.md](PRD.md) · [US-ACS.md](US-ACS.md) |

## 1. Summary

<!-- The approach in three or four sentences. A reader should be able to stop here. -->

## 2. Domains touched

<!--
Which domains this adds or changes, and whether anything crosses a boundary.

A cross-domain call goes through the other domain's `client.py` and carries DTOs,
never ORM objects. If this design needs a domain to reach into another's tables,
that is the thing to resolve now — it is the coupling a service split cannot
sever.
-->

| Domain | New or changed | Notes |
|---|---|---|
| | | |

## 3. Data model

<!--
Tables and columns. For each: the type, whether it is nullable, and what it is
indexed for.

Decisions that are hard to reverse once there is data, so state them explicitly:
UUID primary keys; `Numeric`/`Decimal` for money, never float; timezone-aware
timestamps; soft delete via `deleted_at` where the row must survive; a bare id
with NO foreign key for anything referencing another domain.
-->

```
<table>
  id            UUID   PK
  <column>      <type> <null?>  -- <why, if not obvious>
```

**Migration:** <what the migration does; note anything needing care — a rename,
a backfill, a non-nullable column added to a populated table>

## 4. API surface

<!--
Endpoints, with the permission each requires. Say explicitly which endpoints take
a caller-supplied id, because those need an object-level ownership check in the
service on top of the permission — a permission alone is an IDOR bug.
-->

| Method | Path | Permission | Object-level check | Notes |
|---|---|---|---|---|
| | | | own / none / n/a | |

**Schemas:** <the Create/Read/Update shapes, and anything asymmetric — a field
that is input-only or output-only>

## 5. Authorization

<!--
The permissions this adds, in the fixed vocabulary:
  Create/Read/Update/Delete<X>  — own records, paired with an ownership check
  ReadAll/Moderate/Manage<X>    — crosses ownership
Real state transitions keep their business verb (ApproveRefund).

Derive these from the PRD's access table (PRD.md section 8),
then add them to `scripts/seed_iam.py`.
-->

| Permission | Policy | Groups |
|---|---|---|
| | | |

## 6. Business rules

<!--
Rules that live in the service layer, each traced to the criterion that requires
it. This is where a reviewer checks that the design actually satisfies US-ACS.md.
-->

| Rule | Enforced in | Satisfies |
|---|---|---|
| | `<domain>/service.py` | AC-n.m |

## 7. Failure modes

<!--
What goes wrong and what the user sees. Every error here should have a code in
`docs/tech-book/ERROR-CODES.md`.

Include the concurrency cases: what happens when two callers do this at once. A
"only one X" rule needs a unique constraint plus a caught integrity error, not a
check-then-insert.
-->

| Scenario | Response | Error code |
|---|---|---|
| | | |

## 8. Alternatives considered

<!--
The approaches you rejected and why. This is the section people skip and later
wish they had written — it is what stops the same debate reopening in six months.
-->

| Alternative | Why not |
|---|---|
| | |

## 9. Risks and trade-offs

<!--
What this design makes harder, what could go wrong, and what you are deliberately
deferring. Honest trade-offs here are worth more than a design that claims none.
-->

-

## 10. Test approach

<!--
How US-ACS.md gets covered, and at which level. Test each behavior at the lowest
level that can actually catch its failure: a business rule at the service layer,
a contract at the API layer.

Note anything needing a real database rather than the in-memory SQLite the suite
uses by default.
-->

-
