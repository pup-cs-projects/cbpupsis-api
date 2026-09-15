# PRD: CBPUPSIS (Cloud-Based PUP Student Information System)

| | |
|---|---|
| **Status** | Draft |
| **Owner** | Product owner (@JpCurada) |
| **Last updated** | 2026-09-15 |
| **Related** | [SRS.md](SRS.md) · [BACKLOG.md](BACKLOG.md) · [US-ACS.md](US-ACS.md) · [DESIGN.md](DESIGN.md) |

This PRD condenses the [SRS](SRS.md) into what the team builds and why. When the two disagree,
the SRS wins until the disagreement is recorded in [Open questions](#9-open-questions) and
decided.

## 1. Problem

The Polytechnic University of the Philippines has run a Student Information System beta since
2020. It is the central record of academic and financial data for more than 15,000 students
across multiple campuses (SRS 2.1).

Students, faculty, and staff need one system that covers the whole academic cycle: enrolling
in courses, seeing schedules and grades, settling fees, submitting forms and appeals, and
producing institutional reports. It must work from any internet-connected device, most often a
phone, and it must protect personal data as required by the Data Privacy Act (RA 10173) while
applying Free Higher Education benefits (RA 10931) correctly.

CBPUPSIS is the enhancement of that beta, built as a cloud-hosted web application with its own
authentication, payment processing, email notification, and file storage (SRS 2.1).

## 2. Who this is for

| User type | What they need | How many |
|---|---|---|
| Student | Enroll, view schedule, grades, GPA, and balance, pay online, submit forms, mostly on a phone | 15,000+ |
| Faculty | See assigned classes and rosters; draft and submit grades before the deadline | 30 to 80 |
| System administrator | Manage accounts, calendar, catalog, fees, configuration, backups, monitoring, reports | 3 to 5 |
| Superadministrator | Govern the system and apply emergency overrides | 1 to 2 |

The SRS also names roles that are not among these four: an **advisor** and the **registrar**
approve add and drop requests (FR17), and "appropriate faculty" review petitions (FR18). See
open question Q6.

## 3. Goals

1. A student completes enrollment for a term (course selection with prerequisite, unit limit,
   and schedule conflict checks) and downloads a Certificate of Registration without visiting an
   office. (FR7, UC-STU-002, UC-STU-003)
2. A student sees their grades, GPA, academic standing, schedule, and account balance, and pays
   online. (FR5, FR6, FR8, FR13, FR15)
3. A faculty member submits final grades for every assigned section before the deadline, with a
   draft stage first. (FR9, FR10, UC-FAC-002)
4. An administrator configures the academic calendar, catalog, fees, and accounts, and produces
   enrollment, academic, and financial reports. (FR11, FR12, FR14, FR16, FR19 to FR24)
5. The system meets the security, privacy, accessibility, and reliability requirements in SRS 4
   from the first release, not as a later hardening phase.

## 4. Non-goals

- **Native mobile apps.** The system is a responsive web application (SRS 1.2, 4.3). Deferred,
  not rejected.
- **Storing payment card data.** Payments are tokenized through Xendit for PCI DSS (FR15,
  SRS 4.2). Rejected.
- **Faculty performance reports.** Marked "TBD" in FR24. Deferred until defined.
- **Multi-region deployment.** Single region, ap-southeast-1, with multi-AZ (SRS 2.5). Rejected
  for this release.

## 5. Success metrics

Targets come from SRS 4. The current beta has no recorded baseline in the SRS (see Q13).

| Metric | Today | Target | How measured |
|---|---|---|---|
| API response time | Unknown | p90 at most 500ms, p95 at most 1000ms | CloudWatch and X-Ray latency percentiles |
| Concurrent users during enrollment | Unknown | 5,000 without degradation | Load test at 2x expected peak before production |
| Throughput | Unknown | 100 transactions per second | Load test |
| Monthly uptime | Unknown | 99.5% (at most 3.6 hours down) | Uptime monitoring |
| Mean time to repair | Unknown | Under 2 hours critical, under 4 hours major | Incident records |
| Test coverage | n/a | Above 80% | CI coverage report (enforced in cbpupsis-api) |
| Accessibility | Unknown | WCAG 2.1 Level AA | Automated checks plus a manual audit per release |

## 6. Scope

### In scope

The modules below map one to one onto the epics in [BACKLOG.md](BACKLOG.md).

- Developer setup and team workflow
- Authentication and access control (FR1, FR2)
- Student profile (FR4)
- Course catalog, curriculum, and sections (FR12)
- Academic calendar (FR11)
- Enrollment, registration, and add or drop (FR7, FR17)
- Course schedule viewing and export (FR5)
- Grades, rosters, and grade submission (FR6, FR9, FR10)
- Academic standing and petitions (FR13, FR18)
- Fees, assessments, and student accounts (FR8, FR14)
- Payments through Xendit (FR15)
- User administration and system configuration (FR19, FR20)
- Data management and system monitoring (FR21, FR22)
- Reports: enrollment, academic, financial (FR16, FR23, FR24)
- Notifications and email (FR7, FR8, FR10, SRS 5.3)
- Security, privacy, and compliance (SRS 4.2, 4.6)
- Infrastructure, CI, and deployment (SRS 2.4, 4.1, 4.4)

### Assumptions

From SRS 2.6, the ones most likely to be wrong:

- Data from the current PUP-SIS is valid and can be migrated to PostgreSQL without loss.
- Student numbers stay unique.
- The IT department provides access to institutional LDAP or Active Directory.
- Faculty and administrative staff are trained on the new system before launch.
- The AWS budget is approved, and Free Higher Education continues.

### Dependencies

- **AWS** (ap-southeast-1): compute, Aurora PostgreSQL, S3, CloudFront, WAF, SES, Secrets
  Manager, CloudWatch.
- **Xendit** for payments and payment webhooks.
- **PUP IT department** for institutional directory access and infrastructure resources.
- **PUP academic and finance offices** for policy values: GPA scale, standing thresholds, fee
  schedules, penalty rates, installment plans, and calendar dates.

## 7. User flow

The SRS appendix holds the full use cases (UC-STU-001 to UC-ADM-005). The enrollment flow is
the most constrained, so it is spelled out here with its unhappy paths.

**Enroll in courses (UC-STU-002, FR7, FR8, FR12)**

1. The student opens Enrollment during an open enrollment period.
2. The system shows their enrollment status, Free Higher Education qualification, and the
   courses available to them.
3. The student selects courses.
4. The system validates the selection and the student confirms it.
5. The system enrolls the student, shows the confirmation with the course list, emails it, and
   offers the Certificate of Registration.

When it goes wrong:

- **Outside the enrollment period:** enrollment is refused, and the student sees the period dates.
- **Prerequisite not met:** that course cannot be added, and the student sees which prerequisite
  is missing. A waiver is possible (FR12).
- **More than 21 units** (or the reduced limit for a probationary student, FR13): the selection is
  refused with the current total and the limit.
- **Schedule conflict:** the conflicting courses are named.
- **Outstanding hold** on the student's account (FR8): enrollment is refused, and the student is
  told how to clear the hold.
- **Section full** (FR12 capacity): the section cannot be selected.

## 8. Access and permissions

Four primary roles with function-level permissions (read, write, delete, approve), checked on
the server for every request; the UI only hides what a role cannot use (FR2). This table is
the input to the permission model task in EP-01.

| User type | May | May not |
|---|---|---|
| Student | Read their own profile, schedule, grades, standing, account, and payments; edit limited profile fields (name, phone, email, address), emergency contacts, and photo; enroll and request add or drop in open periods; pay; submit petitions and appeals | Read any other student's data; enroll outside a period; change configuration |
| Faculty | Read the rosters and related student data of courses assigned to them; draft, submit, and view the distribution of grades for those courses before the deadline | Read courses not assigned to them; change grades after final submission or after the deadline without an admin override |
| Admin | Read and manage data for the departments assigned to them; manage accounts and roles; configure calendar, catalog, sections, fees, and notification templates; override grade deadlines; generate reports | Act on departments not assigned to them; perform superadmin overrides |
| Superadmin | Everything, including emergency overrides | Nothing is excluded, so every override must be audited |

Every authorization failure is logged and monitored (FR2), and every user management action is
audited (FR19).

## 9. Open questions

The SRS contradicts itself or the scaffold in several places. None of these may be resolved by
guessing in code. Items marked with a decision issue are tracked on the API board.

| # | Question | Where it comes from | Owner | Needed by |
|---|---|---|---|---|
| Q1 | Does the API run on Lambda behind API Gateway, or as containers on ECS Fargate? The outbox worker needs a long-running process that Lambda does not provide. | SRS 2.4 vs the scaffold | Product owner, backend | Infrastructure epic |
| Q2 | Aurora PostgreSQL (SRS 2.4, 5.3), or is DynamoDB in scope? SRS 2.5, 2.6 mention DynamoDB consistency and pricing. | SRS 2.4 vs 2.5, 2.6 | Product owner | Infrastructure epic |
| Q3 | How do users authenticate: institutional LDAP or Active Directory with local fallback (FR1), AWS Cognito (SRS 2.6), or the scaffold's own email and password? | FR1 vs SRS 2.1, 2.6 | Product owner, PUP IT | Authentication epic |
| Q4 | What is the login identifier: student number `YYYY-NNNNN-XX-N` and employee id (FR1), or email (scaffold)? | FR1 vs scaffold | Product owner | Authentication epic |
| Q5 | Sessions: HttpOnly Secure cookies with CSRF protection (SRS 4.2), or bearer tokens in the response body (scaffold)? The client's token storage depends on it. | SRS 4.2 vs scaffold | Backend, frontend | Authentication epic |
| Q6 | Are advisor and registrar separate roles, groups within Admin, or faculty assignments? Who reviews petitions? | FR17, FR18 vs FR2 | Product owner, registrar | Enrollment and petitions epics |
| Q7 | How is an admin's "department-specific" access modeled: a department scope on the grant, or one group per department? | FR2 | Backend | Authentication epic |
| Q8 | Email through Amazon SES (SRS 2.4) or SendGrid or institutional SMTP (SRS 5.3)? | SRS 2.4 vs 5.3 | Product owner | Notifications epic |
| Q9 | Backups: quarterly (FR21) or automatic daily with 30-day retention (SRS 4.4)? | FR21 vs SRS 4.4 | Product owner | Operations epic |
| Q10 | Faculty Evaluation appears in product functions (SRS 2.2) but has no functional requirement. In or out? | SRS 2.2 | Product owner | Petitions epic |
| Q11 | How and when is data migrated from the current PUP-SIS (SRS 2.5 requires backward compatibility)? No requirement describes it. | SRS 2.5, 2.6 | Product owner, PUP IT | Before launch |
| Q12 | What are the official GPA scale, standing thresholds, penalty rates, and installment rules? | FR6, FR13, FR14 | Registrar, finance office | Grades, standing, fees epics |
| Q13 | What are the current beta's baseline numbers (latency, uptime, peak concurrent users)? | Success metrics | PUP IT | Before load testing |
| Q14 | What are the official PUP maroon and gold values and logo files? | SRS 5.1 | Product owner | Client app shell |
| Q15 | FR3 is missing from the SRS numbering. Was a requirement dropped or is it a numbering gap? | SRS 3.1 | Product owner | Next SRS revision |

## 10. Rollout

What the SRS requires of a release (SRS 2.5, 4.1, 4.4):

- Zero-downtime deployments, with planned maintenance limited to 4 hours a month on weekends.
- A load test at twice the expected peak before production.
- Automated backups verified by a recovery test, and a documented disaster recovery procedure.
- Existing PUP-SIS data must remain usable (Q11).

How the system reaches users (pilot group, campus by campus, or all at once) and how a release
is rolled back are not yet defined. They are decided in the Infrastructure epic before the first
production deploy.
