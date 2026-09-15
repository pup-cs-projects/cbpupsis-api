# Backlog map

| | |
|---|---|
| **Last updated** | 2026-09-15 |
| **Boards** | [CBPUPSIS API](https://github.com/orgs/pup-cs-projects/projects/2) · [CBPUPSIS Client](https://github.com/orgs/pup-cs-projects/projects/3) |
| **Related** | [SRS.md](SRS.md) · [PRD.md](PRD.md) · [US-ACS.md](US-ACS.md) |

How the SRS becomes work. Every epic exists **twice**, once per repository, with the same
number: the API epic holds endpoints, rules, and data; the client epic holds screens and
interaction. Stories and tasks are sub-issues of their epic. How to write them is in
[WRITING-ISSUES.md](../tech-book/WRITING-ISSUES.md).

## Epics

| Epic | Module | SRS scope | Depends on | Story ids | API issue | Client issue |
|---|---|---|---|---|---|---|
| EP-00 | Developer setup and team workflow | SRS 2.4, 4.5 | | DEV-01 and up | | |
| EP-01 | Authentication and access control | FR1, FR2, UC-STU-001, UC-FAC-001, UC-ADM-001 | EP-00, decisions Q3 to Q5, Q7 | US-101 to US-199 | | |
| EP-02 | Student profile | FR4 | EP-01 | US-201 to US-299 | | |
| EP-03 | Course catalog, curriculum, and sections | FR12 | EP-01 | US-301 to US-399 | | |
| EP-04 | Academic calendar | FR11, FR20, UC-ADM-003 | EP-01 | US-401 to US-499 | | |
| EP-05 | Enrollment, registration, and add or drop | FR7, FR17, UC-STU-002, UC-STU-003 | EP-03, EP-04, EP-09 | US-501 to US-599 | | |
| EP-06 | Course schedule | FR5 | EP-05 | US-601 to US-699 | | |
| EP-07 | Grades, rosters, and grade submission | FR6, FR9, FR10, UC-FAC-002 | EP-04, EP-05 | US-701 to US-799 | | |
| EP-08 | Academic standing and petitions | FR13, FR18 | EP-07, decision Q6 | US-801 to US-899 | | |
| EP-09 | Fees, assessments, and student accounts | FR8, FR14, UC-ADM-004 | EP-01, EP-03 | US-901 to US-999 | | |
| EP-10 | Payments through Xendit | FR15 | EP-09 | US-1001 to US-1099 | | |
| EP-11 | User administration and system configuration | FR19, FR20, UC-ADM-002 | EP-01 | US-1101 to US-1199 | | |
| EP-12 | Data management and system monitoring | FR21, FR22 | EP-16, decision Q9 | US-1201 to US-1299 | | |
| EP-13 | Reports: enrollment, academic, financial | FR16, FR23, FR24, UC-ADM-005 | EP-05, EP-07, EP-10 | US-1301 to US-1399 | | |
| EP-14 | Notifications and email | FR7, FR8, FR10, FR20, SRS 5.3, 5.4 | EP-01, decision Q8 | US-1401 to US-1499 | | |
| EP-15 | Security, privacy, and compliance | SRS 4.2, 4.6 | EP-01 | US-1501 to US-1599 | | |
| EP-16 | Infrastructure, CI, and deployment | SRS 2.4, 2.5, 4.1, 4.4 | EP-00, decisions Q1, Q2 | US-1601 to US-1699 | | |

**Story id ranges.** A story's id is its epic number times 100 plus a sequence: the first
enrollment story is `US-501`, and its criteria are `AC-501.1`, `AC-501.2`. Five developers
filing stories in parallel then cannot collide on an id, and an id alone says which module it
belongs to. Register each id in [US-ACS.md](US-ACS.md) when the story is filed.

## Suggested build order

Waves of epics whose dependencies are satisfied. Within a wave, the API half of an epic starts
first; the client half starts as soon as the endpoint contract is agreed, not when it is merged.

| Wave | Epics | Why this order |
|---|---|---|
| 0 | EP-00, the CI part of EP-16, and the open decisions | Nobody can build features on an unmerged scaffold or an undecided login model |
| 1 | EP-01, EP-11, EP-15 baseline | Every other module needs users, roles, and the audit trail |
| 2 | EP-03, EP-04, EP-02, EP-14 | Catalog and calendar are the data enrollment validates against |
| 3 | EP-09, EP-05, EP-06 | Enrollment checks account holds, so fees land first |
| 4 | EP-07, EP-10, EP-08 | Grades need enrolled students; payments need assessments |
| 5 | EP-13, EP-12 | Reports and operations read data the earlier modules produce |

## Decisions

Open questions from [PRD.md](PRD.md#9-open-questions) that block an epic are filed as
`type: decision` issues under EP-00 in the API repository.

| PRD | Decision | Blocks | Issue |
|---|---|---|---|
| Q1, Q2 | API compute and database hosting | EP-16, EP-12 | |
| Q3, Q4 | Authentication provider and login identifier | EP-01 | |
| Q5 | Session model: cookies or bearer tokens | EP-01 (API and client) | |
| Q6, Q7 | Advisor, registrar, and department-scoped admin roles | EP-01, EP-05, EP-08 | |
| Q8 | Email provider | EP-14 | |
| Q9 to Q15 | SRS clarifications for stakeholders | EP-07, EP-08, EP-09, EP-12, client shell | |
