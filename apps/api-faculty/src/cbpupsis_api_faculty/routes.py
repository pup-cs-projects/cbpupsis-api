"""The routers the faculty app serves.

``faculty_auth`` is faculty sign-in and MFA; ``sections`` is what a signed-in
faculty member may read. The common routers (account recovery, profile,
notifications) are served here like in every app.
"""

from __future__ import annotations

from cbpupsis_api_faculty.domains.faculty_auth.router import (
    router as faculty_auth_router,
)
from cbpupsis_api_faculty.domains.sections.router import router as sections_router
from cbpupsis_shared.application import RouterMount
from cbpupsis_shared.routes import COMMON_MOUNTS

FACULTY_AUTH = RouterMount(faculty_auth_router, "/auth/faculty", "faculty-auth")
SECTIONS = RouterMount(sections_router, "/sections", "sections")

MOUNTS: tuple[RouterMount, ...] = (*COMMON_MOUNTS, FACULTY_AUTH, SECTIONS)
