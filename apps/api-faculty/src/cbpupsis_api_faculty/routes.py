"""The routers the faculty app serves.

``faculty_auth`` is faculty sign-in and MFA; ``sections`` is what a signed-in
faculty member may read. The common routers (account recovery, profile,
notifications) are served here like in every app.
"""

from __future__ import annotations

from cbpupsis_api_faculty.domains.faculty_auth.router import (
    router as faculty_auth_router,
)
from cbpupsis_api_faculty.domains.faculty_grades.router import (
    incompletes_router,
)
from cbpupsis_api_faculty.domains.faculty_grades.router import (
    router as grades_router,
)
from cbpupsis_api_faculty.domains.faculty_profile.router import (
    router as profile_router,
)
from cbpupsis_api_faculty.domains.faculty_sections.router import (
    router as sections_router,
)
from cbpupsis_shared.application import RouterMount
from cbpupsis_shared.routes import COMMON_MOUNTS

FACULTY_AUTH = RouterMount(faculty_auth_router, "/auth/faculty", "faculty-auth")
FACULTY_PROFILE = RouterMount(profile_router, "/faculty/profile", "faculty-profile")
SECTIONS = RouterMount(sections_router, "/faculty/sections", "faculty-sections")
GRADES = RouterMount(grades_router, "/faculty/sections", "faculty-grades")
INCOMPLETES = RouterMount(
    incompletes_router, "/faculty/incompletes", "faculty-incompletes"
)

MOUNTS: tuple[RouterMount, ...] = (
    *COMMON_MOUNTS,
    FACULTY_AUTH,
    FACULTY_PROFILE,
    SECTIONS,
    GRADES,
    INCOMPLETES,
)
