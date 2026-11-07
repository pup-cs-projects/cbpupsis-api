"""Grant runtime roles the required student holds privileges.

Revision ID: c189_student_holds_acl
Revises: c188_student_holds
"""

import sqlalchemy as sa
from alembic import op

revision = "c189_student_holds_acl"
down_revision = "c188_student_holds"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    roles = bind.scalars(
        sa.text("""
        SELECT grantee FROM information_schema.role_table_grants
        WHERE table_schema = 'public'
          AND table_name = 'student_billing_accounts'
          AND privilege_type IN ('SELECT', 'INSERT', 'UPDATE', 'DELETE')
          AND grantee <> 'PUBLIC'
        GROUP BY grantee HAVING count(DISTINCT privilege_type) = 4
    """)
    )
    for role in roles:
        quoted = bind.dialect.identifier_preparer.quote_identifier(role)
        op.execute(
            f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE student_holds TO {quoted}"
        )


def downgrade() -> None:
    bind = op.get_bind()
    roles = bind.scalars(
        sa.text("""
        SELECT grantee FROM information_schema.role_table_grants
        WHERE table_schema = 'public'
          AND table_name = 'student_billing_accounts'
          AND privilege_type IN ('SELECT', 'INSERT', 'UPDATE', 'DELETE')
          AND grantee <> 'PUBLIC'
        GROUP BY grantee HAVING count(DISTINCT privilege_type) = 4
    """)
    )
    for role in roles:
        quoted = bind.dialect.identifier_preparer.quote_identifier(role)
        op.execute(
            f"REVOKE INSERT, UPDATE, DELETE ON TABLE student_holds FROM {quoted}"
        )
