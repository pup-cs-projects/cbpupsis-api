"""No shared section HTTP routes; the faculty facade owns its endpoints."""

from fastapi import APIRouter

router = APIRouter()
