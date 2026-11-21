"""HTTP layer for the curriculum domain.

No endpoints yet, and no app mounts this router: the domain serves other
domains through ``client.py`` until the curriculum epic adds its own routes.
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter()
