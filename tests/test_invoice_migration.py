"""Upgrade coverage for the normalized invoice tax schema."""

from pathlib import Path

import sqlalchemy as sa
from alembic import command
from alembic.config import Config

PROJECT_ROOT = Path(__file__).resolve().parents[1]


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
    assert {"validity", "diagnostic_type", "base_retencion", "tipo_irpf", "cuota_irpf"} <= invoice_columns
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
