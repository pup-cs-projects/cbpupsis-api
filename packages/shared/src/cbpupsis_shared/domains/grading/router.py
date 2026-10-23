"""Router for the grading domain."""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/grading", tags=["grading"])
