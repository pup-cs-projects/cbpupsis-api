"""Merge password-reset and Superadmin migration branches.

Revision ID: c10authmerge
Revises: b005reset, b70c004
"""

import sqlalchemy as sa
from alembic import op

from cbpupsis_core.config import settings

revision = "c10authmerge"
down_revision = ("b005reset", "b70c004")
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    role = settings.audit_runtime_role
    if not role:
        if settings.environment != "development":
            raise RuntimeError("AUDIT_RUNTIME_ROLE is required outside development")
        return
    bind = op.get_bind()
    owner = bind.execute(sa.text("SELECT current_user")).scalar_one()
    if role == owner:
        raise RuntimeError("The API database role must not be the migration owner")
    runtime = bind.execute(
        sa.text(
            "SELECT rolcanlogin, rolsuper, rolcreaterole "
            "FROM pg_roles WHERE rolname = :role"
        ),
        {"role": role},
    ).one_or_none()
    if (
        runtime is None
        or not runtime.rolcanlogin
        or runtime.rolsuper
        or runtime.rolcreaterole
    ):
        raise RuntimeError("AUDIT_RUNTIME_ROLE must name a restricted login role")
    if bind.execute(
        sa.text("SELECT pg_has_role(:role, current_user, 'member')"), {"role": role}
    ).scalar_one():
        raise RuntimeError("The API role must not inherit migration-owner privileges")
    quoted_role = bind.dialect.identifier_preparer.quote(role)
    # b005reset may run after b70c004 on databases already stamped b70c004.
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE "
        f"ON password_reset_throttles TO {quoted_role}"
    )


def downgrade() -> None:
    pass
