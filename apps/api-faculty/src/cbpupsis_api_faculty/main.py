"""Entry point for running the faculty app as its own service.

``uvicorn cbpupsis_api_faculty.main:app``. The composed process in the repository
root's ``main.py`` serves these same routes alongside the other apps'.
"""

from __future__ import annotations

from cbpupsis_api_faculty.routes import MOUNTS
from cbpupsis_core.config import settings
from cbpupsis_shared.application import create_app

app = create_app(
    title=f"{settings.app_name} (faculty)",
    mounts=MOUNTS,
)
