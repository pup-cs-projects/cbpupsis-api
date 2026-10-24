"""Fixed values and constants for the student dashboard domain."""

from __future__ import annotations

import re

#: Empty state fallback messages (AC-101.5)
EMPTY_ANNOUNCEMENTS = "No announcements at this time."
EMPTY_EVENTS = "No upcoming events on the academic calendar."
EMPTY_RECENT_ACTIVITY = (
    "Your account activity will appear here once you begin using the portal."
)

#: Default endpoint links (AC-101.4)
DEFAULT_SCHEDULE_URL = "/api/v1/student/schedule"

#: Pagination and bounded query limits
DASHBOARD_LIMIT_MIN = 1
DASHBOARD_LIMIT_MAX = 50
DEFAULT_LIMIT = 10

#: Format pattern for academic year query parameters (YYYY-YYYY)
YEAR_PATTERN = re.compile(r"^\d{4}-\d{4}$")

#: Validation error messages
ANNOUNCEMENT_LIMIT_ERROR_MESSAGE = "announcementLimit must be between 1 and 50."
EVENT_LIMIT_ERROR_MESSAGE = "eventLimit must be between 1 and 50."
ACTIVITY_LIMIT_ERROR_MESSAGE = "activityLimit must be between 1 and 50."
YEAR_FORMAT_ERROR_MESSAGE = "year parameter is malformed; expected YYYY-YYYY."
