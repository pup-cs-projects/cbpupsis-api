"""Superadmin challenge state and runtime audit privileges.

Revision ID: b70c004
Revises: 99a37a906c0d
"""

import sqlalchemy as sa
from alembic import op

from cbpupsis_core.config import settings

revision = "b70c004"
down_revision = "99a37a906c0d"
branch_labels = None
depends_on = None


def _runtime_role_sql() -> str | None:
    role = settings.audit_runtime_role
    if not role:
        if settings.environment != "development":
            raise RuntimeError("AUDIT_RUNTIME_ROLE is required outside development")
        return None
    bind = op.get_bind()
    owner = bind.execute(sa.text("SELECT current_user")).scalar_one()
    if role == owner:
        raise RuntimeError("The API database role must not own the audit ledger")
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
    member = bind.execute(
        sa.text("SELECT pg_has_role(:role, current_user, 'member')"), {"role": role}
    ).scalar_one()
    if member:
        raise RuntimeError("The API role must not inherit migration-owner privileges")
    return bind.dialect.identifier_preparer.quote(role)


def upgrade() -> None:
    op.create_table(
        "superadmin_challenges",
        sa.Column("user_id", sa.Uuid(), primary_key=True),
        sa.Column("active_jti", sa.String(36), nullable=False),
    )
    if op.get_bind().dialect.name != "postgresql":
        return
    role = _runtime_role_sql()
    if role is None:
        return
    op.execute(f"GRANT USAGE ON SCHEMA public TO {role}")
    op.execute(
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {role}"
    )
    op.execute(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {role}")
    # Future migrations grant non-ledger tables explicitly. A blanket default
    # would silently give UPDATE/DELETE on future audit partitions.
    for table in ("audit_entries", "admin_audit_trails", "admin_audit_trails_default"):
        op.execute(f"REVOKE ALL ON {table} FROM {role}")
        op.execute(f"REVOKE ALL ON {table} FROM PUBLIC")
        op.execute(f"GRANT SELECT, INSERT ON {table} TO {role}")
    op.execute(
        """
        CREATE OR REPLACE FUNCTION refuse_admin_audit_trail_mutation()
        RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'admin audit trails are append-only'
                USING ERRCODE = '42501';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER admin_audit_trails_append_only
        BEFORE UPDATE OR DELETE ON admin_audit_trails
        FOR EACH ROW EXECUTE FUNCTION refuse_admin_audit_trail_mutation();
        """
    )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "DROP TRIGGER IF EXISTS admin_audit_trails_append_only "
            "ON admin_audit_trails"
        )
        op.execute("DROP FUNCTION IF EXISTS refuse_admin_audit_trail_mutation()")
    op.drop_table("superadmin_challenges")
