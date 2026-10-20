"""A user's first and last name and profile picture. NULL = not set.

Fork: arya-arav/ServerKit.
"""
from alembic import op
import sqlalchemy as sa

revision = '112_user_profile_name_avatar'
down_revision = '111_redirect_aliases'
branch_labels = None
depends_on = None

_COLUMNS = (
    ('first_name', lambda: sa.Column('first_name', sa.String(80), nullable=True)),
    ('last_name', lambda: sa.Column('last_name', sa.String(80), nullable=True)),
    ('avatar', lambda: sa.Column('avatar', sa.Text, nullable=True)),
)


def upgrade():
    inspector = sa.inspect(op.get_bind())
    # Add-if-missing: the pre-alembic schema sync may already have added them.
    existing = {c['name'] for c in inspector.get_columns('users')}
    for name, column in _COLUMNS:
        if name not in existing:
            op.add_column('users', column())


def downgrade():
    for name, _column in reversed(_COLUMNS):
        op.drop_column('users', name)
