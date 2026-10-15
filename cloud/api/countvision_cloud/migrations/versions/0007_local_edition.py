"""Local edition (CountVision Local): installation settings, cameras managed in the web app.

* app_settings: key -> JSON (licence, install date, highest clock time seen)
* devices.manages_cameras / devices.builtin
* cameras.source_kind / source_secret (encrypted address) / source_label / removed_at

All additive: the cloud edition is unchanged (new columns have defaults or are empty).

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-14

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '0007'
down_revision: str | Sequence[str] | None = '0006'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('app_settings',
    sa.Column('key', sa.String(length=64), nullable=False),
    sa.Column('value', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('key', name=op.f('pk_app_settings'))
    )
    op.add_column('devices', sa.Column('manages_cameras', sa.Boolean(), server_default=sa.text('false'), nullable=False))
    op.add_column('devices', sa.Column('builtin', sa.Boolean(), server_default=sa.text('false'), nullable=False))
    op.add_column('cameras', sa.Column('source_kind', sa.String(length=16), nullable=True))
    op.add_column('cameras', sa.Column('source_secret', sa.Text(), nullable=True))
    op.add_column('cameras', sa.Column('source_label', sa.String(length=300), nullable=True))
    op.add_column('cameras', sa.Column('removed_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('cameras', 'removed_at')
    op.drop_column('cameras', 'source_label')
    op.drop_column('cameras', 'source_secret')
    op.drop_column('cameras', 'source_kind')
    op.drop_column('devices', 'builtin')
    op.drop_column('devices', 'manages_cameras')
    op.drop_table('app_settings')
