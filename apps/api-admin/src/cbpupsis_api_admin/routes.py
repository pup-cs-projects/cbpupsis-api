"""The routers the admin app serves, and its development-only admin panel.

IAM and audit are shared code, because every app enforces permissions and
records audited events, but administering them is this app's job alone.
"""

from __future__ import annotations

from collections.abc import Callable

from fastapi import FastAPI

from cbpupsis_api_admin.domains.admin_auth.router import (
    protected_router as admin_router,
)
from cbpupsis_api_admin.domains.admin_auth.router import router as admin_auth_router
from cbpupsis_api_admin.domains.admin_auth.router import (
    superadmin_auth_router,
    superadmin_protected_router,
)
from cbpupsis_api_admin.domains.superadmin_ops.router import (
    router as superadmin_ops_router,
)
from cbpupsis_shared.application import RouterMount
from cbpupsis_shared.routes import AUDIT, COMMON_MOUNTS, IAM

ADMIN_AUTH = RouterMount(admin_auth_router, "/auth/admin", "admin-auth")
ADMIN = RouterMount(admin_router, "/admin", "admin")

MOUNTS: tuple[RouterMount, ...] = (
    *COMMON_MOUNTS,
    ADMIN_AUTH,
    ADMIN,
    IAM,
    AUDIT,
    RouterMount(superadmin_auth_router, "/auth/superadmin", "superadmin-auth"),
    RouterMount(superadmin_protected_router, "/superadmin", "superadmin"),
    RouterMount(superadmin_ops_router, "/superadmin", "superadmin"),
)

# SQLAdmin bypasses service-layer MFA, justification, and immutable audit staging.
DEVELOPMENT_TOOLS: tuple[Callable[[FastAPI], None], ...] = ()
