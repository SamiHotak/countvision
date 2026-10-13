"""Billing: plan state per organization, Stripe webhook events, platform admins, plan limits.

Every existing organization gets a fresh free trial (14 days from the upgrade), so nothing stops
working on the day billing is switched on.

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-12

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '0006'
down_revision: str | Sequence[str] | None = '0005'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('users', sa.Column('is_platform_admin', sa.Boolean(), server_default=sa.text('false'), nullable=False))
    op.add_column('cameras', sa.Column('plan_paused', sa.Boolean(), server_default=sa.text('false'), nullable=False))
    op.add_column('video_jobs', sa.Column('billed_month', sa.String(length=7), nullable=True))
    op.add_column('video_jobs', sa.Column('billed_minutes', sa.Integer(), server_default=sa.text('0'), nullable=False))

    op.create_table('org_billing',
    sa.Column('org_id', sa.Uuid(), nullable=False),
    sa.Column('trial_ends_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('stripe_customer_id', sa.String(length=64), nullable=True),
    sa.Column('stripe_subscription_id', sa.String(length=64), nullable=True),
    sa.Column('plan', sa.String(length=32), nullable=True),
    sa.Column('sub_status', sa.String(length=24), nullable=True),
    sa.Column('current_period_end', sa.DateTime(timezone=True), nullable=True),
    sa.Column('cancel_at_period_end', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('sub_trial_end', sa.DateTime(timezone=True), nullable=True),
    sa.Column('comp_plan', sa.String(length=32), nullable=True),
    sa.Column('comp_until', sa.DateTime(timezone=True), nullable=True),
    sa.Column('comp_note', sa.String(length=200), nullable=True),
    sa.Column('synced_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['org_id'], ['organizations.id'], name=op.f('fk_org_billing_org_id_organizations'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('org_id', name=op.f('pk_org_billing')),
    sa.UniqueConstraint('stripe_customer_id', name=op.f('uq_org_billing_stripe_customer_id'))
    )
    op.create_table('stripe_events',
    sa.Column('id', sa.String(length=80), nullable=False),
    sa.Column('type', sa.String(length=80), nullable=False),
    sa.Column('org_id', sa.Uuid(), nullable=True),
    sa.Column('received_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('error', sa.String(length=300), nullable=True),
    sa.ForeignKeyConstraint(['org_id'], ['organizations.id'], name=op.f('fk_stripe_events_org_id_organizations'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_stripe_events'))
    )
    op.create_index(op.f('ix_stripe_events_received_at'), 'stripe_events', ['received_at'], unique=False)

    # Existing organizations: a fresh trial from today.
    op.execute("INSERT INTO org_billing (org_id, trial_ends_at) "
               "SELECT id, now() + interval '14 days' FROM organizations")


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_stripe_events_received_at'), table_name='stripe_events')
    op.drop_table('stripe_events')
    op.drop_table('org_billing')
    op.drop_column('video_jobs', 'billed_minutes')
    op.drop_column('video_jobs', 'billed_month')
    op.drop_column('cameras', 'plan_paused')
    op.drop_column('users', 'is_platform_admin')
