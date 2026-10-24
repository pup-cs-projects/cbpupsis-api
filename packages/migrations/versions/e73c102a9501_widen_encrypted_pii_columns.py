"""widen encrypted pii columns to text

Revision ID: e73c102a9501
Revises: 180508baf0e2
Create Date: 2026-10-24 00:45:00.000000

Issue #73 / #95 requires AES-256-GCM encryption at rest on stored birthdate
and contact fields. The ciphertext envelopes (enc:v1:...) exceed the legacy
VARCHAR(25) / DATE column limits, so widen them to Text in PostgreSQL.
"""

from alembic import op
import sqlalchemy as sa


revision = "e73c102a9501"
down_revision = "180508baf0e2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "user_profiles",
        "phone_number",
        existing_type=sa.String(length=25),
        type_=sa.Text(),
        existing_nullable=True,
    )
    op.alter_column(
        "user_profiles",
        "birthdate",
        existing_type=sa.Date(),
        type_=sa.Text(),
        existing_nullable=True,
        postgresql_using="birthdate::text",
    )
    op.alter_column(
        "user_profiles",
        "personal_email",
        existing_type=sa.String(length=255),
        type_=sa.Text(),
        existing_nullable=True,
    )
    op.alter_column(
        "emergency_contacts",
        "phone_number",
        existing_type=sa.String(length=25),
        type_=sa.Text(),
        existing_nullable=False,
    )
    op.alter_column(
        "emergency_contacts",
        "email",
        existing_type=sa.String(length=255),
        type_=sa.Text(),
        existing_nullable=True,
    )


def downgrade() -> None:
    op.alter_column(
        "emergency_contacts",
        "email",
        existing_type=sa.Text(),
        type_=sa.String(length=255),
        existing_nullable=True,
    )
    op.alter_column(
        "emergency_contacts",
        "phone_number",
        existing_type=sa.Text(),
        type_=sa.String(length=25),
        existing_nullable=False,
    )
    op.alter_column(
        "user_profiles",
        "personal_email",
        existing_type=sa.Text(),
        type_=sa.String(length=255),
        existing_nullable=True,
    )
    op.alter_column(
        "user_profiles",
        "birthdate",
        existing_type=sa.Text(),
        type_=sa.Date(),
        existing_nullable=True,
        postgresql_using="birthdate::date",
    )
    op.alter_column(
        "user_profiles",
        "phone_number",
        existing_type=sa.Text(),
        type_=sa.String(length=25),
        existing_nullable=True,
    )
