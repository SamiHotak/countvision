"""Alerts (rules, channels, history), API keys, webhook deliveries, video analysis jobs.

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-11

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '0005'
down_revision: str | Sequence[str] | None = '0004'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('api_keys',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('org_id', sa.Uuid(), nullable=False),
    sa.Column('name', sa.String(length=120), nullable=False),
    sa.Column('prefix', sa.String(length=16), nullable=False),
    sa.Column('key_hash', sa.String(length=64), nullable=False),
    sa.Column('created_by', sa.Uuid(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('last_used_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['created_by'], ['users.id'], name=op.f('fk_api_keys_created_by_users'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['org_id'], ['organizations.id'], name=op.f('fk_api_keys_org_id_organizations'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_api_keys')),
    sa.UniqueConstraint('key_hash', name=op.f('uq_api_keys_key_hash'))
    )
    op.create_index(op.f('ix_api_keys_org_id'), 'api_keys', ['org_id'], unique=False)
    op.create_table('notification_channels',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('org_id', sa.Uuid(), nullable=False),
    sa.Column('kind', sa.String(length=16), nullable=False),
    sa.Column('name', sa.String(length=120), nullable=False),
    sa.Column('config', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('secret_enc', sa.String(length=500), nullable=True),
    sa.Column('enabled', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('created_by', sa.Uuid(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('last_ok_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_error', sa.String(length=300), nullable=True),
    sa.Column('last_error_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['created_by'], ['users.id'], name=op.f('fk_notification_channels_created_by_users'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['org_id'], ['organizations.id'], name=op.f('fk_notification_channels_org_id_organizations'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_notification_channels'))
    )
    op.create_index(op.f('ix_notification_channels_org_id'), 'notification_channels', ['org_id'], unique=False)
    op.create_table('video_jobs',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('org_id', sa.Uuid(), nullable=False),
    sa.Column('created_by', sa.Uuid(), nullable=True),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('status', sa.String(length=12), server_default=sa.text("'uploading'"), nullable=False),
    sa.Column('size_bytes', sa.BigInteger(), nullable=False),
    sa.Column('received_bytes', sa.BigInteger(), server_default=sa.text('0'), nullable=False),
    sa.Column('duration_s', sa.Float(), nullable=True),
    sa.Column('fps', sa.Float(), nullable=True),
    sa.Column('frame_count', sa.Integer(), nullable=True),
    sa.Column('width', sa.Integer(), nullable=True),
    sa.Column('height', sa.Integer(), nullable=True),
    sa.Column('config', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('progress', sa.Float(), server_default=sa.text('0'), nullable=False),
    sa.Column('cancel_requested', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('result', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('error', sa.String(length=500), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('video_deleted_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['created_by'], ['users.id'], name=op.f('fk_video_jobs_created_by_users'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['org_id'], ['organizations.id'], name=op.f('fk_video_jobs_org_id_organizations'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_video_jobs'))
    )
    op.create_index(op.f('ix_video_jobs_org_id'), 'video_jobs', ['org_id'], unique=False)
    op.create_table('webhook_deliveries',
    sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
    sa.Column('org_id', sa.Uuid(), nullable=False),
    sa.Column('channel_id', sa.Uuid(), nullable=False),
    sa.Column('event_id', sa.Uuid(), nullable=False),
    sa.Column('event_type', sa.String(length=32), nullable=False),
    sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('status', sa.String(length=8), server_default=sa.text("'pending'"), nullable=False),
    sa.Column('attempts', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('next_try_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('response_code', sa.Integer(), nullable=True),
    sa.Column('error', sa.String(length=300), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('delivered_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['channel_id'], ['notification_channels.id'], name=op.f('fk_webhook_deliveries_channel_id_notification_channels'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['org_id'], ['organizations.id'], name=op.f('fk_webhook_deliveries_org_id_organizations'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_webhook_deliveries'))
    )
    op.create_index(op.f('ix_webhook_deliveries_channel_id'), 'webhook_deliveries', ['channel_id'], unique=False)
    op.create_index(op.f('ix_webhook_deliveries_created_at'), 'webhook_deliveries', ['created_at'], unique=False)
    op.create_index('ix_webhook_deliveries_due', 'webhook_deliveries', ['status', 'next_try_at'], unique=False)
    op.create_table('alert_rules',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('org_id', sa.Uuid(), nullable=False),
    sa.Column('site_id', sa.Uuid(), nullable=False),
    sa.Column('name', sa.String(length=120), nullable=False),
    sa.Column('kind', sa.String(length=16), nullable=False),
    sa.Column('camera_id', sa.Uuid(), nullable=True),
    sa.Column('zone', sa.String(length=64), nullable=True),
    sa.Column('threshold', sa.Float(), server_default=sa.text('0'), nullable=False),
    sa.Column('duration_min', sa.Integer(), server_default=sa.text('5'), nullable=False),
    sa.Column('channel_ids', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
    sa.Column('enabled', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('created_by', sa.Uuid(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('state', sa.String(length=8), server_default=sa.text("'ok'"), nullable=False),
    sa.Column('true_since', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_true_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('fired_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_value', sa.Float(), nullable=True),
    sa.Column('last_eval_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_message', sa.String(length=300), nullable=True),
    sa.ForeignKeyConstraint(['camera_id'], ['cameras.id'], name=op.f('fk_alert_rules_camera_id_cameras'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['created_by'], ['users.id'], name=op.f('fk_alert_rules_created_by_users'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['org_id'], ['organizations.id'], name=op.f('fk_alert_rules_org_id_organizations'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['site_id'], ['sites.id'], name=op.f('fk_alert_rules_site_id_sites'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_alert_rules'))
    )
    op.create_index(op.f('ix_alert_rules_org_id'), 'alert_rules', ['org_id'], unique=False)
    op.create_index(op.f('ix_alert_rules_site_id'), 'alert_rules', ['site_id'], unique=False)
    op.create_table('alert_events',
    sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
    sa.Column('org_id', sa.Uuid(), nullable=False),
    sa.Column('rule_id', sa.Uuid(), nullable=True),
    sa.Column('site_id', sa.Uuid(), nullable=True),
    sa.Column('rule_name', sa.String(length=120), nullable=False),
    sa.Column('kind', sa.String(length=16), nullable=False),
    sa.Column('status', sa.String(length=8), nullable=False),
    sa.Column('value', sa.Float(), nullable=True),
    sa.Column('message', sa.String(length=500), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['org_id'], ['organizations.id'], name=op.f('fk_alert_events_org_id_organizations'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['rule_id'], ['alert_rules.id'], name=op.f('fk_alert_events_rule_id_alert_rules'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['site_id'], ['sites.id'], name=op.f('fk_alert_events_site_id_sites'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_alert_events'))
    )
    op.create_index('ix_alert_events_org_created', 'alert_events', ['org_id', 'created_at'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_alert_events_org_created', table_name='alert_events')
    op.drop_table('alert_events')
    op.drop_index(op.f('ix_alert_rules_site_id'), table_name='alert_rules')
    op.drop_index(op.f('ix_alert_rules_org_id'), table_name='alert_rules')
    op.drop_table('alert_rules')
    op.drop_index('ix_webhook_deliveries_due', table_name='webhook_deliveries')
    op.drop_index(op.f('ix_webhook_deliveries_created_at'), table_name='webhook_deliveries')
    op.drop_index(op.f('ix_webhook_deliveries_channel_id'), table_name='webhook_deliveries')
    op.drop_table('webhook_deliveries')
    op.drop_index(op.f('ix_video_jobs_org_id'), table_name='video_jobs')
    op.drop_table('video_jobs')
    op.drop_index(op.f('ix_notification_channels_org_id'), table_name='notification_channels')
    op.drop_table('notification_channels')
    op.drop_index(op.f('ix_api_keys_org_id'), table_name='api_keys')
    op.drop_table('api_keys')
