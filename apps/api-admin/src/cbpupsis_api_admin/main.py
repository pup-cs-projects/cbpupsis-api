"""Entry point for running the admin app as its own service.

``uvicorn cbpupsis_api_admin.main:app``. The composed process in the repository
root's ``main.py`` serves these same routes alongside the other apps'.
"""

from __future__ import annotations

from cbpupsis_api_admin.routes import DEVELOPMENT_TOOLS, MOUNTS
from cbpupsis_core.config import settings
from cbpupsis_shared.application import create_app

app = create_app(
    title=f"{settings.app_name} (admin)",
    mounts=MOUNTS,
    development_tools=DEVELOPMENT_TOOLS,
)
