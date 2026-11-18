"""The routers the admin app serves, and its development-only admin panel.

IAM, audit, and user management are shared code, because every app enforces
permissions, records audited events, and serves each user's own account, but
administering them is this app's job alone: the student and faculty apps mount
none of it.
"""

from __future__ import annotations

from collections.abc import Callable

from fastapi import FastAPI

from cbpupsis_api_admin.domains.add_drop_requests.router import (
    router as add_drop_requests_router,
)
from cbpupsis_api_admin.domains.admin_auth.dependencies import require_waiver_reviewer
from cbpupsis_api_admin.domains.admin_auth.router import (
    protected_router as admin_router,
)
from cbpupsis_api_admin.domains.admin_auth.router import router as admin_auth_router
from cbpupsis_api_admin.domains.admin_auth.router import (
    superadmin_auth_router,
    superadmin_protected_router,
)
from cbpupsis_api_admin.domains.admin_catalog.router import (
    router as admin_catalog_router,
)
from cbpupsis_api_admin.domains.fee_structures.router import (
    router as fee_structures_router,
)
from cbpupsis_api_admin.domains.grade_corrections.router import (
    router as grade_corrections_router,
)
from cbpupsis_api_admin.domains.overload_requests.router import (
    router as overload_requests_router,
)
from cbpupsis_api_admin.domains.superadmin_ops.router import (
    router as superadmin_ops_router,
)
from cbpupsis_api_admin.domains.user_import.router import (
    router as user_import_router,
)
from cbpupsis_shared.application import RouterMount
from cbpupsis_shared.domains.enrollment.constants import (
    APPROVE_WAIVER_REQUEST,
    READ_WAIVER_REQUEST,
)
from cbpupsis_shared.domains.enrollment.router import build_admin_router
from cbpupsis_shared.routes import (
    AUDIT,
    COMMON_MOUNTS,
    IAM,
    REPORTS,
    USER_MANAGEMENT,
)

ADMIN_AUTH = RouterMount(admin_auth_router, "/auth/admin", "admin-auth")
ADMIN = RouterMount(admin_router, "/admin", "admin")
ADMIN_CATALOG = RouterMount(admin_catalog_router, "/admin/catalog", "admin-catalog")

MOUNTS: tuple[RouterMount, ...] = (
    *COMMON_MOUNTS,
    ADMIN_AUTH,
    ADMIN,
    REPORTS,
    RouterMount(
        overload_requests_router,
        "/admin/overload-requests",
        "admin-overload-requests",
    ),
    RouterMount(
        add_drop_requests_router,
        "/admin/add-drop",
        "admin-add-drop",
    ),
    RouterMount(
        fee_structures_router,
        "/admin/fee-structures",
        "admin-fee-structures",
    ),
    IAM,
    AUDIT,
    # After COMMON_MOUNTS, so /users/me resolves before /users/{user_id}.
    USER_MANAGEMENT,
    RouterMount(superadmin_auth_router, "/auth/superadmin", "superadmin-auth"),
    RouterMount(superadmin_protected_router, "/superadmin", "superadmin"),
    RouterMount(superadmin_ops_router, "/superadmin", "superadmin"),
    # Beside the shared users router, at the same prefix: only this app parses
    # multipart uploads.
    RouterMount(user_import_router, "/admin/users", "users"),
    RouterMount(
        build_admin_router(
            require_waiver_reviewer(READ_WAIVER_REQUEST),
            require_waiver_reviewer(APPROVE_WAIVER_REQUEST),
        ),
        "/admin/waiver-requests",
        "waiver-requests",
    ),
    ADMIN_CATALOG,
    RouterMount(
        grade_corrections_router, "/admin/grade-corrections", "grade-corrections"
    ),
)

# SQLAdmin bypasses service-layer MFA, justification, and immutable audit staging.
DEVELOPMENT_TOOLS: tuple[Callable[[FastAPI], None], ...] = ()
