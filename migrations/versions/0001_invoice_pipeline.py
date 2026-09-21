"""Create customer, submission, and invoice tables.

Revision ID: 0001_invoice_pipeline
Revises:
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001_invoice_pipeline"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "customers",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("display_name", sa.String(255), nullable=False),
        sa.Column("legal_name", sa.String(255), nullable=True),
        sa.Column("tax_id", sa.String(64), nullable=True),
        sa.Column("address", sa.Text(), nullable=True),
        sa.Column("email", sa.String(320), nullable=True),
        sa.Column("is_provisional", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_customers"),
        sa.UniqueConstraint("tax_id", name="uq_customers_tax_id"),
    )
    op.create_table(
        "customer_phone_numbers",
        sa.Column("phone_number", sa.String(16), nullable=False),
        sa.Column("customer_id", sa.Uuid(), nullable=False),
        sa.Column("meta_user_id", sa.String(255), nullable=True),
        sa.Column("profile_name", sa.String(255), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["customer_id"],
            ["customers.id"],
            name="fk_customer_phone_numbers_customer_id_customers",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("phone_number", name="pk_customer_phone_numbers"),
    )
    op.create_index(
        "ix_customer_phone_numbers_customer_id",
        "customer_phone_numbers",
        ["customer_id"],
    )
    op.create_index(
        "ix_customer_phone_numbers_meta_user_id",
        "customer_phone_numbers",
        ["meta_user_id"],
    )
    op.create_table(
        "invoice_submissions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("whatsapp_message_id", sa.String(255), nullable=False),
        sa.Column("whatsapp_media_id", sa.String(255), nullable=False),
        sa.Column("sender_phone_number", sa.String(16), nullable=False),
        sa.Column("message_type", sa.String(16), nullable=False),
        sa.Column("mime_type", sa.String(127), nullable=False),
        sa.Column("sha256", sa.String(128), nullable=True),
        sa.Column("original_filename", sa.String(255), nullable=True),
        sa.Column("storage_path", sa.String(1024), nullable=True),
        sa.Column("file_size", sa.Integer(), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("downloaded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ocr_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("extractor_name", sa.String(255), nullable=True),
        sa.Column("download_attempts", sa.Integer(), nullable=False),
        sa.Column("ocr_attempts", sa.Integer(), nullable=False),
        sa.Column("failure_stage", sa.String(16), nullable=True),
        sa.Column("last_error", sa.String(2000), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["sender_phone_number"],
            ["customer_phone_numbers.phone_number"],
            name=(
                "fk_invoice_submissions_sender_phone_number_"
                "customer_phone_numbers"
            ),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_invoice_submissions"),
        sa.UniqueConstraint(
            "whatsapp_message_id",
            name="uq_invoice_submissions_whatsapp_message_id",
        ),
        sa.UniqueConstraint(
            "storage_path",
            name="uq_invoice_submissions_storage_path",
        ),
    )
    op.create_index(
        "ix_invoice_submissions_whatsapp_media_id",
        "invoice_submissions",
        ["whatsapp_media_id"],
    )
    op.create_index(
        "ix_invoice_submissions_sender_phone_number",
        "invoice_submissions",
        ["sender_phone_number"],
    )
    op.create_index(
        "ix_invoice_submissions_received_at",
        "invoice_submissions",
        ["received_at"],
    )
    op.create_index(
        "ix_invoice_submissions_status",
        "invoice_submissions",
        ["status"],
    )
    op.create_index(
        "ix_invoice_submissions_status_received",
        "invoice_submissions",
        ["status", "received_at"],
    )
    op.create_table(
        "invoices",
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("fecha", sa.String(64), nullable=True),
        sa.Column("numero_factura", sa.String(255), nullable=True),
        sa.Column("nif_proveedor", sa.String(64), nullable=True),
        sa.Column("nombre_proveedor", sa.String(255), nullable=True),
        sa.Column("base_imponible", sa.String(64), nullable=True),
        sa.Column("tipo_iva", sa.String(64), nullable=True),
        sa.Column("cuota_iva", sa.String(64), nullable=True),
        sa.Column("total", sa.String(64), nullable=True),
        sa.Column("extracted_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["invoice_submissions.id"],
            name="fk_invoices_document_id_invoice_submissions",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("document_id", name="pk_invoices"),
    )


def downgrade() -> None:
    op.drop_table("invoices")
    op.drop_index(
        "ix_invoice_submissions_status_received",
        table_name="invoice_submissions",
    )
    op.drop_index("ix_invoice_submissions_status", table_name="invoice_submissions")
    op.drop_index(
        "ix_invoice_submissions_received_at",
        table_name="invoice_submissions",
    )
    op.drop_index(
        "ix_invoice_submissions_sender_phone_number",
        table_name="invoice_submissions",
    )
    op.drop_index(
        "ix_invoice_submissions_whatsapp_media_id",
        table_name="invoice_submissions",
    )
    op.drop_table("invoice_submissions")
    op.drop_index(
        "ix_customer_phone_numbers_meta_user_id",
        table_name="customer_phone_numbers",
    )
    op.drop_index(
        "ix_customer_phone_numbers_customer_id",
        table_name="customer_phone_numbers",
    )
    op.drop_table("customer_phone_numbers")
    op.drop_table("customers")
