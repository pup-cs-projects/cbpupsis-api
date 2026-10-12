"""The routers the faculty app serves.

``faculty_auth`` handles specialized faculty authentication and multi-factor
authentication (MFA) flows.
"""

from __future__ import annotations

from cbpupsis_api_faculty.domains.faculty_auth.router import (
    router as faculty_auth_router,
)
from cbpupsis_shared.application import RouterMount
from cbpupsis_shared.routes import NOTIFICATIONS, USERS

FACULTY_AUTH = RouterMount(faculty_auth_router, "/faculty-auth", "faculty-auth")
AUTH = FACULTY_AUTH

MOUNTS: tuple[RouterMount, ...] = (AUTH, USERS, NOTIFICATIONS)
