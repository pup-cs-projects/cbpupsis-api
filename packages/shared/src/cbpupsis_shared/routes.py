"""Where each shared domain's router is served, declared once for every app.

An app decides *which* of these it serves; the prefix and tag are fixed here, so
the same router can never be mounted at two different paths by two apps. The
IAM and audit routers are shared code but only the admin app serves them.
"""

from __future__ import annotations

from cbpupsis_shared.application import RouterMount
from cbpupsis_shared.domains.audit.router import router as audit_router
from cbpupsis_shared.domains.auth.router import router as auth_router
from cbpupsis_shared.domains.iam.router import router as iam_router
from cbpupsis_shared.domains.notifications.router import router as notifications_router
from cbpupsis_shared.domains.reports.router import router as reports_router
from cbpupsis_shared.domains.users.router import management_router
from cbpupsis_shared.domains.users.router import router as users_router

# Tagged "account": what it documents is email verification, password reset, and
# password change for every role. Sign-in is each role's own /auth/<role> router.
AUTH = RouterMount(auth_router, "/auth", "account")
USERS = RouterMount(users_router, "/users", "users")
#: Other people's accounts; the admin app alone serves it.
USER_MANAGEMENT = RouterMount(management_router, "/admin/users", "users")
NOTIFICATIONS = RouterMount(notifications_router, "/notifications", "notifications")
IAM = RouterMount(iam_router, "/iam", "iam")
AUDIT = RouterMount(audit_router, "/audit", "audit")
REPORTS = RouterMount(reports_router, "/admin/reports", "admin-reports")

#: Served by every app: your account recovery and password change, your own
#: profile, your own notifications. Never anything another role administers.
COMMON_MOUNTS: tuple[RouterMount, ...] = (AUTH, USERS, NOTIFICATIONS)
