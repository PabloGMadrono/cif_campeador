"""Invoice prompts: shared business rules plus input-specific instructions.

Edit INVOICE_CLASSIFICATION_RULES for valid/invalid decisions,
INVOICE_EXTRACTION_RULES and INVOICE_FISCAL_AND_IDENTITY_RULES for fields,
and the relevant wrapper for image or block-specific behavior. The text-only
parser is retained for backends that transcribe before parsing.
"""

INVOICE_LABEL_HINTS = """Spanish invoice and receipt terminology to help recognize
the printed fields. These are possible labels, not values to invent or substitute:
- Invoice number (numero_factura): Nº de factura, Factura Simplificada, F.S.,
  Ticket, Nº Ticket, Operación, Op, Nº Oper, Transacción, Trans, Referencia, Ref,
  Recibo, Nº Recibo, Nº, #. Generic labels need the surrounding document context;
  they do not automatically identify the invoice number.
- Supplier tax ID (nif_proveedor, also called CIF): CIF, NIF, DNI, NIE, NIF-IVA,
  VAT, VAT Number. For an individual self-employed supplier, this can be a DNI
  with a final letter or a NIE. It may also be an unlabelled identifier directly
  below the company name. Pay particular attention to this identifier, which is
  more important for identifying the supplier than the shop's commercial name.
- Supplier legal name (nombre_proveedor): Razón Social, Titular, Empresa,
  Expedido por. Possible legal suffixes include S.L., S.L.U., S.A., S.A.U.,
  S.C.P., S.Coop. A self-employed supplier may instead use their given name and
  surnames without any company suffix. Do not require a suffix to recognize them.
"""

INVOICE_EXTRACTION_RULES = """Return null for missing or unreadable
fields; do not guess. Extract the supplier's name and tax ID, not the customer's.
Preserve invoice identifiers, leading zeroes, punctuation, and supplier names.
Return dates as YYYY-MM-DD when unambiguous. Return monetary amounts as decimal
strings in euros without currency symbols or thousands separators. Return tax
rates in percentage points (21 for 21%, not 0.21). Use only printed values; do
not calculate, aggregate, or correct monetary or percentage fields.
"""

INVOICE_CLASSIFICATION_RULES = """Classify every document with validity valid or invalid. Valid documents are
invoices, simplified invoices/fiscal receipts, and rectifying invoices. Invalid
documents include proformas, pretickets, comandas, cuentas de mesa,
card-terminal/payment slips without an invoice, non-invoice documents, and
documents too unreadable to establish that they are invoices. If the intended
document contains the standalone printed word proforma, preticket, or comanda,
or the printed phrase cuenta de mesa, return validity invalid and
diagnostic_type exactly Proforma. Recognize them regardless of case and ordinary
punctuation or whitespace, but not inside longer words. Do not assign this
diagnosis from an invoice-like
layout or from words on an unrelated document in the image. This rule applies
even if the intended document otherwise looks like an invoice. Never return
review. For other documents, diagnostic_type is a short optional informative
label and does not need to use a fixed vocabulary. Classification must not stop
extraction: return every readable field even for invalid documents.
"""

INVOICE_FISCAL_AND_IDENTITY_RULES = """Create one lineas_iva item for each printed VAT breakdown row. Create one
recargos_equivalencia item for each explicitly printed RE breakdown; never put
IVA values in RE fields. Return empty arrays when those concepts are absent.
Return retencion_irpf only when IRPF is explicitly printed, preserve a printed
negative sign in cuota_irpf, and otherwise return null. Do not infer IRPF from
the supplier type. Do not create fiscal objects whose every field is null.
When several identifiers are present, prefer an explicitly labelled invoice or
simplified-invoice number. Use a ticket, operation, transaction, reference or
receipt number only when the context identifies it as this document's number;
do not confuse it with a card payment authorization, terminal, order or item ID.
Prioritize the supplier's printed tax ID, preserving its letters and leading
zeroes, even if the company name is unclear. Do not derive a tax ID from a name,
or a name from a tax ID. Prefer the printed legal name associated with that tax
ID over a commercial brand when both appear. Never use the customer's tax ID
or name for the supplier, and do not add a company suffix that is not printed.
"""

# Both structured parsers receive these rules in the same order.
INVOICE_RULES = (
    INVOICE_EXTRACTION_RULES
    + "\n"
    + INVOICE_CLASSIFICATION_RULES
    + "\n"
    + INVOICE_FISCAL_AND_IDENTITY_RULES
    + INVOICE_LABEL_HINTS
)
