"""All three apps in one process: ``uvicorn main:app``.

This is what local compose, the Bruno suite, and the pytest suite run. It serves
the union of every app's routers at the same paths each app serves them at, so a
client cannot tell this process from the three apps behind one gateway. Whether
production runs this or the apps separately is an open deployment decision.

Lives outside ``apps/`` because it is the one module allowed to import every app.
"""

from __future__ import annotations

from cbpupsis_api_admin.routes import DEVELOPMENT_TOOLS
from cbpupsis_api_admin.routes import MOUNTS as ADMIN_MOUNTS
from cbpupsis_api_faculty.routes import MOUNTS as FACULTY_MOUNTS
from cbpupsis_api_student.routes import MOUNTS as STUDENT_MOUNTS
from cbpupsis_core.config import settings
from cbpupsis_shared.application import create_app

app = create_app(
    title=settings.app_name,
    mounts=(*STUDENT_MOUNTS, *FACULTY_MOUNTS, *ADMIN_MOUNTS),
    development_tools=DEVELOPMENT_TOOLS,
)
