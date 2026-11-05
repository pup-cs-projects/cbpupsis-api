"""Immutable encrypted CORs and reproducible assessment inputs.

Revision ID: e100_cor_assessment
Revises: e170_program_college
"""

import sqlalchemy as sa
from alembic import op

revision = "e100_cor_assessment"
down_revision = "e170_program_college"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    # Lock out concurrent old writers between inspection and index creation.
    op.execute("LOCK TABLE cor_issuances IN ACCESS EXCLUSIVE MODE")
    duplicates = bind.scalar(sa.text("""
        SELECT EXISTS (SELECT 1 FROM cor_issuances WHERE status = 'CURRENT'
        GROUP BY term_enrollment_id HAVING count(*) > 1)
    """))
    if duplicates:
        raise RuntimeError("Duplicate current CORs require owner reconciliation before migration")
    # Legacy rows deliberately remain NULL: neither the old raw token nor the
    # historical certificate content can safely be invented by a migration.
    op.add_column("cor_issuances", sa.Column("token_envelope", sa.Text(), nullable=True))
    op.add_column("cor_issuances", sa.Column("snapshot_envelope", sa.Text(), nullable=True))
    op.add_column("student_billing_accounts", sa.Column("assessment_snapshot", sa.JSON(), nullable=True))
    op.create_index("uq_cor_current_enrollment", "cor_issuances", ["term_enrollment_id"], unique=True, postgresql_where=sa.text("status = 'CURRENT'"))
    op.execute("""
        CREATE FUNCTION protect_cor_content() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF ROW(NEW.id, NEW.student_id, NEW.term_enrollment_id,
                 NEW.verification_token_hash, NEW.token_envelope,
                 NEW.snapshot_envelope, NEW.issued_at)
             IS DISTINCT FROM
             ROW(OLD.id, OLD.student_id, OLD.term_enrollment_id,
                 OLD.verification_token_hash, OLD.token_envelope,
                 OLD.snapshot_envelope, OLD.issued_at) THEN
            RAISE EXCEPTION 'COR content is immutable';
          END IF;
          IF OLD.status <> 'CURRENT' OR NEW.status <> 'SUPERSEDED'
             OR OLD.superseded_at IS NOT NULL OR NEW.superseded_at IS NULL THEN
            RAISE EXCEPTION 'Invalid COR history transition';
          END IF;
          RETURN NEW;
        END $$
    """)
    op.execute("CREATE TRIGGER cor_content_immutable BEFORE UPDATE ON cor_issuances FOR EACH ROW EXECUTE FUNCTION protect_cor_content()")
    # Existing table grants cover new columns. No new tables, roles, broad
    # default privileges or elevated runtime grants are introduced here.


def downgrade() -> None:
    op.execute("DROP TRIGGER cor_content_immutable ON cor_issuances")
    op.execute("DROP FUNCTION protect_cor_content()")
    op.drop_index("uq_cor_current_enrollment", table_name="cor_issuances")
    op.drop_column("student_billing_accounts", "assessment_snapshot")
    op.drop_column("cor_issuances", "snapshot_envelope")
    op.drop_column("cor_issuances", "token_envelope")
