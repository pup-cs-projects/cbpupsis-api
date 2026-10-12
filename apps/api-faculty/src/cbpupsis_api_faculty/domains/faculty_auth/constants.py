"""Fixed values for the faculty authentication domain."""

from __future__ import annotations

import re

# Lockout policy (SRS 4.2 / BR-AUTH-003)
MAX_FAILED_ATTEMPTS = 5
LOCKOUT_WINDOW_MINUTES = 15
LOCKOUT_DURATION_SECONDS = 900

# Institutional Faculty ID format: YYYY-NNNNN-XX-N
FACULTY_ID_PATTERN = r"^(\d{4})-(\d{5})-([A-Z]{2})-(\d)$"
FACULTY_ID_REGEX = re.compile(FACULTY_ID_PATTERN)

# MFA and token settings
CHALLENGE_TTL_MINUTES = 5
AES_NONCE_BYTES = 12
