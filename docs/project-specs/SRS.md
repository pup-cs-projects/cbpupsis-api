**SOFTWARE REQUIREMENTS SPECIFICATION (SRS)**

CBPUPSIS: Cloud-Based PUP Student Information System

# **1\. INTRODUCTION**

**1.1 Purpose**
This document provides a comprehensive description of the system requirements for the Cloud-Based PUP Student Information System (CBPUPSIS). It details all functional and non-functional requirements necessary for the development, implementation, and maintenance of an enhanced student information management system that serves students, faculty, and administrative staff at Polytechnic University of the Philippines.

**1.2 Scope**
The CBPUPSIS is designed to serve as a centralized platform for managing student academic records, financial information, course enrollment, grade management, and institutional reporting. The system encompasses student self-service functions, faculty grade submission capabilities, comprehensive financial management, and administrative operations. The system is a web-based application accessible from any internet-connected device and includes modules for authentication, enrollment, grades, financial management, forms processing, communications, and reporting.

**1.3 Definitions, Acronyms, Abbreviations**
SRS – Software Requirements Specification

UI – User Interface

API – Application Programming Interface

RBAC – Role-Based Access Control

FHE – Free Higher Education (RA 10931\)

LDAP – Lightweight Directory Access Protocol

PCI DSS – Payment Card Industry Data Security Standard

WCAG – Web Content Accessibility Guidelines

GPA – Grade Point Average

MTBF – Mean Time Between Failures

MTTR – Mean Time To Repair

TLS – Transport Layer Security

JWT – JSON Web Token

REST – Representational State Transfer

CRUD – Create, Read, Update, Delete

PII – Personally Identifiable Information

DSAR – Data Subject Access Request

CoR – Certificate of Registration

HSTS – HTTP Strict Transport Security

&nbsp;

**1.4 References**

* IEEE 830-1998: Recommended Practice for Software Requirements Specifications
* Philippine Data Privacy Act (RA 10173\)
* Free Higher Education Act (RA 10931\)
* WCAG 2.1 Accessibility Guidelines
* PUP Administrative Code and Academic Policies
* Current PUP-SIS System Implementation
* Payment Gateway Provider Documentation (Xendit)
* ISO/IEC/IEEE 29148:2018 Systems and software engineering

**1.5 Overview**
This document outlines all system features, functional requirements, non-functional requirements, constraints, and dependencies for CBPUPSIS. It serves as the primary reference for development, testing, and implementation teams. The document is organized into sections covering introduction, overall description, system features, non-functional requirements, external interfaces, and appendices with

# **2\. OVERALL DESCRIPTION**

**2.1 Product Perspective**
CBPUPSIS is a major enhancement of the current PUP Student Information System beta that has been operational since 2020\. The system serves as the central repository for student academic and financial records at the Polytechnic University of the Philippines, supporting approximately 15,000+ students across multiple campuses. The system will include its own authentication module, payment processing system, email notification service, and file storage system. All components will be built as part of the CBPUPSIS development.

**2.2 Product Functions**

* User Authentication and Authorization – Secure login with role-based access control for students, faculty, administrators, and system administrators
* Student Profile Management – View and update personal information, emergency contacts, and enrollment status
* Course Schedule Management – View course schedules, fees, faculty information, and course details
* Grade Management – Students view grades and GPA; faculty submit grades and track assessment scores
* Financial Account Management – View fees, payment history, balance, and submit online payments
* Enrollment and Registration – Students enroll in courses, manage add/drop requests, and receive enrollment confirmation
* Forms and Appeals – Submit Faculty Evaluation and Grade Appeals
* Administrative Functions – User management, academic calendar configuration, financial settings, and system monitoring
* Reporting and Analytics – Generate institutional reports on enrollment, academic performance, and financial data
  &nbsp;

**2.3 User Classes and Characteristics**

* Students (15,000+): Primary users; access own records; low to medium technical expertise; daily to weekly usage; mobile-first preference
* Faculty (30-80): Grade submission and class management; medium technical expertise; periodic usage during semester; office/classroom access
* System Administrators (3-5): System configuration, backups, security; high technical expertise; daily monitoring and maintenance
* Superadministrators (1-2): System governance and emergency overrides; expert-level expertise; decision-making authority

**2.4 Operating Environment**

Development Environment:

* IDE: Visual Studio Code
* Language: Python 3.12
* Framework: FastAPI (lightweight, fast)
* Local Testing: Uvicorn (ASGI server) \+ Docker
* Containerization: Docker for local development and production
* Database: PostgreSQL
* Caching: In-memory cache (FastAPI built-in)
* Version Control: Git with GitHub/GitLab
* Testing: pytest for Python

Production/Deployment Environment (AWS Serverless):

* Container Registry: Amazon ECR (Elastic Container Registry)
* Compute: AWS Lambda (containerized FastAPI application)
* API Gateway: Amazon API Gateway for REST endpoints
* Web Application Firewall: AWS WAF (Web Application Firewall) protecting API Gateway
* Database: AWS Aurora (serverless PostgreSQL database)
* Cache: In-memory cache (FastAPI) \+ CloudFront \+ Tanstack
* Storage: Amazon S3 (object storage for files, documents, transcripts)
* Content Delivery: Amazon CloudFront (CDN for static assets and API caching)
* Email: Amazon SES (Simple Email Service)
* Load Balancing: API Gateway \+ CloudFront handles auto-scaling and distribution
* Monitoring: CloudWatch Logs and X-Ray
* CI/CD: GitHub Actions or AWS CodePipeline (auto-build Docker images to ECR, deploy to Lambda)

Deployment Pipeline:

* Developer pushes code to GitHub
* GitHub Actions builds Docker image
* Docker image pushed to Amazon ECR
* Lambda function updated with new container image
* CloudFront cache invalidated for updated content

Client Environment:

* Operating Systems: Windows 10+, macOS 10.13+, Linux
* Browsers: Chrome 90+, Firefox 88+, Safari 14+, Edge 90+
* Network: HTTPS/TLS 1.2+, 1Mbps+ internet connection
* Mobile: iOS 12+, Android 8+ (responsive web design)
* CDN: Requests routed through CloudFront for faster delivery

**2.5 Constraints**
**Technical**: Lambda has 15-minute execution timeout; DynamoDB uses eventual consistency; container images limited to 10GB; CloudFront cache invalidation takes 60+ seconds; in-memory cache limited to container memory; FastAPI requires stateless design for scaling.

**Regulatory**: Must comply with Philippine Data Privacy Act (RA 10173\) and RA 10931 Free Education requirements; WCAG 2.1 Level AA accessibility required; AWS WAF must block known attack patterns.

**Business**: 12-18 month implementation timeline; 6-8 person development team; backward compatibility with current PUP-SIS data required; AWS costs must be optimized; zero-downtime deployments required.

**Infrastructure**: Single AWS region (ap-southeast-1) with multi-AZ deployment; VPC security required; no hardcoded secrets allowed; 30-day minimum CloudWatch log retention.

resources dependent on IT department; stakeholder review cycles required

**2.6 Assumptions and Dependencies**
**Technology Assumptions**: AWS infrastructure stable (99.99% SLA); Docker and ECR maintained by AWS; DynamoDB auto-scales to peak loads; FastAPI framework continues supported; Lambda cold start acceptable (\< 5 seconds); GitHub Actions executes reliably.

**User Assumptions:** Students have internet access; faculty familiar with web applications; administrative staff trained on system operations; developers understand Docker and AWS.

**Data Assumptions**: Current system data valid and accurate; historical data migrable to AWS Aurora DB without loss; student IDs remain unique; financial records retained 7 years minimum; AWS Aurora DB throughput sufficient for 15,000 concurrent users.

**Business Assumptions:** Free Higher Education program continues; current academic policies remain; AWS billing budget approved; stakeholders adopt system without resistance.

**AWS Dependencies:** AWS Lambda, AWS Aurora DB, CloudFront, WAF, ECR, S3, SES available in ap-southeast-1; API Gateway maintains 99.95% uptime; CloudWatch monitoring functional; Secrets Manager secures credentials.

**Development Dependencies:** GitHub for version control; GitHub Actions for CI/CD; Docker for containerization; FastAPI and Python 3.12+ ecosystem maintained; security scanning tools available.

**Operational Dependencies:** AWS support plan for production issues; Cognito for authentication; helpdesk trained on system; DevOps team manages Lambda, AWS Aurora DB, CloudFront; Finance approves AWS budget; IT leadership reviews major changes.

**Mitigation Strategies:** Provisioned concurrency for Lambda cold starts; on-demand DynamoDB pricing for auto-scaling; TTL optimization for CloudFront cache hits; gradual WAF rule rollout; automated dependency scanning for security; lifecycle policies for S3 storage cost control.

# **3\. SYSTEM FEATURES (FUNCTIONAL REQUIREMENTS)**

3.1 Authentication and Authorization Module

FR1: User Login and Authentication

* Students, faculty, and administrators authenticate using institutional credentials
* System accepts student ID (format: YYYY-NNNNN-XX-N) and password
* LDAP/Active Directory integration for institutional authentication
* Fallback local authentication if LDAP unavailable
* Account lockout after 5 failed attempts within 15 minutes
* Session timeout after 30 minutes of inactivity with 5-minute warning
* Password recovery via email verification with 24-hour link expiration
* All login attempts logged with timestamp and status

FR2: Role-Based Access Control

* Four primary roles defined: Student, Faculty, Admin, Superadmin
* Role-based menu visibility showing only available functions
* Students access only own records; cannot view other students' data
* Faculty access assigned courses and related student data
* Admin users access department-specific data based on assignment
* Superadmin users have full system access with override capability
* Granular permissions at function level (read, write, delete, approve)
* Server-side permission checks on all requests; UI checks for user experience only
* Authorization failures logged and monitored for suspicious patterns

3.2 Student Module

FR4: Student Profile Management

* Students view personal information (name, ID, enrollment status, program)
* Edit capability for name, phone, email, address (limited fields)
* Emergency contact management (multiple contacts supported)
* Profile picture upload and update
* Profile completion status indicator

FR5: Course Schedule Viewing

* Display course schedule by semester (current semester default)
* Show course code, description, times, faculty name
* Separate display of lecture and laboratory hours
* Unit count and total credits calculation
* Enrollment status indicator per course
* Filtering by course code or faculty name
* Export to PDF or iCal calendar format
* Add to calendar applications (Google Calendar, Outlook)

FR6: Grade Viewing

* Display grades by semester with course information
* Show course code, description, units, final grade, grade status
* GPA calculation and display (excludes NSTP and non-numeric ratings)
* Academic standing display based on GPA
* Complete transcript with multi-year history

&nbsp;

FR7: Enrollment Management

* Display enrollment status (enrolled, not enrolled, pending)
* Show Free Higher Education (FHE) qualification and benefits
* View available courses during enrollment period
* Enforce enrollment period dates preventing out-of-period enrollment
* Validate course prerequisites before enrollment
* Enforce maximum unit limit (21 units per semester)
* Prevent course schedule conflicts
* Send email notifications for key events (grade posted, enrollment, payment received)
* Enrollment confirmation with course list
* Certificate of Registration (CoR) generation and download
* Course add/drop request submission during designated period

FR8: Financial Account Management

* Display financial account showing assessments by semester
* Show payment status (paid, partial, due, overdue)
* Display account balance (amount owed or overpaid)
* Complete payment history with dates, amounts, reference numbers
* Display Free Education benefits applied to account
* Online payment submission through payment gateway
* Payment receipt generation and download
* Display payment due dates and late payment penalties
* Installment payment plan options available
* Overdue payment notifications via email
* Prevent enrollment if student has outstanding holds

3.3 Faculty Module

FR9: Class Roster Management

* Faculty dashboard showing assigned courses and statistics
* View complete class roster with student names, IDs, contact information
* Student add/drop status indication in roster

FR10: Grade Submission and Management

* Grade submission interface for assigned courses
* Draft mode for editing; final submission distinct from draft
* Grade submission deadline enforcement with admin override option
* Confirmation upon grade submission with email notification
* Class grade distribution display (statistics, curve, performance analysis)
* Pass/Fail grade option for courses without numeric grades

3.4 Academic Management Module

FR11: Academic Calendar Management

* Maintain academic calendar with semesters and special periods
* Configure enrollment periods (early, regular, late enrollment)
* System enforces period boundaries
* Display academic calendar to users with important dates
* Configure grade submission deadlines per semester
* Set all critical academic dates and milestones

FR12: Course Catalog and Curriculum

* Maintain course catalog with code, title, description, credits, semester offered
* Define course prerequisites and corequisites
* Enforce prerequisites during enrollment with waiver option
* Define program curricula with required and elective courses
* Create and manage course sections
* Track course capacity and current enrollment
* Support course substitution for curriculum changes&nbsp;

&nbsp;

FR13: Academic Standing Management

* Calculate academic standing based on GPA thresholds
* Define standing categories (Good Standing, Probation, Suspension, Dismissal)
* Enforce restrictions on probationary students (reduced course load)
* Process academic suspension or dismissal per policy
* Allow students to appeal academic standing decisions

3.5 Financial Management Module

FR14: Fee Structure Configuration

* Configure tuition fee structure by program and year
* Support per-unit charges and special fees
* Apply Free Higher Education (FHE) exemptions per RA 10931
* Support multiple assessment types (tuition, fees, miscellaneous)
* Apply late payment penalties with configurable rates
* Support installment payment plans with schedule generation

FR15: Payment Processing

* Integrate with Xendit payment gateway
* Validate payment amounts and account status before processing
* Generate payment receipt upon successful transaction
* Handle payment failures with clear error messaging
* Implement PCI DSS compliance for payment security
* Update student account balance in real-time or within 1 hour
* Track payment history with dates, amounts, reference numbers
* Process refunds and reversals of overpaid amounts

FR16: Financial Reporting

* Track collection rates and generate collection reports
* Support payment reconciliation with bank feeds
* Generate revenue reports by semester and program

3.6 Forms and Requests Module

FR17: Course Add/Drop Form

* Students submit add/drop requests during designated periods
* Validate prerequisites before allowing course add
* Validate that course change does not violate enrollment limits
* Route form to advisor or registrar for approval
* Display form status (submitted, pending, approved, rejected) to student
* Update schedule and account upon approval
* Track all form decisions and changes

FR18: Petition Forms

* Provide petition templates (late enrollment, grade appeal, academic standing appeal)
* Students submit petition with supporting documentation
* Route petitions to appropriate faculty for review

3.7 Administrative Module

FR19: User Account Management

* Create user accounts for students, faculty, and staff
* Assign roles (Student, Faculty, Admin, Superadmin)
* Activate and deactivate accounts
* Reset user passwords with temporary password generation
* Manage user permissions and role assignments
* Log all user management activities with audit trail
* Export user list and account status reports

FR20: System Configuration

* Configure academic calendar and enrollment periods
* Configure course catalog and curriculum
* Configure fee structure and financial parameters
* Configure notification templates and schedules
* Configure system-wide policies and parameters

FR21: Data Management and Backup

* Monitor and perform quarterly backup status&nbsp;
* Manage data retention policies with automatic archival
* Maintain referential integrity with foreign key constraints

FR22: System Monitoring and Maintenance

* Provide performance monitoring dashboard
* View system logs and error messages with filtering
* Alert administrators of critical issues
* Monitor uptime and system health

3.8 Reporting and Analytics Module

FR23: Enrollment Reports

* Generate enrollment statistics by program
* Generate enrollment reports by student status
* Generate demographic enrollment reports
* Support filtering and drill-down capability

FR24: Academic Performance Reports

* Generate grade distribution reports by course
* Generate academic standing reports
* Generate retention and progression reports
* Generate faculty performance reports (TBD)

# **4\. NON-FUNCTIONAL REQUIREMENTS**

4.1 Performance Requirements

* API response time: 90th percentile ≤ 500ms; 95th percentile ≤ 1000ms
* Database query time: 99th percentile ≤ 1 second for indexed queries
* System shall support 5,000 concurrent users in enrollment without performance degradation
* System shall process 100 transactions per second
* System uptime: 99.5% monthly (maximum 3.6 hours downtime)
* Planned maintenance: Maximum 4 hours per month, scheduled on weekends
* Load testing required before production deployment (2x peak expected load)
* Horizontal scalability for future growth (support 30,000 students)
* Database storage: Minimum 500GB for 10 years of transaction history

4.2 Security Requirements

* All communication encrypted with HTTPS/TLS 1.2 or higher
* HSTS headers enforced; no HTTP connections allowed
* Secure cookies with HttpOnly and Secure flags
* Passwords meet complexity requirements (8+ characters, mixed case, numbers, symbols)
* Passwords hashed with bcrypt or equivalent (12+ rounds)
* Account lockout after 5 failed attempts within 15 minutes
* Session timeout after 30 minutes inactivity
* No credit card data stored in system (PCI DSS compliance, tokenization required)
* SQL injection prevention through parameterized queries and ORM
* XSS prevention through input validation and output encoding
* CSRF prevention through token validation and SameSite cookies
* Principle of least privilege in role-based access control
* Audit logging of all user activities and authorization checks
* Intrusion detection and prevention systems
* Regular security patching and vulnerability scanning
* Multi-factor authentication support (optional)
* Compliance with Philippine Data Privacy Act (RA 10173\)
* Data Subject Access Request (DSAR) capability for users
* User data deletion capability (Right to Erasure) with policy exceptions
* Data minimization principle implementation

4.3 Usability Requirements

* Intuitive interface requiring minimal training
* WCAG 2.1 Level AA accessibility compliance
* All pages accessible to screen readers
* Keyboard navigable interface
* Descriptive alt text for all images
* Responsive design for mobile, tablet, desktop
* Touch targets minimum 44x44 pixels
* Support landscape and portrait orientations
* Clear error messages with actionable guidance
* Consistent navigation structure across all pages
* Breadcrumb navigation on all pages except home
* Loading indicators during asynchronous operations
* Professional appearance with PUP branding

&nbsp;

4.4 Reliability Requirements

* MTBF (Mean Time Between Failures): \> 720 hours (no more than 1 crash per 30 days)
* MTTR (Mean Time To Repair): \< 2 hours for critical issues; \< 4 hours for major issues
* Zero data corruption incidents; all transactions ACID compliant
* Automatic daily backups with 30-day retention minimum
* Disaster recovery capability with documented procedures
* Backup integrity verification and recovery testing
* Automated failover for high-availability configuration
* Data integrity audit and transaction rollback testing

4.5 Maintainability Requirements

* Code follows consistent standards and conventions
* Code review process enforced before deployment
* Code coverage \> 80% with automated unit tests
* Integration and end-to-end (E2E) tests for critical workflows
* Comprehensive documentation (API, architecture, runbooks)
* Version control with Git (GitHub or GitLab)
* CI/CD pipeline for automated testing and deployment
* Code linting and formatting tools (ESLint/Prettier, Black/Pylint)
* Hot-fix capability without system restart
* Configuration management for different environments
* Hot-fixes and selected bug fixes deployable without restart

4.6 Compliance and Regulatory Requirements

* Philippine Data Privacy Act (RA 10173\) compliance
* Free Higher Education Act (RA 10931\) implementation
* WCAG 2.1 Level AA accessibility compliance
* Regulatory audit support with complete audit trail
* Data integrity verification

# **5\. EXTERNAL INTERFACE REQUIREMENTS**

5.1 User Interface Requirements

* Professional appearance with PUP visual identity (maroon/gold colors, official logo)
* Responsive design supporting 320px to 2560px screen width
* Clear, readable typography (minimum 12px font size)
* Consistent navigation structure on all pages
* Breadcrumb navigation showing current page location
* Loading indicators for asynchronous operations
* Clear error message display with actionable guidance
* Form labels aligned with input fields
* Required fields visually indicated with asterisk (\*)
* Field-level error messages displayed inline
* Submit and cancel buttons with clear labels
* Prevent double-submit with disabled button after click
* Save form input to local storage for recovery
* Auto-save capability for long forms (every 30 seconds)
* Mobile hamburger menu (\< 768px width)
* Mobile layout removes non-essential elements
* Touch targets minimum 44x44 pixels
* Landscape and portrait orientation support
* Print-friendly stylesheet hiding navigation and non-essential elements
* Professional print output for critical documents

5.2 Hardware Interface Requirements

* Support TCP/IP networks (both IPv4 and IPv6)
* Support 16-bit or higher color depth displays
* Support mouse, touchpad, keyboard input devices
* Support touchscreen input on mobile devices
* Support network and connected printers

5.3 Software Interface Requirements

* PostgreSQL 16 database with ORM abstraction layer
* Database transaction consistency (ACID properties)
* Connection pooling for performance optimization
* Full-text search capability for student/faculty records
* SendGrid or institutional SMTP integration for email
* Email templates with variable substitution
* Email queue with retry logic for failed messages
* Payment gateway integration (Xendit)
* PCI DSS compliance for payment processing
* Payment webhook callbacks for status updates
* Secure file upload with virus scanning
* File versioning and retention management
* PDF, Excel, CSV export functionality
* JSON API responses with consistent structure

&nbsp;

5.4 Communication Interface Requirements

* All client-server communication via HTTPS/TLS 1.2+
* HSTS headers enforced for secure connections
* RESTful API following REST conventions
* Standard HTTP methods for resource operations
* JSON format for API responses with documented schema
* Content negotiation for format selection
* API versioning for backward compatibility
* Rate limiting per user/IP (429 status returned)
* Pagination support for large datasets (limit/offset)
* Request/response compression (gzip)
* Webhook support for payment gateways
* Webhook signature verification for security
* Webhook retry logic with exponential backoff
* Email authentication (DKIM, SPF, DMARC)
* HTML and plain text email format support

# **6\. APPENDICES**&nbsp;&nbsp;&nbsp;

### **STUDENT USE CASES**

**UC-STU-001: Student Login**

Actor: Student Precondition: Student has valid ID and password Main Flow:

1. Student enters student ID (YYYY-NNNNN-XX-N)
2. Student enters password
3. System validates credentials
4. System creates session
5. Student redirected to dashboard Postcondition: Student logged in and can access own records

**UC-STU-002: Enroll in Courses**

Actor: Student Precondition: Enrollment period is open; student meets prerequisites Main Flow:

1. Student navigates to Enrollment page
2. System displays available courses
3. Student selects courses (max 21 units)
4. System validates prerequisites and conflicts
5. Student confirms selection
6. System processes enrollment Postcondition: Student enrolled; confirmation sent via email

**UC-STU-003: Download Certificate of Registration (CoR)**

Actor: Student Precondition: Student is enrolled in courses for current semester Main Flow:

1. Student navigates to Enrollment page
2. System displays "Download CoR" button
3. Student clicks button
4. System generates CoR PDF with enrolled courses
5. Student downloads PDF file Postcondition: CoR PDF downloaded and ready for submission

### **FACULTY USE CASES**

**UC-FAC-001: Faculty Login**

Actor: Faculty Precondition: Faculty has valid employee ID and password Main Flow:

1. Faculty enters employee ID
2. Faculty enters password
3. System validates credentials
4. System creates session
5. Faculty redirected to dashboard showing assigned courses Postcondition: Faculty logged in and can access courses

**UC-FAC-002: Submit Grades**

Actor: Faculty Precondition: Grade submission period is open; faculty has courses assigned Main Flow:

1. Faculty navigates to Grade Submission page
2. Faculty selects course section
3. System displays class roster
4. Faculty enters assessment scores for each student
5. System calculates final grades automatically
6. Faculty reviews and confirms grades
7. Faculty clicks "Submit Final Grades"
8. System displays confirmation Postcondition: Grades submitted and locked; students can view grades

### **ADMINISTRATOR USE CASES**

**UC-ADM-001: Admin Login**

Actor: Administrator Precondition: Admin has valid credentials with Admin role Main Flow:

1. Admin enters ID and password
2. System validates credentials and role
3. System creates session with Admin permissions
4. Admin redirected to Admin dashboard Postcondition: Admin logged in with full system access

**UC-ADM-002: Create User Account**

Actor: Administrator Precondition: New user information available Main Flow:

1. Admin navigates to User Management page
2. Admin clicks "Create Account"
3. Admin enters: name, ID, email, role
4. System validates information
5. Admin clicks "Create"
6. System generates temporary password
7. System sends welcome email to new user Postcondition: User account created; user can log in

**UC-ADM-003: Configure Academic Calendar**

Actor: Administrator Precondition: Academic dates determined by institution Main Flow:

1. Admin navigates to Academic Calendar page
2. Admin enters term dates (start, end)
3. Admin sets enrollment periods (early, regular, late)
4. Admin sets critical deadlines (add/drop, grade submission)
5. Admin configures holidays (no classes)
6. Admin clicks "Save"
7. System activates calendar Postcondition: Calendar active; system enforces dates

**UC-ADM-004: Configure Fee Structure**

Actor: Administrator Precondition: Tuition rates and fees determined Main Flow:

1. Admin navigates to Financial Configuration page
2. Admin enters tuition per unit by program
3. Admin enters fixed fees (lab, registration, etc.)
4. Admin sets Free Higher Education (FHE) exemptions
5. Admin defines payment due date and penalties
6. Admin sets installment payment plans
7. Admin clicks "Save"
8. System applies fees to student accounts Postcondition: Fees configured and applied

**UC-ADM-005: Generate Reports**

Actor: Administrator Precondition: System data current and available Main Flow:

1. Admin navigates to Reports page
2. Admin selects report type (Enrollment, Grades, Financial)
3. Admin specifies parameters (semester, program, date range)
4. System generates report with data and charts
5. Admin reviews report
6. Admin exports to PDF or Excel
7. Admin downloads file Postcondition: Report generated and exported
