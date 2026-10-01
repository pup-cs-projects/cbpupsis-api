"""The routers the student app serves.

``items`` is the template's reference domain, kept here as the worked example of
an app-owned domain until the first student feature replaces it.
"""

from __future__ import annotations

from cbpupsis_api_student.auth.router import router as student_auth_router
from cbpupsis_api_student.domains.items.router import router as items_router
from cbpupsis_shared.application import RouterMount
from cbpupsis_shared.routes import NOTIFICATIONS, USERS

AUTH = RouterMount(student_auth_router, "/auth", "auth")
ITEMS = RouterMount(items_router, "/items", "items")

MOUNTS: tuple[RouterMount, ...] = (AUTH, USERS, NOTIFICATIONS, ITEMS)
