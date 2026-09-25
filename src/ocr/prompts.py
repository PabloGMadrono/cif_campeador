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

INVOICE_CLASSIFICATION_RULES = """Clasifica cada documento como valid o
invalid. Son documentos valid las facturas, facturas simplificadas o tickets
fiscales y facturas rectificativas. Son invalid los documentos provisionales,
los resguardos de pago de datáfono sin factura, otros documentos que no sean
facturas y los documentos demasiado ilegibles para establecer que lo son.
Nunca devuelvas review.

Determina la función del documento principal a partir del encabezado, las
expresiones destacadas, su contexto y el significado de sus identificadores.
No decidas por una palabra aislada. Tolera errores habituales de OCR,
variaciones de mayúsculas, tildes, espacios y puntuación. No utilices para
clasificar texto perteneciente a otro documento visible en la imagen.

DOCUMENTOS PROVISIONALES — diagnostic_type exactamente "Proforma":
Clasifica como invalid el documento principal cuando se presente como
factura proforma, pro-forma, prefactura, borrador de factura, preticket,
pre-ticket, ticket provisional, precuenta, cuenta provisional, comanda
o cuenta de mesa.

También son indicios expresiones como «sin validez fiscal»,
«documento no fiscal», «no válido como factura», «pendiente de facturar»
o «solicite su factura», siempre que describan el documento principal.

Una cuenta provisional de restaurante puede incluir establecimiento,
fecha, consumiciones, total e incluso IVA. Esos datos no la convierten
en factura definitiva. Un número de mesa, pedido, comanda o cuenta no
equivale a un número de factura.

No asignes "Proforma" a una factura definitiva por mencionar una
proforma anterior, ni por incluir la mesa o la comanda como referencias
internas. La ausencia de un número de factura refuerza otros indicios
de provisionalidad, pero por sí sola no demuestra que sea una proforma:
el OCR podría haber omitido ese número.

RESGUARDOS DE DATÁFONO — diagnostic_type exactamente
"Resguardo de datáfono":
Clasifica como invalid el documento principal cuando registre
principalmente una transacción con tarjeta, en vez de documentar la
venta de bienes o servicios.

Busca una combinación coherente de indicios: «copia cliente»,
«resguardo TPV», «operación autorizada», «pago aceptado», contactless,
número de tarjeta enmascarado, marca de tarjeta, código de autorización,
referencia de pago, identificación del terminal o TPV, entidad bancaria
e importe cobrado. Suele mostrar fecha, hora e importe,
pero no conceptos comprados, base imponible, tipo ni cuota de IVA.
La ausencia de estos datos fiscales refuerza el diagnóstico; no
basta por sí sola para determinarlo.

El código de autorización, el número del terminal y la referencia
bancaria no son números de factura. Ningún indicio de pago aislado basta
para clasificar el documento como resguardo: una factura también puede
indicar que se pagó con tarjeta.

Si aparecen juntos una factura y su resguardo de TPV, clasifica la
factura cuando su sección sea identificable. Si aparecen juntos una
cuenta provisional y su resguardo de TPV, clasifica el documento
principal como "Proforma". No confundas «ticket» con «preticket»:
un ticket puede ser una factura simplificada.

Si falta el número de factura y el documento es suficientemente legible
para concluir que realmente no figura, clasifícalo como invalid con
otro diagnostic_type informativo; no lo etiquetes falsamente como
"Proforma" ni como "Resguardo de datáfono". Para otros documentos
invalid, diagnostic_type puede ser una etiqueta breve y descriptiva.

La clasificación no detiene la extracción: devuelve todos los campos
legibles, también cuando el documento sea invalid.
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

