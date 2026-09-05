# Microservicio de Pago de Servicios

## Los dos hallazgos que definen el diseño

**1. CFE no tiene API pública.** Ni de consulta de adeudo ni de pago. No existe portal
para desarrolladores de CFE. Toda integración pasa obligatoriamente por un agregador con
convenio.

**2. CAPDAM es de Manzanillo, Colima** — no de Durango, como suele asumirse. (El
organismo de Durango es AMD.) Su sitio no expone ninguna API y bloquea el acceso
automatizado.

Los organismos operadores municipales de agua en México casi nunca exponen API. La vía
real es siempre un agregador que ya tenga el convenio firmado.

Detalle y fuentes en [`api-integrations.md`](api-integrations.md).

---

## Lo que NO se va a hacer

**Scraping del portal de CFE.** Es frágil, viola sus términos de servicio y —lo decisivo
para este sistema— **no da confirmación fiable de pago**. Un sistema que mueve dinero
necesita una respuesta autoritativa del proveedor, no el HTML de una página que puede
cambiar mañana sin aviso.

---

## El código de barras del recibo

**Lo único verificable públicamente:** el número de servicio de CFE tiene **12 dígitos**.

**No es verificable públicamente:** la simbología exacta (¿CODE128? ¿Interleaved 2 of 5?),
la longitud total del código, ni la estructura de sus campos. No existe especificación
oficial abierta.

### La decisión que se tomó

`ReferenceFormat` guarda la especificación como **configuración por biller**, no como
código:

| Campo | Para qué |
|---|---|
| `validation_regex` | Vacío = no hay especificación; solo se valida longitud |
| `barcode_formats` | Simbologías que puede traer ese recibo |
| `barcode_extraction_regex` | Cómo extraer la referencia si el código trae más datos |
| `specification_source` | **De dónde salió.** Si dice "pendiente", nadie debe asumir que está verificada |

**No se escribió un parser del recibo de CFE.** Inventarlo produciría un parser que parece
funcionar, pasa pruebas con datos inventados, y falla con recibos reales recortando mal la
referencia — es decir, **pagándole el recibo a otra persona**. La especificación debe
pedirse al agregador como parte del due diligence.

---

## Flujo (cuando haya agregador)

```
1. Cajero pulsa Pago de servicios
2. Selecciona el servicio (CFE, agua…)
3. Elige: "Escanear código" o "Capturar manualmente"
   └─ Escanear: cámara → enfoque → detección → se apaga la cámara → validación
4. Se valida el formato de la referencia (usabilidad, no garantía)
5. Se consulta el adeudo al agregador       ← la validación que SÍ cuenta
6. Pantalla de confirmación: titular, periodo, monto, vencimiento
7. Se crea la orden y se cobra
8. ── El proveedor de pago confirma ──►  orden PAID
9. Billpay VERIFICA con Pagos que esté realmente pagada
10. Se envía el pago al agregador
11. El agregador confirma
12. Comprobante con el folio del biller
```

La consulta del paso 5 tiene vigencia (`INQUIRY_TTL_SECONDS`, 5 min por defecto): un
recibo consultado hace media hora pudo haberse pagado en otra ventanilla.

`found=False` es un resultado **válido y definitivo**, distinto de un error de red: se le
dice al cajero que revise la referencia, no que reintente.

---

## Estado

| Agregador | Docs públicas | Sandbox | Estado |
|---|---|---|---|
| **tapi** | ✅ `developers.tapila.cloud` | ❌ Bajo contrato | ⏸ `PENDING_CONTRACT` |
| Taecel | ❌ | ❌ | Alternativa |
| Sivetel | ❌ | ❌ | Alternativa |
| Arcus (Mastercard) | Indexadas, contenido no verificable | ❌ | Alternativa |

`tapi.py` existe y sus métodos levantan `ProviderNotConfigured` con los requisitos exactos.

### Qué falta

1. Contacto comercial con tapi (o Taecel / Sivetel / Arcus)
2. Contrato firmado y alta como comercio (RFC, acta constitutiva)
3. Documentación oficial de su API
4. Credenciales de sandbox y producción
5. Fondeo prepagado
6. **Especificación del código de barras del recibo de CFE**
