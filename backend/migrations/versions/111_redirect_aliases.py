"""Opt-in redirect of a service's other domains to its primary domain. NULL = off.

Fork: arya-arav/ServerKit.
"""
from alembic import op
import sqlalchemy as sa

revision = '111_redirect_aliases'
down_revision = '110_drop_workflow_schedules_job'
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    # Add-if-missing: the pre-alembic schema sync may already have added it.
    if 'redirect_aliases' not in {c['name'] for c in inspector.get_columns('applications')}:
        op.add_column('applications', sa.Column('redirect_aliases', sa.Boolean, nullable=True))


def downgrade():
    op.drop_column('applications', 'redirect_aliases')
