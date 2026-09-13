# Readiness de producción — SAMY Cloud

**Última actualización: domingo 13 de septiembre de 2026.**

Este documento es el único lugar donde vive el estado de preparación para
cobrar y entregar dinero real. Si una afirmación de este archivo contradice a
otro documento, gana este.

Regla de lectura: **una casilla solo se marca cuando está verificada contra la
fuente**, no cuando está implementada. "El adaptador está escrito" y "la
integración funciona" son dos cosas distintas, y confundirlas es exactamente
lo que este archivo existe para evitar.

---

## Estado de las banderas

| Bandera | Valor | Verificado |
|---|---|---|
| `TAECEL_REGISTERED` | **TRUE** | Cuenta creada y activa. Titular: quien será el titular fiscal/comercial de la integración. |
| `TAECEL_API_REQUESTED` | **PENDING** | Levantamiento tecnológico a `cc@taecel.com`. |
| `TAECEL_CREDENTIALS_RECEIVED` | FALSE | — |
| `TAECEL_CONTRACT_VERIFIED` | FALSE | Nadie ha leído todavía la documentación real de su web service. |
| `TAECEL_FUNDED` | FALSE | No se ha comprado saldo. |
| `CONEKTA_SANDBOX` | TRUE | Operando. Órdenes y webhooks reales de sandbox. |
| `CONEKTA_PRODUCTION` | FALSE | KYC sin completar. |
| `RELOADLY_SANDBOX` | TRUE | `READY`. Saldo de prueba disponible. |
| `RELOADLY_PRODUCTION` | FALSE | No solicitado. |
| `PRIMERA_RECARGA_REAL` | **NO** | Ninguna operación monetaria ejecutada. |

**Productos comerciales vendibles hoy: 0 de 68.** Todos bloqueados por
`PROVIDER_NOT_MAPPED`: existe el catálogo oficial verificado, no existe un
mapping de proveedor aprobado. Eso es correcto, no es un defecto.

---

## Lo que bloquea la primera venta real, y quién lo bloquea

Los dos bloqueos son de terceros. Ninguno se resuelve escribiendo código.

### 1. TAECEL — acceso API

El flujo es: levantamiento tecnológico → revisión de un ingeniero suyo →
credenciales de prueba → verificación → credenciales de producción.

**No publican SLA para ninguno de los dos pasos humanos.** El "máximo 24
horas" que aparece en su sitio corresponde a *distribuidor de red de
afiliados*, que es otro producto.

Correo oficial confirmado: `cc@taecel.com`. (Su PDF de levantamiento dice
`integraciones@taecel.com`; sus dos documentos se contradicen.)

### 2. Conekta — producción

Validación declarada de **2 días hábiles**, más primer depósito de hasta 10
días hábiles. Requiere KYC completo: identificación, comprobante de domicilio
≤3 meses, constancia fiscal ≤3 meses, CLABE **a nombre de la razón social**,
acta constitutiva si es persona moral, y un sitio con términos y condiciones,
aviso de privacidad y política de reembolsos.

El sandbox **no puede** crear órdenes ni cargos reales. No hay atajo.

### 3. Restricción comercial que cambia el modelo de negocio

Los Términos de Conekta (Suplemento de Tarjetas → Prohibiciones) **prohíben el
recargo por pagar con tarjeta**: cobrar comisiones al cliente por pagar con
débito o crédito, o añadir cargos al precio por aceptar pagos en su
plataforma. Violarlo causa suspensión de cuenta.

Consecuencia aritmética: una recarga de $100 con tarjeta cuesta **$7.42** en
comisión de Conekta (3.4% + $3 + IVA). La comisión que paga TAECEL **no está
publicada** ("pregúntanos por el porcentaje"). Si esa comisión es menor a
7.42%, cada recarga con tarjeta pierde dinero.

Tres salidas legítimas, todas decisión de Sizú:

1. Recargas solo en efectivo; tarjeta para otros productos.
2. Una cuota de servicio **uniforme en todos los métodos de pago** (no es un
   recargo por tarjeta, y por eso no está prohibida).
3. Absorber el costo.

**Pendiente de decisión.** El motor de precios está construido para soportar
las tres sin cambiar código, pero no elige por su cuenta.

---

## Qué está listo del lado nuestro

Verificado el 13/09/2026 con los 10 contenedores arriba y las cuatro suites en
verde (core 70 · payments 52 · topups 88 · samy_common 162).

- Catálogo comercial separado del catálogo técnico del proveedor. 68 productos
  de oferta oficial verificada, con fuente y folio IFT donde existe.
- La regla `OFICIAL != EJECUTABLE`: el estado vendible se **calcula**, no se
  guarda. Un proveedor caído deja de vender sin que nadie toque la base.
- Router sin failover. Un reintento exige el proveedor original; si ese no
  sirve, se detiene y presenta alternativas a una persona.
- Emparejamiento de catálogo por **identidad exacta** (operador + familia +
  SKU + importe). Nunca por precio. Los mappings propuestos nacen
  deshabilitados y en revisión.
- `TaecelProvider` preparado para recibir credenciales sin rehacer
  arquitectura, con tres cerraduras independientes (ver abajo).
- Regla del dinero intacta: primero el pago confirmado, después el servicio.
  Se sostiene por la **ausencia** de transiciones en la máquina de estados, no
  por una validación que alguien pueda olvidar.

---

## Las tres cerraduras de TAECEL

`TaecelProvider.check_health()` devuelve `PENDING_CONTRACT` y toda operación
levanta `ProviderNotConfigured` mientras falte cualquiera de estas:

1. `TAECEL_BASE_URL` — la URL que TAECEL entrega con las credenciales. **No
   tiene valor por omisión** porque no publican ninguna.
2. `TAECEL_KEY` + `TAECEL_NIP`.
3. `TAECEL_CONTRACT_VERIFIED=True` — **firma humana**: alguien leyó el manual
   real y confirmó que las rutas y los campos del adaptador coinciden.

Y una cuarta condición para llegar a `READY`: `TAECEL_PATH_BALANCE`
configurado y con saldo positivo. TAECEL es prepago; vender sin saber el saldo
puede dejar una orden ya cobrada sin recarga.

### Por qué existe la tercera cerradura

TAECEL **no publica documentación técnica**. Se revisaron el 13/09/2026 su
página de integración, su página de API para cadenas comerciales y su folleto
de integrador: ninguno publica una URL base, un endpoint, un nombre de
parámetro ni un código de error.

Los nombres `RequestTXN` y `StatusTXN` y sus campos (`Key`, `NIP`, `Producto`,
`Referencia`, `Monto` / `Key`, `NIP`, `transID`) **no provienen de una fuente
oficial de TAECEL**. Están en el adaptador como valores por omisión
sobreescribibles por entorno, marcados `SIN CONFIRMAR`, y no se envía nada a
ninguno mientras la tercera cerradura esté cerrada.

Lo único verificado de su API: que es REST, que autentica con `Key` y `NIP`, y
que el modelo es de saldo prepagado.

---

## Cuando lleguen las credenciales de prueba

En este orden. Nada de esto requiere cambios de arquitectura.

1. Poner `TAECEL_BASE_URL`, `TAECEL_KEY`, `TAECEL_NIP` en `.env`
   (**nunca en el repositorio**).
2. Leer el manual. Corregir en `apps/providers/taecel.py` lo que difiera:
   `CAMPO_*`, `LLAVES_*`, `ENVIO_FORM_ENCODED`, y las rutas por entorno.
3. Poner `TAECEL_PATH_BALANCE` y `TAECEL_PATH_PRODUCTS`.
4. `TAECEL_CONTRACT_VERIFIED=True`.
5. Verificar salud: debe reportar `READY` con saldo, o explicar por qué no.
6. `manage.py importar_catalogo_proveedor --proveedor taecel --seco`
7. `manage.py importar_catalogo_proveedor --proveedor taecel`
8. Escribir los alias de operador y familia
   (`CommercialOperator.provider_aliases`, `CommercialFamily.provider_aliases`)
   leyendo los nombres reales de su catálogo. **No adivinarlos.**
9. `manage.py emparejar_catalogo_proveedor --proveedor taecel` (en seco) y leer
   el reporte, sobre todo la línea "por precio habría elegido también".
10. `--guardar`, y aprobar a mano en el panel. Prioridad acordada:
    **TELCEL → Amigo Sin Límite → $100, luego $200.**

Solo entonces existe un producto vendible, y sigue siendo en sandbox.

---

## Qué falta del lado nuestro

| Pendiente | Estado |
|---|---|
| Separación SANDBOX/PRODUCTION | ✅ **Hecha.** Ver abajo. |
| Motor de precios / objeto `Quote` | Bloqueado por la decisión de política de comisión (arriba). |
| CSP en modo `enforce` | Pendiente. Alpine 3.14.9 usa `new Function()`; hay que elegir entre `@alpinejs/csp` o quitar Alpine del flujo crítico. |
| Idempotencia de extremo a extremo | Pendiente. |
| Conciliación automática | Pendiente. Hay comando documentado, sin ejecutar. |
| Comprobante productivo | Pendiente. |
| `CENTRO-260905-YKK4` | Orden PAID de $300 sin conciliar. Recomendación escrita; **no ejecutada**. |
| 8 órdenes CREATED abandonadas | Sin limpiar. |
| `billpay` sin pruebas | 0 tests. |

---

## Separación SANDBOX / PRODUCTION

Implementada en `samy_common/providers/environment.py` y aplicada en
`BaseProvider.ensure_ready()`, que es la guardia por la que pasa **toda**
operación que mueve dinero. Está ahí y no en cada adaptador para que un
proveedor nuevo herede la protección sin que nadie tenga que acordarse.

    ENVIRONMENT=production   <->  proveedores en modo PRODUCTION
    cualquier otro ambiente  <->  proveedores en modo SANDBOX

Las dos combinaciones prohibidas son simétricas, y las dos han arruinado
lanzamientos reales:

| Combinación | Qué pasaría | Qué pasa |
|---|---|---|
| producción + sandbox | Se cobra $100 reales, el sandbox responde «éxito», el cliente se va sin recarga. **Parece que funcionó.** | Rechazado. |
| pruebas + producción | La suite gasta saldo real y recarga teléfonos reales, en silencio. | Rechazado. |

Detalles que importan:

- **Fail-closed.** Un `ENVIRONMENT` vacío o desconocido no se interpreta como
  desarrollo: se rechaza. Un servicio que no sabe dónde está no mueve dinero.
  «produccion» en español **no** cuenta como `production`.
- **El valor por omisión es `development`**, así que un despliegue productivo
  que olvide definirlo se niega a usar credenciales de producción, en lugar de
  venderlas por error.
- **`staging` exige sandbox.** Un staging con credenciales reales cobra de
  verdad.
- **El ambiente se revisa ANTES de la salud.** Preguntarle a un proveedor cómo
  está con credenciales productivas ya es autenticarse contra producción.
- `ENVIRONMENT=test` en los cuatro `settings/test.py`. Eso es lo que impide que
  TAECEL en modo `PRODUCTION` corra en la suite.

Verificado el 13/09/2026 en el contenedor en marcha: Reloadly sandbox en
`development` sigue pasando (el golden path no se rompió), y un proveedor en
modo `PRODUCTION` es rechazado con `code=provider_environment_mismatch`.

---

## Lo que no se ha hecho, a propósito

- Ninguna compra de saldo.
- Ninguna recarga, ni de prueba contra un teléfono real.
- Ningún cobro con tarjeta real.
- Ningún pago, transferencia, contratación ni aceptación legal a nombre de Sizú.
- Ningún secreto escrito en el repositorio.
