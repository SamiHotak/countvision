"""Platform tables: users, login sessions, email tokens, organizations, memberships, invites, audit log.

Revision ID: 0001
Revises: 
Create Date: 2026-10-07

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '0001'
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('users',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('email', sa.String(length=320), nullable=False),
    sa.Column('name', sa.String(length=120), nullable=False),
    sa.Column('password_hash', sa.String(length=255), nullable=True),
    sa.Column('email_verified_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('google_sub', sa.String(length=255), nullable=True),
    sa.Column('is_active', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('last_login_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_users')),
    sa.UniqueConstraint('email', name=op.f('uq_users_email')),
    sa.UniqueConstraint('google_sub', name=op.f('uq_users_google_sub'))
    )
    op.create_table('email_tokens',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('purpose', sa.String(length=32), nullable=False),
    sa.Column('token_hash', sa.String(length=64), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('used_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint("purpose IN ('verify_email', 'reset_password')", name=op.f('ck_email_tokens_purpose')),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_email_tokens_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_email_tokens')),
    sa.UniqueConstraint('token_hash', name=op.f('uq_email_tokens_token_hash'))
    )
    op.create_index(op.f('ix_email_tokens_expires_at'), 'email_tokens', ['expires_at'], unique=False)
    op.create_index(op.f('ix_email_tokens_user_id'), 'email_tokens', ['user_id'], unique=False)
    op.create_table('organizations',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('name', sa.String(length=120), nullable=False),
    sa.Column('slug', sa.String(length=80), nullable=False),
    sa.Column('created_by', sa.Uuid(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['created_by'], ['users.id'], name=op.f('fk_organizations_created_by_users'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_organizations')),
    sa.UniqueConstraint('slug', name=op.f('uq_organizations_slug'))
    )
    op.create_table('user_sessions',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('token_hash', sa.String(length=64), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('last_seen_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_user_sessions_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_user_sessions')),
    sa.UniqueConstraint('token_hash', name=op.f('uq_user_sessions_token_hash'))
    )
    op.create_index(op.f('ix_user_sessions_expires_at'), 'user_sessions', ['expires_at'], unique=False)
    op.create_index(op.f('ix_user_sessions_user_id'), 'user_sessions', ['user_id'], unique=False)
    op.create_table('audit_log',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.Column('org_id', sa.Uuid(), nullable=True),
    sa.Column('actor_user_id', sa.Uuid(), nullable=True),
    sa.Column('action', sa.String(length=64), nullable=False),
    sa.Column('target_type', sa.String(length=32), nullable=True),
    sa.Column('target_id', sa.String(length=64), nullable=True),
    sa.Column('meta', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['actor_user_id'], ['users.id'], name=op.f('fk_audit_log_actor_user_id_users'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['org_id'], ['organizations.id'], name=op.f('fk_audit_log_org_id_organizations'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_audit_log'))
    )
    op.create_index(op.f('ix_audit_log_created_at'), 'audit_log', ['created_at'], unique=False)
    op.create_index(op.f('ix_audit_log_org_id'), 'audit_log', ['org_id'], unique=False)
    op.create_table('invites',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('org_id', sa.Uuid(), nullable=False),
    sa.Column('email', sa.String(length=320), nullable=False),
    sa.Column('role', sa.String(length=16), nullable=False),
    sa.Column('token_hash', sa.String(length=64), nullable=False),
    sa.Column('invited_by', sa.Uuid(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('accepted_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('accepted_by', sa.Uuid(), nullable=True),
    sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint("role IN ('owner', 'admin', 'member', 'viewer')", name=op.f('ck_invites_role')),
    sa.ForeignKeyConstraint(['accepted_by'], ['users.id'], name=op.f('fk_invites_accepted_by_users'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['invited_by'], ['users.id'], name=op.f('fk_invites_invited_by_users'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['org_id'], ['organizations.id'], name=op.f('fk_invites_org_id_organizations'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_invites')),
    sa.UniqueConstraint('token_hash', name=op.f('uq_invites_token_hash'))
    )
    op.create_index(op.f('ix_invites_org_id'), 'invites', ['org_id'], unique=False)
    op.create_index('uq_invites_open_email', 'invites', ['org_id', 'email'], unique=True, postgresql_where=sa.text('accepted_at IS NULL AND revoked_at IS NULL'))
    op.create_table('memberships',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('org_id', sa.Uuid(), nullable=False),
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('role', sa.String(length=16), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("role IN ('owner', 'admin', 'member', 'viewer')", name=op.f('ck_memberships_role')),
    sa.ForeignKeyConstraint(['org_id'], ['organizations.id'], name=op.f('fk_memberships_org_id_organizations'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_memberships_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_memberships')),
    sa.UniqueConstraint('org_id', 'user_id', name='uq_memberships_org_user')
    )
    op.create_index(op.f('ix_memberships_user_id'), 'memberships', ['user_id'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_memberships_user_id'), table_name='memberships')
    op.drop_table('memberships')
    op.drop_index('uq_invites_open_email', table_name='invites', postgresql_where=sa.text('accepted_at IS NULL AND revoked_at IS NULL'))
    op.drop_index(op.f('ix_invites_org_id'), table_name='invites')
    op.drop_table('invites')
    op.drop_index(op.f('ix_audit_log_org_id'), table_name='audit_log')
    op.drop_index(op.f('ix_audit_log_created_at'), table_name='audit_log')
    op.drop_table('audit_log')
    op.drop_index(op.f('ix_user_sessions_user_id'), table_name='user_sessions')
    op.drop_index(op.f('ix_user_sessions_expires_at'), table_name='user_sessions')
    op.drop_table('user_sessions')
    op.drop_table('organizations')
    op.drop_index(op.f('ix_email_tokens_user_id'), table_name='email_tokens')
    op.drop_index(op.f('ix_email_tokens_expires_at'), table_name='email_tokens')
    op.drop_table('email_tokens')
    op.drop_table('users')
