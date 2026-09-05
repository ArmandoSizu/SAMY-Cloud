# Microservicio de Recargas Telefónicas

## El hallazgo que define el diseño

**Telcel, AT&T, Movistar y Unefon no ofrecen API pública.** La página de distribuidores
de Telcel solo tiene un formulario de contacto; no existe documentación técnica pública.

**Consecuencia:** toda recarga se vende obligatoriamente a través de un distribuidor o
agregador autorizado. Por eso `TopupProvider` es una abstracción y no un cliente de
Telcel: cambiar de distribuidor será escribir un adaptador, no reescribir el servicio.

Detalle completo en [`api-integrations.md`](api-integrations.md).

---

## El catálogo NO se codifica

Requisito literal: *"No hardcodees catálogos como si fueran eternos. NO debes permitir
arbitrariamente $75, $100, $150 si el proveedor no ofrece esa denominación."*

Cómo se cumple:

- La tabla se llena **exclusivamente** desde `provider.fetch_catalog()`.
- No hay migración con denominaciones, ni lista en el código, ni valores por defecto.
- Si nunca se ha sincronizado, la pantalla muestra un estado vacío que **explica que
  falta configurar el proveedor** — no denominaciones inventadas.
- Un producto que desaparece del catálogo se marca **inactivo, no se borra**: las órdenes
  históricas lo referencian y borrarlo dejaría comprobantes sin explicación.
- `last_seen_at` permite advertir cuando el catálogo está viejo, en vez de vender a ciegas.

La sincronización completa corre en una transacción: si falla a la mitad, el catálogo
anterior queda intacto. Es preferible operar con el catálogo de ayer que con medio
catálogo de hoy.

---

## Validación del número telefónico

Reglas del plan de numeración mexicano (IFT) implementadas en `samy_common/phone.py`:

- 10 dígitos: lada (2 o 3) + número local.
- Ladas de 2 dígitos: **solo** 55 (Valle de México), 33 (Guadalajara), 81 (Monterrey).
- Se aceptan las formas en que la gente realmente escribe: `5512345678`,
  `(55) 1234-5678`, `+52 55 1234 5678`, `0445512345678`, `015512345678`.

### Lo que deliberadamente NO se hace

**No se deduce el operador a partir del prefijo.** La portabilidad numérica existe en
México desde 2008: un número que "parece Telcel" puede estar en Movistar. Deducirlo del
prefijo produciría recargas enviadas a la compañía equivocada.

El operador lo elige el cajero y, cuando el proveedor ofrece consulta de portabilidad, se
valida contra su API **antes de cobrar**.

---

## Flujo

```
1. Cajero pulsa Recargas
2. Catálogo real de operadores (del proveedor, no inventado)
3. Selecciona compañía
4. Captura número → validación de formato en el SERVIDOR
5. Selecciona denominación o paquete (solo los que el proveedor vende)
6. Confirmación: compañía, número enmascarado, producto, precio, comisión, total
7. Se crea la orden en el servicio de Pagos
8. Se cobra
9. ── El proveedor de pago confirma ──►  orden PAID
10. Topups VERIFICA con Pagos que la orden esté realmente pagada
11. Se envía la recarga al proveedor
12. El proveedor confirma
13. Comprobante con el folio del operador
```

Los pasos 9-10 son el corazón: **el evento no es prueba de pago**, solo un aviso de
"revisa esta orden". La comprobación la hace `_order_is_paid()` con una consulta firmada
al servicio de Pagos. Ante cualquier duda devuelve `False` y **no se recarga**: una
recarga no ejecutada se reintenta; una recarga regalada no se recupera.

---

## Manejo de resultados

| Respuesta del proveedor | Qué se hace |
|---|---|
| `SUCCEEDED` | `SUCCEEDED`, se notifica a Pagos, comprobante |
| `FAILED` | `FAILED` → Pagos dispara **reembolso automático** |
| `PENDING` | Se deja en `SENT`; la conciliación consulta después |
| Timeout tras enviar | `UNDER_REVIEW`. **NUNCA se reintenta a ciegas** |

El último caso es el importante: si la recarga ya se aplicó, un reintento la duplica y
ese dinero no se recupera. La conciliación consulta el estado real por la clave de
idempotencia antes de decidir.

---

## Proveedores

| Proveedor | Sandbox self-service | Estado |
|---|---|---|
| **Reloadly** | ✅ Sí, gratis, mismo día | 🔧 Adaptador listo, faltan llaves |
| **Taecel** | ❌ Requiere contrato | ⏸ `PENDING_CONTRACT` |

`taecel.py` existe y **no llama a endpoints supuestos**: sus métodos levantan
`ProviderNotConfigured` con la lista exacta de lo que falta. Escribir un cliente contra
una API cuya documentación no se ha leído produce código que compila, pasa pruebas con
respuestas inventadas y falla el día que se conecta de verdad.
