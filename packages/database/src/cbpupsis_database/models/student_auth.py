"""Student-auth persistence façade.

The student-auth application domain needs the shared active-session and
authentication-audit tables. The table classes remain defined in their shared
model modules; this module only provides a stable import surface and does not
define or duplicate any tables.
"""

from __future__ import annotations

from cbpupsis_database.models.audit import AuthAuditLog
from cbpupsis_database.models.auth import UserActiveSession
from cbpupsis_database.models.users import StudentProfile, User, UserProfile

__all__ = [
    "AuthAuditLog",
    "StudentProfile",
    "User",
    "UserActiveSession",
    "UserProfile",
]
