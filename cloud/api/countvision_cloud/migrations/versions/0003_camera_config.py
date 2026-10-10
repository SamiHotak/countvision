"""Camera config: reported/desired config, versions, snapshot permission, frame size.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-09

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '0003'
down_revision: str | Sequence[str] | None = '0002'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('cameras', sa.Column('reported_config', postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.add_column('cameras', sa.Column('desired_config', postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.add_column('cameras', sa.Column('desired_version', sa.Integer(), server_default=sa.text('0'), nullable=False))
    op.add_column('cameras', sa.Column('applied_version', sa.Integer(), server_default=sa.text('0'), nullable=False))
    op.add_column('cameras', sa.Column('config_error', sa.String(length=300), nullable=True))
    op.add_column('cameras', sa.Column('snapshots_allowed', sa.Boolean(), nullable=True))
    op.add_column('cameras', sa.Column('frame_width', sa.Integer(), nullable=True))
    op.add_column('cameras', sa.Column('frame_height', sa.Integer(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('cameras', 'frame_height')
    op.drop_column('cameras', 'frame_width')
    op.drop_column('cameras', 'snapshots_allowed')
    op.drop_column('cameras', 'config_error')
    op.drop_column('cameras', 'applied_version')
    op.drop_column('cameras', 'desired_version')
    op.drop_column('cameras', 'desired_config')
    op.drop_column('cameras', 'reported_config')
