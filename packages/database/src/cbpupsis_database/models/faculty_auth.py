"""Persistence owned by or accessed by the Faculty authentication domain."""

from __future__ import annotations

from cbpupsis_database.models.audit import AuthAuditLog
from cbpupsis_database.models.auth import (
    IdempotencyKey,
    UserActiveSession,
    UserMfaCredential,
    UserMfaRecoveryCode,
)
from cbpupsis_database.models.sections import CourseSection
from cbpupsis_database.models.users import FacultyProfile, User, UserProfile

__all__ = [
    "AuthAuditLog",
    "CourseSection",
    "FacultyProfile",
    "IdempotencyKey",
    "User",
    "UserActiveSession",
    "UserMfaCredential",
    "UserMfaRecoveryCode",
    "UserProfile",
]
