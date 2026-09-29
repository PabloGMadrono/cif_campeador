"""Add the invoice fiscal reconciliation status.

Revision ID: 0004_invoice_fiscal_status
Revises: 0003_invoice_tax_lines
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_invoice_fiscal_status"
down_revision: str | None = "0003_invoice_tax_lines"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("invoices", sa.Column("fiscal_status", sa.String(32), nullable=True))


def downgrade() -> None:
    op.drop_column("invoices", "fiscal_status")
