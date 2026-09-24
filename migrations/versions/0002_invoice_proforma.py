"""Add the historical proforma flag to invoices.

Revision ID: 0002_invoice_proforma
Revises: 0001_invoice_pipeline
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_invoice_proforma"
down_revision: str | None = "0001_invoice_pipeline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("invoices", sa.Column("is_proforma", sa.Boolean(), nullable=True))


def downgrade() -> None:
    op.drop_column("invoices", "is_proforma")
