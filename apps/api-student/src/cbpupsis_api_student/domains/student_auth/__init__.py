"""Student-specific login, lockout, audit, and idle-session domain."""

from __future__ import annotations

from cbpupsis_api_student.domains.student_auth.router import router

__all__ = ["router"]
