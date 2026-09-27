"""administrative MFA, position scope, and shared lockout

Revision ID: 9d2e4a1c7b63
Revises: e5df35334433
Create Date: 2026-09-27 00:00:00.000000
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "9d2e4a1c7b63"
down_revision = "e5df35334433"
branch_labels = None
depends_on = None


def upgrade() -> None:
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
    op.create_table(
        "admin_auth_profiles",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column(
            "position",
            sa.Enum(
                "chairperson",
                "dean",
                "registrar",
                name="ck_admin_auth_profiles_position",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("department_id", sa.String(length=100), nullable=True),
        sa.Column("college_id", sa.String(length=100), nullable=True),
        sa.Column("active_mfa_challenge_jti", sa.String(length=36), nullable=True),
        sa.Column("totp_secret_encrypted", sa.String(length=512), nullable=True),
        sa.Column("totp_confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("totp_last_used_counter", sa.Integer(), nullable=True),
        sa.Column(
            "webauthn_credential_id_hash", sa.String(length=64), nullable=True
        ),
        sa.Column(
            "webauthn_credential_id_encrypted",
            sa.String(length=2048),
            nullable=True,
        ),
        sa.Column("webauthn_public_key_encrypted", sa.Text(), nullable=True),
        sa.Column("webauthn_sign_count", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("user_id"),
    )
    op.create_index(
        op.f("ix_admin_auth_profiles_webauthn_credential_id_hash"),
        "admin_auth_profiles",
        ["webauthn_credential_id_hash"],
        unique=True,
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
        unique=False,
    )
    op.create_index(
        op.f("ix_authentication_failures_occurred_at"),
        "authentication_failures",
        ["occurred_at"],
        unique=False,
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
        unique=False,
    )


def downgrade() -> None:
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
        op.f("ix_admin_auth_profiles_webauthn_credential_id_hash"),
        table_name="admin_auth_profiles",
    )
    op.drop_table("admin_auth_profiles")
    op.execute("DROP TRIGGER IF EXISTS audit_entries_append_only ON audit_entries")
    op.execute("DROP FUNCTION IF EXISTS refuse_audit_entry_mutation()")
    op.drop_column("audit_entries", "new_state")
    op.drop_column("audit_entries", "prior_state")
