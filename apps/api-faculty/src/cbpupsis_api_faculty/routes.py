"""The routers the faculty app serves.

Only the shared ones until the first faculty feature lands; a faculty domain
goes in ``cbpupsis_api_faculty/domains/<name>/`` and is added to ``MOUNTS``.
"""

from __future__ import annotations

from cbpupsis_shared.application import RouterMount
from cbpupsis_shared.routes import COMMON_MOUNTS

MOUNTS: tuple[RouterMount, ...] = COMMON_MOUNTS
