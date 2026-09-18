"""v1 API aggregator: the single place domain routers are mounted.

Adding a domain is one import plus one ``include_router`` line here; ``main.py``
never changes. When an incompatible API version is needed, add
``app/api/v2/router.py`` and mount it under ``/api/v2`` alongside this one,
leaving v1 and its schemas frozen.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.domains.audit.router import router as audit_router
from app.domains.auth.router import router as auth_router
from app.domains.iam.router import router as iam_router
from app.domains.items.router import router as items_router
from app.domains.notifications.router import router as notifications_router
from app.domains.users.router import router as users_router

v1_router = APIRouter()
v1_router.include_router(auth_router, prefix="/auth", tags=["auth"])
v1_router.include_router(users_router, prefix="/users", tags=["users"])
v1_router.include_router(iam_router, prefix="/iam", tags=["iam"])
v1_router.include_router(items_router, prefix="/items", tags=["items"])
v1_router.include_router(audit_router, prefix="/audit", tags=["audit"])
v1_router.include_router(
    notifications_router, prefix="/notifications", tags=["notifications"]
)
# add each new domain here
