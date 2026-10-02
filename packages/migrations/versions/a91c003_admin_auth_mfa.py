"""Admin authentication, MFA challenge state, lockout, and immutable audit

Revision ID: a91c003admin
Revises: 092426_create_initial_schema
Create Date: 2026-09-28 00:00:00.000000
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "a91c003admin"
down_revision = "092426_create_initial_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Earlier template seeds attached user/IAM/audit administration to the
    # Admins group. Product ownership now assigns those capabilities to the
    # separate Superadmin role, so remove only those group-level seed grants.
    op.execute(
        """
        DELETE FROM group_policies
        WHERE group_id = (SELECT id FROM groups WHERE name = 'Admins')
          AND policy_id IN (
              SELECT id FROM policies
              WHERE name IN ('ItemAuthor', 'ItemModerator', 'UserAdmin',
                             'IAMAdmin', 'AuditReader', 'NotificationAdmin')
          )
        """
    )
    op.add_column(
        "admin_profiles",
        sa.Column("active_mfa_challenge_jti", sa.String(length=36), nullable=True),
    )
    op.drop_index("ix_user_mfa_credential_id", table_name="user_mfa_credentials")
    op.alter_column(
        "user_mfa_credentials",
        "credential_id",
        existing_type=sa.String(length=255),
        type_=sa.Text(),
        existing_nullable=True,
    )
    op.add_column(
        "user_mfa_credentials",
        sa.Column("credential_id_digest", sa.String(length=64), nullable=True),
    )
    op.create_index(
        "ix_user_mfa_credential_id_digest",
        "user_mfa_credentials",
        ["credential_id_digest"],
        unique=True,
        postgresql_where=sa.text("credential_id_digest IS NOT NULL"),
    )

    op.create_table(
        "authentication_failures",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column(
            "step",
            sa.Enum(
                "credentials",
                "mfa",
                name="ck_authentication_failures_step",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_authentication_failures_user_id"),
        "authentication_failures",
        ["user_id"],
    )
    op.create_index(
        op.f("ix_authentication_failures_occurred_at"),
        "authentication_failures",
        ["occurred_at"],
    )

    op.create_table(
        "authentication_lockouts",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("user_id"),
    )
    op.create_index(
        op.f("ix_authentication_lockouts_locked_until"),
        "authentication_lockouts",
        ["locked_until"],
    )

    op.add_column(
        "audit_entries",
        sa.Column(
            "prior_state",
            postgresql.JSONB(astext_type=sa.Text()).with_variant(sa.JSON(), "sqlite"),
            nullable=True,
        ),
    )
    op.add_column(
        "audit_entries",
        sa.Column(
            "new_state",
            postgresql.JSONB(astext_type=sa.Text()).with_variant(sa.JSON(), "sqlite"),
            nullable=True,
        ),
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION refuse_audit_entry_mutation()
        RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'audit entries are append-only'
                USING ERRCODE = '42501';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER audit_entries_append_only
        BEFORE UPDATE OR DELETE ON audit_entries
        FOR EACH ROW EXECUTE FUNCTION refuse_audit_entry_mutation();
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS audit_entries_append_only ON audit_entries")
    op.execute("DROP FUNCTION IF EXISTS refuse_audit_entry_mutation()")
    op.drop_column("audit_entries", "new_state")
    op.drop_column("audit_entries", "prior_state")

    op.drop_index(
        op.f("ix_authentication_lockouts_locked_until"),
        table_name="authentication_lockouts",
    )
    op.drop_table("authentication_lockouts")
    op.drop_index(
        op.f("ix_authentication_failures_occurred_at"),
        table_name="authentication_failures",
    )
    op.drop_index(
        op.f("ix_authentication_failures_user_id"),
        table_name="authentication_failures",
    )
    op.drop_table("authentication_failures")
    op.drop_index(
        "ix_user_mfa_credential_id_digest",
        table_name="user_mfa_credentials",
    )
    op.drop_column("user_mfa_credentials", "credential_id_digest")
    op.alter_column(
        "user_mfa_credentials",
        "credential_id",
        existing_type=sa.Text(),
        type_=sa.String(length=255),
        existing_nullable=True,
    )
    op.create_index(
        "ix_user_mfa_credential_id",
        "user_mfa_credentials",
        ["credential_id"],
        unique=True,
        postgresql_where=sa.text("credential_id IS NOT NULL"),
    )
    op.drop_column("admin_profiles", "active_mfa_challenge_jti")
    op.execute(
        """
        INSERT INTO group_policies (group_id, policy_id)
        SELECT groups.id, policies.id
        FROM groups
        CROSS JOIN policies
        WHERE groups.name = 'Admins'
          AND policies.name IN ('ItemAuthor', 'ItemModerator', 'UserAdmin',
                                'IAMAdmin', 'AuditReader', 'NotificationAdmin')
        ON CONFLICT DO NOTHING
        """
    )
