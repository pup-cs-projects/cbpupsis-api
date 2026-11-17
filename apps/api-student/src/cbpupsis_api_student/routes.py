"""The routers the student app serves.

``items`` is the template's reference domain, kept here as the worked example of
an app-owned domain until the first student feature replaces it.
"""

from __future__ import annotations

from cbpupsis_api_student.domains.dashboard.router import (
    router as dashboard_router,
)
from cbpupsis_api_student.domains.google_calendar.router import (
    router as google_calendar_router,
)
from cbpupsis_api_student.domains.items.router import router as items_router
from cbpupsis_api_student.domains.schedule.cor_router import (
    public_router as cor_public_router,
)
from cbpupsis_api_student.domains.schedule.cor_router import (
    router as cor_router,
)
from cbpupsis_api_student.domains.schedule.router import router as schedule_router
from cbpupsis_api_student.domains.student_account.router import (
    router as student_account_router,
)
from cbpupsis_api_student.domains.student_auth.router import (
    router as student_auth_router,
)
from cbpupsis_api_student.domains.student_enrollment.router import (
    router as enrollment_router,
)
from cbpupsis_api_student.domains.student_grades.router import router as grades_router
from cbpupsis_api_student.domains.student_profile.router import (
    router as student_profile_router,
)
from cbpupsis_shared.application import RouterMount
from cbpupsis_shared.routes import COMMON_MOUNTS

STUDENT_AUTH = RouterMount(student_auth_router, "/auth/student", "student-auth")
STUDENT_PROFILE = RouterMount(
    student_profile_router, "/student/profile", "student-profile"
)
STUDENT_DASHBOARD = RouterMount(dashboard_router, "/student", "student-dashboard")
ITEMS = RouterMount(items_router, "/items", "items")
GRADES = RouterMount(grades_router, "", "student-grades")
ENROLLMENT = RouterMount(enrollment_router, "/student/enrollment", "student-enrollment")

SCHEDULE = RouterMount(schedule_router, "", "student-schedule")

GOOGLE_CALENDAR = RouterMount(
    google_calendar_router, "/students/me/schedule/google-calendar", "google-calendar"
)

MOUNTS: tuple[RouterMount, ...] = (
    *COMMON_MOUNTS,
    STUDENT_AUTH,
    STUDENT_PROFILE,
    RouterMount(student_account_router, "/student", "student-account"),
    STUDENT_DASHBOARD,
    ITEMS,
    GRADES,
    ENROLLMENT,
    SCHEDULE,
    GOOGLE_CALENDAR,
    RouterMount(cor_router, "", "student-certificate"),
    RouterMount(cor_public_router, "", "public-certificate"),
)
