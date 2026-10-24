"""Router for the announcements domain."""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/announcements", tags=["announcements"])
