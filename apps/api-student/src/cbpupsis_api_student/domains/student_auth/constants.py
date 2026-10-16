"""Fixed values for the student authentication domain."""

from __future__ import annotations

import re
from datetime import timedelta, timezone

# Timezone policy (UTC+8 Philippine Standard Time)
UTC_PLUS_8 = timezone(timedelta(hours=8))

# Lockout policy (SRS 4.2 / BR-AUTH-001)
LOCKOUT_THRESHOLD = 5
MAX_FAILED_ATTEMPTS = LOCKOUT_THRESHOLD
LOCKOUT_WINDOW_MINUTES = 15
LOCKOUT_DURATION_SECONDS = 900

# Institutional Student ID format policy (BR-AUTH-001)
# Format: YYYY-XXXXX-CC-S (4-digit year, 5-digit sequence,
# 2-letter campus code, 1-digit status)
STUDENT_ID_PATTERN = r"^\d{4}-\d{5}-[A-Z]{2}-\d$"
STUDENT_ID_REGEX = re.compile(STUDENT_ID_PATTERN)

# Session and token settings (AC-001.7)
IDLE_SESSION_TIMEOUT_MINUTES = 15
ACCESS_TOKEN_EXPIRE_SECONDS = 900
REFRESH_TOKEN_COOKIE_NAME = "refresh_token"
REFRESH_TOKEN_COOKIE_MAX_AGE = 30 * 24 * 3600
DEFAULT_CLIENT_IP = "127.0.0.1"

# Email notification settings (AC-001.4)
ACCOUNT_LOCKED_EMAIL_SUBJECT = "Security Alert: Account Locked"
ACCOUNT_LOCKED_EMAIL_BODY = (
    "Your CBPUPSIS student account has been locked for 15 minutes "
    "due to 5 consecutive failed login attempts."
)
