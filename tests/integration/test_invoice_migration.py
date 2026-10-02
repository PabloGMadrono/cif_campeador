"""Upgrade coverage for the normalized invoice tax schema."""

from pathlib import Path

import sqlalchemy as sa
from alembic import command
from alembic.config import Config

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_migration_preserves_the_legacy_vat_line(monkeypatch, tmp_path):
    database_path = tmp_path / "migration.db"
    database_url = f"sqlite:///{database_path.as_posix()}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    config = Config()
    config.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))

    command.upgrade(config, "0001_invoice_pipeline")
    engine = sa.create_engine(database_url)
    document_id = "00000000000000000000000000000001"
    with engine.begin() as connection:
        connection.execute(sa.text("""
            INSERT INTO invoices (
                document_id, fecha, numero_factura, nif_proveedor,
                nombre_proveedor, base_imponible, tipo_iva, cuota_iva,
                total, extracted_at
            ) VALUES (
                :document_id, '2026-09-24', 'F-1', 'B12345678',
                'Supplier SL', '100.00', '21', '21.00',
                '121.00', '2026-09-24 12:00:00'
            )
        """), {"document_id": document_id})

    command.upgrade(config, "head")
    inspector = sa.inspect(engine)
    invoice_columns = {column["name"] for column in inspector.get_columns("invoices")}
    assert {
        "validity",
        "diagnostic_type",
        "fiscal_status",
        "base_retencion",
        "tipo_irpf",
        "cuota_irpf",
    } <= invoice_columns
    assert {"base_imponible", "tipo_iva", "cuota_iva"}.isdisjoint(invoice_columns)
    assert "invoice_iva_lines" in inspector.get_table_names()
    assert "invoice_equivalence_surcharges" in inspector.get_table_names()
    with engine.connect() as connection:
        migrated = connection.execute(sa.text("""
            SELECT position, base_imponible, tipo_iva, cuota_iva
            FROM invoice_iva_lines
            WHERE document_id = :document_id
        """), {"document_id": document_id}).one()
    assert migrated == (0, "100.00", "21", "21.00")

    command.downgrade(config, "0001_invoice_pipeline")
    with engine.connect() as connection:
        restored = connection.execute(sa.text("""
            SELECT base_imponible, tipo_iva, cuota_iva
            FROM invoices
            WHERE document_id = :document_id
        """), {"document_id": document_id}).one()
    assert restored == ("100.00", "21", "21.00")
    engine.dispose()


def test_minio_migration_preserves_legacy_paths_and_schema_constraints(monkeypatch, tmp_path):
    database_url = f"sqlite:///{(tmp_path / 'media-schema.db').as_posix()}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    config = Config()
    config.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    command.upgrade(config, "0004_invoice_fiscal_status")
    engine = sa.create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(sa.text("""
            INSERT INTO invoice_submissions (
                id, whatsapp_message_id, whatsapp_media_id, sender_phone_number,
                message_type, mime_type, storage_path, file_size, received_at,
                status, download_attempts, ocr_attempts, created_at, updated_at
            ) VALUES (
                '00000000000000000000000000000001', 'message-1', 'media-1', '+34600000000',
                'image', 'image/jpeg', '2026/10/02/legacy.jpg', 7, '2026-10-02 00:00:00',
                'completed', 1, 1, '2026-10-02 00:00:00', '2026-10-02 00:00:00'
            )
        """))
    command.upgrade(config, "head")
    with engine.connect() as connection:
        document = connection.execute(sa.text("""
            SELECT storage_backend, storage_path, storage_bucket, storage_object_key,
                   content_sha256, status, download_attempts, ocr_attempts
            FROM invoice_submissions
        """)).one()
    assert document == ("local", "2026/10/02/legacy.jpg", None, None, None, "completed", 1, 1)
    inspector = sa.inspect(engine)
    constraints = inspector.get_unique_constraints("invoice_submissions")
    assert any(constraint["column_names"] == ["storage_bucket", "storage_object_key"] for constraint in constraints)
    columns = {column["name"]: column for column in inspector.get_columns("invoice_submissions")}
    assert columns["storage_backend"]["nullable"] is False
    assert columns["storage_backend"]["default"] is None

    command.downgrade(config, "0004_invoice_fiscal_status")
    with engine.connect() as connection:
        assert connection.execute(sa.text("SELECT storage_path, status FROM invoice_submissions")).one() == (
            "2026/10/02/legacy.jpg", "completed",
        )
    engine.dispose()
