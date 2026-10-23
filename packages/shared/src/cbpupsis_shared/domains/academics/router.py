"""Router for the academics domain."""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/academics", tags=["academics"])
