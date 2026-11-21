"""Superadmin challenges, bound refresh sessions, and restricted runtime ACLs.

Revision ID: c004scratch
Revises: b005reset
"""

import sqlalchemy as sa
from alembic import op

from cbpupsis_core.config import settings

revision = "c004scratch"
down_revision = "b005reset"
branch_labels = None
depends_on = None


def _configure_runtime_acl() -> None:
    bind = op.get_bind()
    role = (settings.audit_runtime_role or "").strip()
    if not role:
        raise RuntimeError("AUDIT_RUNTIME_ROLE must identify a restricted runtime role")
    role_info = bind.execute(
        sa.text("""
            SELECT oid, rolsuper, rolcreaterole, rolcreatedb, rolbypassrls, rolreplication
            FROM pg_roles WHERE rolname = :role
        """), {"role": role},
    ).mappings().one_or_none()
    if role_info is None or any(role_info[key] for key in (
        "rolsuper", "rolcreaterole", "rolcreatedb", "rolbypassrls", "rolreplication"
    )):
        raise RuntimeError("Runtime role is missing or has administrative attributes")
    unsafe = bind.scalar(sa.text("""
        SELECT pg_has_role(:role, current_user, 'MEMBER')
          OR EXISTS (
            SELECT 1 FROM pg_database d WHERE d.datname = current_database()
              AND pg_has_role(:role, d.datdba, 'MEMBER')
          )
          OR EXISTS (
            SELECT 1 FROM pg_namespace n WHERE n.nspname = 'public'
              AND pg_has_role(:role, n.nspowner, 'MEMBER')
          )
          OR EXISTS (
            SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p', 'S')
              AND pg_has_role(:role, c.relowner, 'MEMBER')
          )
          OR EXISTS (
            SELECT 1 FROM pg_roles r WHERE pg_has_role(:role, r.oid, 'MEMBER')
              AND (r.rolsuper OR r.rolcreaterole OR r.rolcreatedb
                   OR r.rolbypassrls OR r.rolreplication)
          )
    """), {"role": role})
    if unsafe:
        raise RuntimeError("Runtime role must not own tables or reach an owner/admin role")
    quoted = bind.dialect.identifier_preparer.quote_identifier(role)
    op.execute(f"GRANT USAGE ON SCHEMA public TO {quoted}")
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {quoted}")
    op.execute(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {quoted}")
    # Future migrations grant ordinary tables explicitly, never blanket defaults
    # that silently permit mutation of a future ledger partition.
    relations = bind.execute(sa.text("""
        WITH RECURSIVE protected(oid, metadata) AS (
            SELECT c.oid, c.relname = 'alembic_version'
            FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'public'
              AND c.relname IN ('audit_entries', 'admin_audit_trails', 'alembic_version')
            UNION ALL
            SELECT i.inhrelid, p.metadata
            FROM pg_inherits i JOIN protected p ON i.inhparent = p.oid
        )
        SELECT DISTINCT c.oid, n.nspname, c.relname, p.metadata
        FROM protected p JOIN pg_class c ON c.oid = p.oid
        JOIN pg_namespace n ON n.oid = c.relnamespace
    """)).mappings().all()
    for relation in relations:
        table = ".".join(bind.dialect.identifier_preparer.quote_identifier(relation[k])
                         for k in ("nspname", "relname"))
        forbidden = "INSERT, UPDATE, DELETE, TRUNCATE" if relation["metadata"] else "UPDATE, DELETE, TRUNCATE"
        op.execute(f"REVOKE {forbidden} ON TABLE {table} FROM PUBLIC, {quoted}")
        allowed = "SELECT" if relation["metadata"] else "SELECT, INSERT"
        op.execute(f"GRANT {allowed} ON TABLE {table} TO {quoted}")
        # Include NOINHERIT memberships: SET ROLE must not bypass the ledger.
        for privilege in forbidden.split(", "):
            if bind.scalar(sa.text("""
                SELECT EXISTS (
                    SELECT 1 FROM pg_roles r
                    WHERE pg_has_role(:role, r.oid, 'MEMBER')
                      AND has_table_privilege(r.oid, CAST(:oid AS oid), :privilege)
                )
            """), {"role": role, "oid": relation["oid"], "privilege": privilege}):
                raise RuntimeError("Runtime role has an inherited protected-table write grant")


def upgrade() -> None:
    op.create_table(
        "superadmin_challenges",
        sa.Column("user_id", sa.Uuid(), primary_key=True),
        sa.Column("active_jti", sa.String(36), nullable=False),
    )
    op.add_column("refresh_tokens", sa.Column("session_id", sa.Uuid(), nullable=True))
    op.create_index("ix_refresh_tokens_session_id", "refresh_tokens", ["session_id"])
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("""
        CREATE TRIGGER admin_audit_trails_append_only
        BEFORE UPDATE OR DELETE ON admin_audit_trails
        FOR EACH ROW EXECUTE FUNCTION refuse_audit_entry_mutation()
    """)
    _configure_runtime_acl()


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP TRIGGER IF EXISTS admin_audit_trails_append_only ON admin_audit_trails")
    op.drop_index("ix_refresh_tokens_session_id", table_name="refresh_tokens")
    op.drop_column("refresh_tokens", "session_id")
    op.drop_table("superadmin_challenges")
    # Intentionally never weaken audit ACLs during a downgrade.
