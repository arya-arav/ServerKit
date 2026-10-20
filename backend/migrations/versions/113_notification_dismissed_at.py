"""In-app notifications a user removed from their bell. NULL = still shown.

Fork: arya-arav/ServerKit.
"""
from alembic import op
import sqlalchemy as sa

revision = '113_notification_dismissed_at'
down_revision = '112_user_profile_name_avatar'
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    # Add-if-missing: the pre-alembic schema sync may already have added it.
    if 'dismissed_at' not in {c['name'] for c in inspector.get_columns('notification_deliveries')}:
        op.add_column('notification_deliveries', sa.Column('dismissed_at', sa.DateTime, nullable=True))


def downgrade():
    op.drop_column('notification_deliveries', 'dismissed_at')
