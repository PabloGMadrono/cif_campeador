"""Invoice prompts: shared business rules plus input-specific instructions.

Edit INVOICE_CLASSIFICATION_RULES for valid/invalid decisions,
INVOICE_EXTRACTION_RULES and INVOICE_FISCAL_AND_IDENTITY_RULES for fields,
and the relevant wrapper for image or block-specific behavior. The text-only
parser is retained for backends that transcribe before parsing.
"""


INVOICE_EXTRACTION_RULES = """Devuelve null para los campos ausentes o
ilegibles; no adivines. Extrae el nombre y el identificador fiscal del
proveedor, no los del cliente. Conserva literalmente los identificadores
de factura, incluidos ceros iniciales y signos de puntuación, así como
los nombres impresos del proveedor.

Devuelve las fechas en formato YYYY-MM-DD cuando su interpretación sea
inequívoca. Devuelve los importes como cadenas decimales en euros, sin
símbolo de moneda ni separadores de miles. Expresa los tipos impositivos
en puntos porcentuales: 21 para 21 %, no 0.21.

Utiliza únicamente valores impresos. No calcules, sumes, agregues ni
corrijas importes o porcentajes.
"""
INVOICE_CLASSIFICATION_RULES = """Clasifica el documento principal como valid o invalid.
Son valid las facturas, facturas simplificadas, tickets fiscales y
facturas rectificativas. Son invalid los documentos provisionales,
los resguardos de datáfono sin factura, los documentos que no sean
facturas y los demasiado ilegibles para reconocer su naturaleza.
Nunca devuelvas review. No clasifiques por una palabra aislada.

- Provisional → invalid; diagnostic_type exactamente "Proforma".
  Reconoce factura proforma, pro-forma, prefactura, borrador, preticket,
  pre-ticket, comanda, precuenta, cuenta de mesa y cuenta provisional.
  «Sin validez fiscal» o «solicite su factura» son indicios si describen
  el documento actual. Estos documentos pueden mostrar conceptos,
  total e IVA. No clasifiques como Proforma una factura definitiva
  que solo haga referencia a una proforma, mesa o comanda anterior.

- Resguardo de datáfono → invalid; diagnostic_type exactamente
  "Resguardo de datáfono". Reconócelo cuando el documento registre
  principalmente un pago con tarjeta: Visa, Mastercard, copia cliente,
  operación aprobada o autorizada, código de autorización, tarjeta
  enmascarada, terminal/TPV, referencia bancaria e importe cobrado.
  Valora el conjunto: «pagado con tarjeta» dentro de una factura no
  convierte la factura en resguardo. Si aparecen juntos una factura y
  su resguardo, clasifica la factura; si aparecen una cuenta provisional
  y su resguardo, clasifica la cuenta como Proforma.

- Datos fiscales insuficientes → invalid; diagnostic_type
  "Datos fiscales insuficientes". Aplica esta regla si un documento
  legible se presenta como factura simplificada, afirma «IVA incluido»
  y no muestra NI identificador fiscal del emisor (NIF/DNI/NIE) NI
  porcentaje de IVA. «IVA incluido» no indica el porcentaje. No
  confundas datos realmente ausentes con texto cortado o ilegible.

- Si casi no hay texto reconocible (no puedes identificar num de factura ni cif), usa validity "invalid" y diagnostic_type 
"ilegible". No uses este diagnóstico cuando el documento se entiende pero le
 faltan campos.

La falta de datos del comprador o de cuota de IVA separada no activa
por sí sola ninguna de estas tres reglas. Un número de mesa, pedido
o autorización bancaria no equivale a un número de factura. Si el
documento claramente carece de número de factura, clasifícalo como
invalid con otro diagnostic_type descriptivo, nunca como Proforma
solo por esa ausencia.

Tolera variaciones de mayúsculas, tildes, espacios y errores de OCR.
Para otros documentos invalid, usa un diagnostic_type breve y
descriptivo. Clasificar como invalid no detiene la extracción:
devuelve todos los campos legibles.
"""

INVOICE_FISCAL_AND_IDENTITY_RULES = """Crea un elemento en lineas_iva
por cada fila de desglose de IVA impresa. Crea un elemento en
recargos_equivalencia por cada desglose de RE expresamente impreso;
nunca coloques valores de IVA en campos de RE. Devuelve listas vacías
cuando no aparezcan esos conceptos.

Devuelve retencion_irpf únicamente si el IRPF está impreso. Conserva
el signo negativo de cuota_irpf si aparece; en caso contrario, devuelve
null. No infieras una retención por el tipo de proveedor. No crees
objetos fiscales en los que todos los campos sean null.

Cuando haya varios identificadores, prioriza el número expresamente
etiquetado como número de factura o de factura simplificada. Usa un
número de ticket, operación, transacción, referencia o recibo únicamente
si el contexto permite reconocerlo como número del documento fiscal.
No lo confundas con una autorización de tarjeta, un terminal, un
pedido, una mesa, una comanda o un identificador de artículo.

Prioriza el identificador fiscal impreso del proveedor y conserva sus
letras y ceros iniciales, incluso cuando el nombre de la empresa no esté
claro. No deduzcas un identificador fiscal a partir de un nombre, ni
un nombre a partir de un identificador fiscal. Si aparecen una marca
comercial y una razón social asociada al identificador fiscal, prioriza
la razón social impresa. Nunca atribuyas al proveedor el nombre o
identificador fiscal del cliente, ni añadas una terminación societaria
que no esté impresa.
"""

INVOICE_LABEL_HINTS = """Terminología que puede aparecer en facturas y tickets
españoles. Estas expresiones ayudan a reconocer campos: no son valores que
debas inventar ni sustituir por el texto impreso.

- Número de factura (numero_factura): Nº de factura, Núm. factura,
  Factura simplificada, F.S., Ticket, Nº ticket, Operación, Op.,
  Nº operación, Transacción, Referencia, Ref., Recibo, Nº recibo, Nº, #.
  Las etiquetas genéricas solo identifican el número de factura si el
  contexto del documento lo confirma. Un número de autorización bancaria,
  terminal, mesa, pedido o comanda no es un número de factura.

- Identificador fiscal del proveedor (nif_proveedor, también llamado CIF):
  CIF, NIF, DNI, NIE, NIF-IVA, VAT, VAT Number. Si el proveedor es autónomo,
  puede aparecer un DNI con letra final o un NIE. También puede aparecer
  sin etiqueta, inmediatamente debajo del nombre de la empresa. Presta
  especial atención a este identificador para distinguir al proveedor
  legal de la marca comercial del establecimiento.

- Nombre legal del proveedor (nombre_proveedor): Razón social, Titular,
  Empresa, Expedido por. Entre las terminaciones posibles figuran S.L.,
  S.L.U., S.A., S.A.U., S.C.P. y S.Coop. Un autónomo puede identificarse
  mediante nombre y apellidos, sin terminación societaria.
"""


# Ambos parsers estructurados reciben las reglas en el mismo orden.
INVOICE_RULES = (
    INVOICE_EXTRACTION_RULES
    + "\n"
    + INVOICE_CLASSIFICATION_RULES
    + "\n"
    + INVOICE_FISCAL_AND_IDENTITY_RULES
    + "\n"
    + INVOICE_LABEL_HINTS
)

