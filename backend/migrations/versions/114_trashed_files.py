"""Files and folders the file manager moved to the Recycle Bin.

Fork: arya-arav/ServerKit.
"""
from alembic import op
import sqlalchemy as sa

revision = '114_trashed_files'
down_revision = '113_notification_dismissed_at'
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    # Create-if-missing: the pre-alembic schema sync may already have made it.
    if 'trashed_files' in inspector.get_table_names():
        return
    op.create_table(
        'trashed_files',
        sa.Column('id', sa.Integer, primary_key=True),
        sa.Column('original_path', sa.Text, nullable=False),
        sa.Column('trash_path', sa.Text, nullable=False),
        sa.Column('is_dir', sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column('size', sa.BigInteger),
        sa.Column('deleted_at', sa.DateTime),
        sa.Column('deleted_by_id', sa.Integer),
    )
    op.create_index('ix_trashed_files_deleted_at', 'trashed_files', ['deleted_at'])


def downgrade():
    op.drop_index('ix_trashed_files_deleted_at', table_name='trashed_files')
    op.drop_table('trashed_files')
