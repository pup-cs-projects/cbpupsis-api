"""Remove runtime write access to Alembic revision metadata.

Revision ID: d11alembicacl
Revises: c10authmerge
"""

import sqlalchemy as sa
from alembic import op

from cbpupsis_core.config import settings

revision = "d11alembicacl"
down_revision = "c10authmerge"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    role = settings.audit_runtime_role
    if not role:
        raise RuntimeError("AUDIT_RUNTIME_ROLE is required for PostgreSQL migrations")
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

    owner = bind.execute(
        sa.text(
            "SELECT pg_get_userbyid(relowner) FROM pg_class "
            "WHERE oid = 'public.alembic_version'::regclass"
        )
    ).scalar_one()
    if (
        role == owner
        or bind.execute(
            sa.text("SELECT pg_has_role(:role, :owner, 'member')"),
            {"role": role, "owner": owner},
        ).scalar_one()
    ):
        raise RuntimeError("The API role must not own or join the Alembic owner role")
    if bind.execute(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM pg_roles "
            "WHERE rolsuper AND pg_has_role(:role, rolname, 'member'))"
        ),
        {"role": role},
    ).scalar_one():
        raise RuntimeError("The API role must not join a superuser role")

    quoted_role = bind.dialect.identifier_preparer.quote(role)
    op.execute(
        "REVOKE INSERT, UPDATE, DELETE, TRUNCATE "
        f"ON TABLE public.alembic_version FROM {quoted_role}"
    )
    op.execute(
        "REVOKE INSERT, UPDATE, DELETE, TRUNCATE "
        "ON TABLE public.alembic_version FROM PUBLIC"
    )

    write_privileges = bind.execute(
        sa.text(
            "SELECT has_table_privilege(:role, 'public.alembic_version', 'INSERT'), "
            "has_table_privilege(:role, 'public.alembic_version', 'UPDATE'), "
            "has_table_privilege(:role, 'public.alembic_version', 'DELETE'), "
            "has_table_privilege(:role, 'public.alembic_version', 'TRUNCATE')"
        ),
        {"role": role},
    ).one()
    if any(write_privileges):
        raise RuntimeError("The API role still has effective Alembic write access")

    inherited_or_public_write = bind.execute(
        sa.text(
            "SELECT EXISTS ("
            "  SELECT 1 FROM pg_class c "
            "  CROSS JOIN LATERAL aclexplode("
            "    COALESCE(c.relacl, acldefault('r', c.relowner))"
            "  ) acl "
            "  LEFT JOIN pg_roles grantee ON grantee.oid = acl.grantee "
            "  WHERE c.oid = 'public.alembic_version'::regclass "
            "    AND acl.privilege_type IN ('INSERT', 'UPDATE', 'DELETE', 'TRUNCATE') "
            "    AND (acl.grantee = 0 OR (grantee.oid IS NOT NULL "
            "      AND pg_has_role(:role, grantee.rolname, 'member')))"
            ")"
        ),
        {"role": role},
    ).scalar_one()
    if inherited_or_public_write:
        raise RuntimeError("PUBLIC or a settable role still grants Alembic writes")


def downgrade() -> None:
    # Do not restore insecure metadata grants when reverting application code.
    pass
