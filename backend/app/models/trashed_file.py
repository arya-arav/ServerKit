"""A file or folder moved to the Recycle Bin by the file manager.

Fork: arya-arav/ServerKit. The row exists only while the item is in the bin
(it is created already tombstoned): restoring moves the item back to
``original_path`` and drops the row, purging deletes ``trash_path`` for good.
"""
from app import db
from app.models.mixins import SoftDeleteMixin


class TrashedFile(SoftDeleteMixin, db.Model):
    __tablename__ = 'trashed_files'

    id = db.Column(db.Integer, primary_key=True)
    original_path = db.Column(db.Text, nullable=False)
    trash_path = db.Column(db.Text, nullable=False)
    is_dir = db.Column(db.Boolean, nullable=False, default=False)
    size = db.Column(db.BigInteger, nullable=True)  # files only; folders aren't walked

    def __repr__(self):
        return f'<TrashedFile {self.id} {self.original_path}>'
