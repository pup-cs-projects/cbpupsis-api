"""Router for the enrollment domain."""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/enrollment", tags=["enrollment"])
