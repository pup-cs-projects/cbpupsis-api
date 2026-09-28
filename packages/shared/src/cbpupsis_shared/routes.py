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
from cbpupsis_shared.domains.users.router import router as users_router

AUTH = RouterMount(auth_router, "/auth", "auth")
USERS = RouterMount(users_router, "/users", "users")
NOTIFICATIONS = RouterMount(notifications_router, "/notifications", "notifications")
IAM = RouterMount(iam_router, "/iam", "iam")
AUDIT = RouterMount(audit_router, "/audit", "audit")

#: Served by every app: signing in, your own profile, your own notifications.
COMMON_MOUNTS: tuple[RouterMount, ...] = (AUTH, USERS, NOTIFICATIONS)
