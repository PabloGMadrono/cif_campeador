"""Add explicit object storage references while preserving legacy local paths.

Revision ID: 0005_minio_media
Revises: 0004_invoice_fiscal_status
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_minio_media"
down_revision: str | None = "0004_invoice_fiscal_status"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("invoice_submissions") as batch:
        batch.add_column(sa.Column("storage_backend", sa.String(16), nullable=False, server_default="local"))
        batch.add_column(sa.Column("storage_bucket", sa.String(63), nullable=True))
        batch.add_column(sa.Column("storage_object_key", sa.String(1024), nullable=True))
        batch.add_column(sa.Column("content_sha256", sa.String(64), nullable=True))
        batch.create_unique_constraint("uq_invoice_submissions_object", ["storage_bucket", "storage_object_key"])
    with op.batch_alter_table("invoice_submissions") as batch:
        batch.alter_column("storage_backend", existing_type=sa.String(16), server_default=None)


def downgrade() -> None:
    with op.batch_alter_table("invoice_submissions") as batch:
        batch.drop_constraint("uq_invoice_submissions_object", type_="unique")
        batch.drop_column("content_sha256")
        batch.drop_column("storage_object_key")
        batch.drop_column("storage_bucket")
        batch.drop_column("storage_backend")
