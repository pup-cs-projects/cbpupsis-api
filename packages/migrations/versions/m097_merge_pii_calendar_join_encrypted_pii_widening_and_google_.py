"""join encrypted PII widening and Google Calendar heads

Revision ID: m097_merge_pii_calendar
Revises: e73c102a9501, g096_google_calendar
Create Date: 2026-10-24 09:43:39.364421
"""
from alembic import op
import sqlalchemy as sa


revision = 'm097_merge_pii_calendar'
down_revision = ('e73c102a9501', 'g096_google_calendar')
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
