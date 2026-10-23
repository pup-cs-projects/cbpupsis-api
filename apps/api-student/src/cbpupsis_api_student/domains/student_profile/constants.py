"""Fixed values for the student_profile domain (US-STU-002)."""

from __future__ import annotations

import re
from zoneinfo import ZoneInfo

PETITION_ROUTE = "/api/v1/student/petitions/record-correction"

#: 5 MB upload ceiling (AC-102.6, AC-102.7).
AVATAR_MAX_BYTES = 5 * 1024 * 1024

#: Philippine mobile number shape: +639XXXXXXXXX or 09XXXXXXXXX (AC-102.4).
PHILIPPINE_MOBILE_PATTERN = re.compile(r"^(?:\+639|09)\d{9}$")

#: Basic email shape check paired with EmailStr validation (AC-102.4).
EMAIL_SHAPE_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

#: Audit timestamps are recorded in UTC+8 (Asia/Manila) per issue #95.
MANILA_TZ = ZoneInfo("Asia/Manila")

JPEG_MAGIC_PREFIX = b"\xff\xd8\xff"
PNG_MAGIC_PREFIX = b"\x89PNG\r\n\x1a\n"

#: Known malware / script payloads refused by the upload scanner.
MALWARE_SIGNATURES: tuple[bytes, ...] = (
    b"X5O!P%@AP[4\\ZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*",
    b"<?php",
    b"<script",
    b"#!/",
)

#: Master demographic and academic fields locked against self-service edits
#: (BR-STAT-004, AC-102.3). Maps accepted input names to canonical camelCase.
LOCKED_FIELD_ALIASES: dict[str, str] = {
    "studentId": "studentId",
    "student_id": "studentId",
    "studentNumber": "studentId",
    "student_number": "studentId",
    "firstName": "firstName",
    "first_name": "firstName",
    "middleName": "middleName",
    "middle_name": "middleName",
    "lastName": "lastName",
    "last_name": "lastName",
    "legalName": "legalName",
    "legal_name": "legalName",
    "fullName": "fullName",
    "full_name": "fullName",
    "birthdate": "birthdate",
    "birthDate": "birthdate",
    "birth_date": "birthdate",
    "gender": "gender",
    "civilStatus": "civilStatus",
    "civil_status": "civilStatus",
    "program": "program",
    "programme": "program",
    "programCode": "programCode",
    "program_code": "programCode",
    "catalogYear": "catalogYear",
    "catalog_year": "catalogYear",
    "curriculumYear": "catalogYear",
    "curriculum_year": "catalogYear",
    "yearLevel": "yearLevel",
    "year_level": "yearLevel",
    "enrollmentStatus": "enrollmentStatus",
    "enrollment_status": "enrollmentStatus",
}

#: Editable profile fields mapped to (canonical_field_name, UserProfile column).
EDITABLE_FIELD_COLUMNS: dict[str, tuple[str, str]] = {
    "phone": ("phone", "phone_number"),
    "phoneNumber": ("phone", "phone_number"),
    "phone_number": ("phone", "phone_number"),
    "mobile": ("phone", "phone_number"),
    "contactNumber": ("phone", "phone_number"),
    "contact_number": ("phone", "phone_number"),
    "email": ("emailAddress", "personal_email"),
    "emailAddress": ("emailAddress", "personal_email"),
    "email_address": ("emailAddress", "personal_email"),
    "personalEmail": ("emailAddress", "personal_email"),
    "personal_email": ("emailAddress", "personal_email"),
    "permanentAddress": ("permanentAddress", "home_address"),
    "permanent_address": ("permanentAddress", "home_address"),
    "homeAddress": ("permanentAddress", "home_address"),
    "home_address": ("permanentAddress", "home_address"),
    "mailingAddress": ("permanentAddress", "home_address"),
    "mailing_address": ("permanentAddress", "home_address"),
    "currentAddress": ("currentAddress", "current_address"),
    "current_address": ("currentAddress", "current_address"),
}
