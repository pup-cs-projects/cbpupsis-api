"""The routers the admin app serves, and its development-only admin panel.

IAM and audit are shared code, because every app enforces permissions and
records audited events, but administering them is this app's job alone.
"""

from __future__ import annotations

from collections.abc import Callable

from fastapi import FastAPI

from cbpupsis_api_admin.admin import setup_admin
from cbpupsis_shared.application import RouterMount
from cbpupsis_shared.routes import AUDIT, COMMON_MOUNTS, IAM

MOUNTS: tuple[RouterMount, ...] = (*COMMON_MOUNTS, IAM, AUDIT)

DEVELOPMENT_TOOLS: tuple[Callable[[FastAPI], None], ...] = (setup_admin,)
