"""Sites, devices, pairing codes, cameras and the counting data (hypertables with TimescaleDB).

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-08

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '0002'
down_revision: str | Sequence[str] | None = '0001'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('sites',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('org_id', sa.Uuid(), nullable=False),
    sa.Column('name', sa.String(length=120), nullable=False),
    sa.Column('timezone', sa.String(length=64), server_default=sa.text("'Europe/Berlin'"), nullable=False),
    sa.Column('address', sa.String(length=240), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['org_id'], ['organizations.id'], name=op.f('fk_sites_org_id_organizations'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_sites'))
    )
    op.create_index(op.f('ix_sites_org_id'), 'sites', ['org_id'], unique=False)
    op.create_table('devices',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('org_id', sa.Uuid(), nullable=False),
    sa.Column('site_id', sa.Uuid(), nullable=False),
    sa.Column('name', sa.String(length=120), nullable=False),
    sa.Column('edge_device_id', sa.String(length=64), nullable=False),
    sa.Column('token_hash', sa.String(length=64), nullable=False),
    sa.Column('agent_version', sa.String(length=32), nullable=True),
    sa.Column('status', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('paired_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('last_seen_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_data_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['org_id'], ['organizations.id'], name=op.f('fk_devices_org_id_organizations'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['site_id'], ['sites.id'], name=op.f('fk_devices_site_id_sites'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_devices')),
    sa.UniqueConstraint('token_hash', name=op.f('uq_devices_token_hash'))
    )
    op.create_index(op.f('ix_devices_org_id'), 'devices', ['org_id'], unique=False)
    op.create_index(op.f('ix_devices_site_id'), 'devices', ['site_id'], unique=False)
    op.create_table('cameras',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('org_id', sa.Uuid(), nullable=False),
    sa.Column('device_id', sa.Uuid(), nullable=False),
    sa.Column('edge_camera_id', sa.String(length=64), nullable=False),
    sa.Column('name', sa.String(length=120), nullable=False),
    sa.Column('name_locked', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('state', sa.String(length=16), nullable=True),
    sa.Column('connected', sa.Boolean(), nullable=True),
    sa.Column('fps', sa.Float(), nullable=True),
    sa.Column('reconnects', sa.Integer(), nullable=True),
    sa.Column('alert', sa.String(length=200), nullable=True),
    sa.Column('lines', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
    sa.Column('zones', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
    sa.Column('last_seen_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['device_id'], ['devices.id'], name=op.f('fk_cameras_device_id_devices'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['org_id'], ['organizations.id'], name=op.f('fk_cameras_org_id_organizations'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_cameras')),
    sa.UniqueConstraint('device_id', 'edge_camera_id', name='uq_cameras_device_edge_id')
    )
    op.create_index(op.f('ix_cameras_org_id'), 'cameras', ['org_id'], unique=False)
    op.create_table('ingest_batches',
    sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
    sa.Column('device_id', sa.Uuid(), nullable=False),
    sa.Column('batch_id', sa.String(length=64), nullable=False),
    sa.Column('received_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('rows', sa.Integer(), nullable=False),
    sa.Column('rejected', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.ForeignKeyConstraint(['device_id'], ['devices.id'], name=op.f('fk_ingest_batches_device_id_devices'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_ingest_batches')),
    sa.UniqueConstraint('device_id', 'batch_id', name='uq_ingest_batches_device_batch')
    )
    op.create_index(op.f('ix_ingest_batches_device_id'), 'ingest_batches', ['device_id'], unique=False)
    op.create_index(op.f('ix_ingest_batches_received_at'), 'ingest_batches', ['received_at'], unique=False)
    op.create_table('pairing_codes',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('org_id', sa.Uuid(), nullable=False),
    sa.Column('site_id', sa.Uuid(), nullable=False),
    sa.Column('device_name', sa.String(length=120), nullable=False),
    sa.Column('code_hash', sa.String(length=64), nullable=False),
    sa.Column('created_by', sa.Uuid(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('used_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('device_id', sa.Uuid(), nullable=True),
    sa.ForeignKeyConstraint(['created_by'], ['users.id'], name=op.f('fk_pairing_codes_created_by_users'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['device_id'], ['devices.id'], name=op.f('fk_pairing_codes_device_id_devices'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['org_id'], ['organizations.id'], name=op.f('fk_pairing_codes_org_id_organizations'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['site_id'], ['sites.id'], name=op.f('fk_pairing_codes_site_id_sites'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_pairing_codes')),
    sa.UniqueConstraint('code_hash', name=op.f('uq_pairing_codes_code_hash'))
    )
    op.create_index(op.f('ix_pairing_codes_org_id'), 'pairing_codes', ['org_id'], unique=False)
    op.create_table('count_events',
    sa.Column('camera_id', sa.Uuid(), nullable=False),
    sa.Column('ts', sa.DateTime(timezone=True), nullable=False),
    sa.Column('event_id', sa.String(length=64), nullable=False),
    sa.Column('kind', sa.String(length=32), nullable=False),
    sa.Column('name', sa.String(length=64), nullable=False),
    sa.Column('class_name', sa.String(length=32), nullable=True),
    sa.Column('direction', sa.String(length=8), nullable=True),
    sa.Column('dwell_s', sa.Float(), nullable=True),
    sa.Column('speed_kmh', sa.Float(), nullable=True),
    sa.ForeignKeyConstraint(['camera_id'], ['cameras.id'], name=op.f('fk_count_events_camera_id_cameras'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('camera_id', 'event_id', 'ts', name=op.f('pk_count_events'))
    )
    op.create_index('ix_count_events_camera_ts', 'count_events', ['camera_id', 'ts'], unique=False)
    op.create_table('coverage',
    sa.Column('camera_id', sa.Uuid(), nullable=False),
    sa.Column('window_start', sa.DateTime(timezone=True), nullable=False),
    sa.Column('frames', sa.Integer(), nullable=False),
    sa.Column('seconds', sa.Float(), nullable=False),
    sa.ForeignKeyConstraint(['camera_id'], ['cameras.id'], name=op.f('fk_coverage_camera_id_cameras'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('camera_id', 'window_start', name=op.f('pk_coverage'))
    )
    op.create_table('line_counts',
    sa.Column('camera_id', sa.Uuid(), nullable=False),
    sa.Column('window_start', sa.DateTime(timezone=True), nullable=False),
    sa.Column('line', sa.String(length=64), nullable=False),
    sa.Column('class_name', sa.String(length=32), nullable=False),
    sa.Column('in_count', sa.Integer(), nullable=False),
    sa.Column('out_count', sa.Integer(), nullable=False),
    sa.Column('received_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['camera_id'], ['cameras.id'], name=op.f('fk_line_counts_camera_id_cameras'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('camera_id', 'line', 'class_name', 'window_start', name=op.f('pk_line_counts'))
    )
    op.create_table('zone_stats',
    sa.Column('camera_id', sa.Uuid(), nullable=False),
    sa.Column('window_start', sa.DateTime(timezone=True), nullable=False),
    sa.Column('zone', sa.String(length=64), nullable=False),
    sa.Column('class_name', sa.String(length=32), nullable=False),
    sa.Column('sample_s', sa.Float(), nullable=False),
    sa.Column('occ_avg', sa.Float(), nullable=False),
    sa.Column('occ_max', sa.Integer(), nullable=False),
    sa.Column('occ_last', sa.Integer(), nullable=False),
    sa.Column('queue_avg', sa.Float(), nullable=False),
    sa.Column('queue_max', sa.Integer(), nullable=False),
    sa.Column('visits', sa.Integer(), nullable=False),
    sa.Column('dwell_sum_s', sa.Float(), nullable=False),
    sa.Column('dwell_max_s', sa.Float(), nullable=False),
    sa.Column('received_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['camera_id'], ['cameras.id'], name=op.f('fk_zone_stats_camera_id_cameras'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('camera_id', 'zone', 'class_name', 'window_start', name=op.f('pk_zone_stats'))
    )
    _make_hypertables()


# Time series tables -> TimescaleDB hypertables (7-day chunks), only if the extension is there.
# create_default_indexes=false: our primary keys already cover (camera, ..., time).
HYPERTABLES = {"line_counts": "window_start", "zone_stats": "window_start", "coverage": "window_start",
               "count_events": "ts"}


def _make_hypertables() -> None:
    bind = op.get_bind()
    has_ts = bind.execute(sa.text("SELECT 1 FROM pg_extension WHERE extname = 'timescaledb'")).first()
    if not has_ts:
        return
    for table, column in HYPERTABLES.items():
        op.execute(sa.text(
            f"SELECT create_hypertable('{table}', by_range('{column}', INTERVAL '7 days'), "
            "create_default_indexes => FALSE, if_not_exists => TRUE)"
        ))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('zone_stats')
    op.drop_table('line_counts')
    op.drop_table('coverage')
    op.drop_index('ix_count_events_camera_ts', table_name='count_events')
    op.drop_table('count_events')
    op.drop_index(op.f('ix_pairing_codes_org_id'), table_name='pairing_codes')
    op.drop_table('pairing_codes')
    op.drop_index(op.f('ix_ingest_batches_received_at'), table_name='ingest_batches')
    op.drop_index(op.f('ix_ingest_batches_device_id'), table_name='ingest_batches')
    op.drop_table('ingest_batches')
    op.drop_index(op.f('ix_cameras_org_id'), table_name='cameras')
    op.drop_table('cameras')
    op.drop_index(op.f('ix_devices_site_id'), table_name='devices')
    op.drop_index(op.f('ix_devices_org_id'), table_name='devices')
    op.drop_table('devices')
    op.drop_index(op.f('ix_sites_org_id'), table_name='sites')
    op.drop_table('sites')
