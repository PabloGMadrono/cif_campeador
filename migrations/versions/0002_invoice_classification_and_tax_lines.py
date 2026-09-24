"""Add invoice classification and normalized fiscal breakdowns.

Revision ID: 0003_invoice_tax_lines
Revises: 0002_invoice_proforma
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_invoice_tax_lines"
down_revision: str | None = "0002_invoice_proforma"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("invoices") as batch:
        batch.add_column(sa.Column("validity", sa.String(16), nullable=True))
        batch.add_column(sa.Column("diagnostic_type", sa.String(255), nullable=True))
        batch.add_column(sa.Column("base_retencion", sa.String(64), nullable=True))
        batch.add_column(sa.Column("tipo_irpf", sa.String(64), nullable=True))
        batch.add_column(sa.Column("cuota_irpf", sa.String(64), nullable=True))
        batch.create_index("ix_invoices_validity", ["validity"])
        batch.drop_column("is_proforma")

    op.create_table(
        "invoice_iva_lines",
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("base_imponible", sa.String(64), nullable=True),
        sa.Column("tipo_iva", sa.String(64), nullable=True),
        sa.Column("cuota_iva", sa.String(64), nullable=True),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["invoices.document_id"],
            name="fk_invoice_iva_lines_document_id_invoices",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "document_id", "position", name="pk_invoice_iva_lines"
        ),
    )
    op.create_table(
        "invoice_equivalence_surcharges",
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("base_imponible", sa.String(64), nullable=True),
        sa.Column("tipo_re", sa.String(64), nullable=True),
        sa.Column("cuota_re", sa.String(64), nullable=True),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["invoices.document_id"],
            name="fk_invoice_equivalence_surcharges_document_id_invoices",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "document_id",
            "position",
            name="pk_invoice_equivalence_surcharges",
        ),
    )

    invoices = sa.table(
        "invoices",
        sa.column("document_id", sa.Uuid()),
        sa.column("base_imponible", sa.String()),
        sa.column("tipo_iva", sa.String()),
        sa.column("cuota_iva", sa.String()),
    )
    iva_lines = sa.table(
        "invoice_iva_lines",
        sa.column("document_id", sa.Uuid()),
        sa.column("position", sa.Integer()),
        sa.column("base_imponible", sa.String()),
        sa.column("tipo_iva", sa.String()),
        sa.column("cuota_iva", sa.String()),
    )
    op.execute(
        iva_lines.insert().from_select(
            ["document_id", "position", "base_imponible", "tipo_iva", "cuota_iva"],
            sa.select(
                invoices.c.document_id,
                sa.literal(0),
                invoices.c.base_imponible,
                invoices.c.tipo_iva,
                invoices.c.cuota_iva,
            ).where(
                sa.or_(
                    invoices.c.base_imponible.is_not(None),
                    invoices.c.tipo_iva.is_not(None),
                    invoices.c.cuota_iva.is_not(None),
                )
            ),
        )
    )
    with op.batch_alter_table("invoices") as batch:
        batch.drop_column("base_imponible")
        batch.drop_column("tipo_iva")
        batch.drop_column("cuota_iva")


def downgrade() -> None:
    with op.batch_alter_table("invoices") as batch:
        batch.add_column(sa.Column("base_imponible", sa.String(64), nullable=True))
        batch.add_column(sa.Column("tipo_iva", sa.String(64), nullable=True))
        batch.add_column(sa.Column("cuota_iva", sa.String(64), nullable=True))

    connection = op.get_bind()
    connection.execute(
        sa.text(
            """
            UPDATE invoices
            SET base_imponible = (
                    SELECT base_imponible FROM invoice_iva_lines
                    WHERE invoice_iva_lines.document_id = invoices.document_id
                    ORDER BY position LIMIT 1
                ),
                tipo_iva = (
                    SELECT tipo_iva FROM invoice_iva_lines
                    WHERE invoice_iva_lines.document_id = invoices.document_id
                    ORDER BY position LIMIT 1
                ),
                cuota_iva = (
                    SELECT cuota_iva FROM invoice_iva_lines
                    WHERE invoice_iva_lines.document_id = invoices.document_id
                    ORDER BY position LIMIT 1
                )
            """
        )
    )
    op.drop_table("invoice_equivalence_surcharges")
    op.drop_table("invoice_iva_lines")
    with op.batch_alter_table("invoices") as batch:
        batch.drop_index("ix_invoices_validity")
        batch.drop_column("cuota_irpf")
        batch.drop_column("tipo_irpf")
        batch.drop_column("base_retencion")
        batch.drop_column("diagnostic_type")
        batch.drop_column("validity")
        batch.add_column(sa.Column("is_proforma", sa.Boolean(), nullable=True))
