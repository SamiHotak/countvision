"""Dashboards and reports: visitor lines per site, report schedules, time indexes.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-10

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '0004'
down_revision: str | Sequence[str] | None = '0003'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('sites', sa.Column('visitor_lines', postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.create_table(
        'report_schedules',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('org_id', sa.Uuid(), nullable=False),
        sa.Column('site_id', sa.Uuid(), nullable=False),
        sa.Column('frequency', sa.String(length=8), nullable=False),
        sa.Column('weekday', sa.Integer(), server_default=sa.text('0'), nullable=False),
        sa.Column('hour', sa.Integer(), server_default=sa.text('7'), nullable=False),
        sa.Column('recipients', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column('attach_csv', sa.Boolean(), server_default=sa.text('false'), nullable=False),
        sa.Column('enabled', sa.Boolean(), server_default=sa.text('true'), nullable=False),
        sa.Column('created_by', sa.Uuid(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('last_sent_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_period_end', sa.Date(), nullable=True),
        sa.Column('last_error', sa.String(length=300), nullable=True),
        sa.Column('failures', sa.Integer(), server_default=sa.text('0'), nullable=False),
        sa.ForeignKeyConstraint(['created_by'], ['users.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['org_id'], ['organizations.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['site_id'], ['sites.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_report_schedules_org_id'), 'report_schedules', ['org_id'], unique=False)
    op.create_index(op.f('ix_report_schedules_site_id'), 'report_schedules', ['site_id'], unique=False)
    # Dashboards read "all rows of these cameras between two times". The primary keys start
    # with camera + line/zone, so they do not help for that; these indexes do.
    op.create_index('ix_line_counts_camera_ws', 'line_counts', ['camera_id', 'window_start'], unique=False)
    op.create_index('ix_zone_stats_camera_ws', 'zone_stats', ['camera_id', 'window_start'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_zone_stats_camera_ws', table_name='zone_stats')
    op.drop_index('ix_line_counts_camera_ws', table_name='line_counts')
    op.drop_index(op.f('ix_report_schedules_site_id'), table_name='report_schedules')
    op.drop_index(op.f('ix_report_schedules_org_id'), table_name='report_schedules')
    op.drop_table('report_schedules')
    op.drop_column('sites', 'visitor_lines')
