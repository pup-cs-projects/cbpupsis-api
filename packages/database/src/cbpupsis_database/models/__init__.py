"""Every table in the workspace, one module per owning domain.

Importing this package registers all of them on ``Base.metadata``, which is what
Alembic autogenerate and the test suite's ``create_all`` read. A model module
missing from this list is a table autogenerate silently skips, so
``tests/core/test_architecture.py`` checks the list against the directory.

Domains import only their own module (``cbpupsis_database.models.users``), never
this package as a whole: centralising the tables does not make another domain's
tables public.
"""

from __future__ import annotations

from cbpupsis_database.models import (
    academics,
    admin_auth,
    announcements,
    audit,
    auth,
    billing,
    curriculum,
    enrollment,
    evaluations,
    faculty_auth,
    grading,
    iam,
    items,
    notifications,
    outbox,
    sections,
    superadmin_ops,
    system,
    users,
)

__all__ = [
    "academics",
    "admin_auth",
    "announcements",
    "audit",
    "auth",
    "billing",
    "curriculum",
    "enrollment",
    "evaluations",
    "faculty_auth",
    "grading",
    "iam",
    "items",
    "notifications",
    "outbox",
    "sections",
    "superadmin_ops",
    "system",
    "users",
]
