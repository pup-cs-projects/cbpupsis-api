"""Fixed values for the auth domain.

The failure messages are constants because the security property is that they
are *identical* across call sites: two that drift apart become an oracle telling
an attacker which half of a credential was wrong.
"""

from __future__ import annotations

import re

INVALID_TOKEN_MESSAGE = "Invalid or expired token"
INVALID_CREDENTIALS_MESSAGE = "Incorrect email or password"
SESSION_ENDED_MESSAGE = "Your session has ended. Sign in again."
RESOURCE_NOT_FOUND_MESSAGE = "Resource not found."

#: Role sign-ins that use an institutional identifier instead of an email. One
#: message per role, byte-identical for every way that role's sign-in can fail.
STUDENT_SIGN_IN_FAILED_MESSAGE = (
    "Invalid credentials. Please verify your Student ID, birthdate, and password."
)
FACULTY_SIGN_IN_FAILED_MESSAGE = "Invalid employee ID or password."
STUDENT_ID_FORMAT_MESSAGE = "Invalid Student ID format."
EMPLOYEE_ID_FORMAT_MESSAGE = (
    "Invalid employee ID format. Expected format: YYYY-NNNNN-XX-N"
)

#: The institutional identifier shape shared by student numbers and employee
#: ids (BR-AUTH-001): year, five-digit sequence, campus code, status digit.
INSTITUTIONAL_ID_PATTERN = re.compile(r"^\d{4}-\d{5}-[A-Z]{2}-\d$")

FORGOT_PASSWORD_SUCCESS_MESSAGE = (
    "If that address is registered, a reset link has been sent."
)
RESET_TOKEN_EXPIRED_MESSAGE = "The password reset link has expired; request a new one"
RESET_TOKEN_USED_MESSAGE = (
    "The password reset link has already been used; request a new one"
)

PASSWORD_CHANGE_REQUIRED_MESSAGE = "Change your temporary password to continue."
TEMPORARY_PASSWORD_EXPIRED_MESSAGE = (
    "Your temporary password has expired. Ask the system administrator to "
    "issue a new one."
)

RESET_PASSWORD_MIN_LENGTH = 8
BCRYPT_MAX_PASSWORD_BYTES = 72
PASSWORD_MIN_LENGTH = 12
PASSWORD_MAX_LENGTH = 128
#: System-generated temporary passwords: 20 characters from an alphabet with
#: no look-alikes (0/O, 1/l/I), at least one of each complexity class.
TEMPORARY_PASSWORD_LENGTH = 20
TEMPORARY_PASSWORD_ALPHABETS = (
    "ABCDEFGHJKLMNPQRSTUVWXYZ",
    "abcdefghijkmnopqrstuvwxyz",
    "23456789",
    "!#$%&*+-=?@^_",
)

#: SHA-256 hex digest of a mailed secret.
ONE_TIME_TOKEN_MAX_LENGTH = 64

#: Session roles. A token's ``role`` claim names which sign-in issued it, and
#: each role refreshes only through its own endpoint.
ROLE_STUDENT = "student"
ROLE_FACULTY = "faculty"
ROLE_ADMIN = "admin"
ROLE_SUPERADMIN = "superadmin"

#: Roles that sign in with an institutional identifier rather than an email,
#: so the verified-email gate does not apply to their sessions.
IDENTIFIER_SIGN_IN_ROLES = frozenset({ROLE_STUDENT, ROLE_FACULTY})

#: The refresh-token cookie, one name and shape for every role.
REFRESH_COOKIE_NAME = "refresh_token"

#: Issuer label shown in authenticator apps.
TOTP_ISSUER = "CBPUPSIS"

#: Recovery codes: ten single-use codes of 80 random bits each.
RECOVERY_CODE_COUNT = 10
RECOVERY_CODE_BYTES = 10
