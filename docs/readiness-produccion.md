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
| `TAECEL_REGISTERED` | **TRUE** | Cuenta creada y activa. Titular comercial/fiscal: **Johany Josefina Palomino Carrillo**. |
| `TAECEL_API_REQUESTED` | **TRUE** | Ticket de integración API / Web Service enviado a Soporte TAECEL. |
| `TAECEL_TECH_SURVEY_SENT` | **TRUE** | Levantamiento tecnológico entregado. |
| `TAECEL_PENDING_TEST_CREDENTIALS` | **TRUE** | **Confirmado por Soporte:** la revisión y entrega de credenciales de prueba tarda **tiempo variable según su carga de trabajo**. No hay SLA. |
| `TAECEL_CREDENTIALS_RECEIVED` | FALSE | — |
| `TAECEL_FUNDING_MINIMUM` | **$5,000 MXN** | **Confirmado por Soporte:** las claves productivas exigen una primera compra/fondeo desde $5,000. **No fondear todavía** — va después de aprobar las pruebas. |
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

### 1. TAECEL — acceso API (ticket ya enviado, esperando respuesta)

El flujo es: levantamiento tecnológico → revisión de un ingeniero suyo →
credenciales de prueba → verificación → credenciales de producción.

**Estado: ticket enviado a Soporte.** Lo que falta ya no está de nuestro lado:
es su revisión de ingeniería. **No publican SLA para ninguno de los dos pasos
humanos**; el "máximo 24 horas" de su sitio corresponde a *distribuidor de red
de afiliados*, que es otro producto.

### El orden, confirmado por TAECEL

Nada se puede adelantar. Cada flecha es un paso que depende del anterior, y
dos de ellas son revisiones humanas de TAECEL sin plazo publicado:

```
cuenta ✓ → levantamiento ✓ → credenciales TEST ⏳ → integración → pruebas
  → envío de resultados → revisión TAECEL → fondeo mínimo $5,000
  → claves PRODUCTION → mapping productivo → primera recarga real
```

**Implicación de calendario:** el fondeo de $5,000 **no** es el primer paso,
es el penúltimo. Va *después* de que TAECEL apruebe los resultados de las
pruebas. Con dos revisiones humanas sin SLA en el camino, la primera recarga
real está a semanas, no a días. Hoy el cuello de botella está enteramente en
su lado.

Lo que tiene que llegar de ellos, y sin lo cual nada avanza:

1. La documentación del web service (URL base, rutas, parámetros, códigos de
   error). **Nada de eso es público** — ver «Las tres cerraduras» abajo.
2. `TAECEL_KEY` y `TAECEL_NIP` de pruebas.
3. **La comisión que nos conceden.** No la publican, y es el dato que decide
   si una recarga con tarjeta gana o pierde dinero. Ver la tabla de abajo:
   el punto de equilibrio está en **7.42%** para una recarga de $100.

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

1. `SOLO_EFECTIVO` — recargas solo en efectivo; tarjeta para otros productos.
2. `CUOTA_UNIFORME` — una cuota de servicio **idéntica en todos los métodos de
   pago**. No es un recargo por tarjeta porque quien paga en efectivo paga
   exactamente lo mismo, y por eso no está prohibida.
3. `ABSORBER` — el negocio se come el costo.

**Pendiente de decisión.** El motor de precios soporta las tres sin cambiar
código y **no elige por su cuenta**: sin política configurada no cotiza.

### La tabla que responde la pregunta

Calculada con el motor (`samy_common/pricing.py`, 31 pruebas en verde) sobre
la tarifa real de Conekta (3.4% + $3 + IVA). «Cuota» es la cuota uniforme
mínima con la que la venta **no pierde dinero al cobrarse con tarjeta**:

| Comisión TAECEL | $100 | $200 | $500 |
|---|---|---|---|
| 3% | $4.61 | $5.60 | $8.55 |
| 4% | $3.58 | $3.51 | $3.34 |
| 5% | $2.53 | $1.43 | **$0** (+$1.80) |
| 6% | $1.50 | **$0** (+$0.63) | **$0** (+$6.80) |
| **7.42%** | **$0** (equilibrio exacto) | **$0** (+$3.47) | **$0** (+$13.90) |
| 10% | **$0** (+$2.57) | **$0** (+$8.63) | **$0** (+$26.80) |

Tres lecturas que cambian la decisión:

- **El punto de equilibrio es 7.42% a $100.** Por encima de eso no hace falta
  cuota ninguna: la tarjeta ya es rentable sola.
- **Las denominaciones grandes perdonan mucho más.** A $500 basta con ~4.6%.
  Si la comisión de TAECEL queda entre 5% y 7%, una salida es vender $100 solo
  en efectivo y aceptar tarjeta desde $200.
- **La cuota también paga comisión.** Subirla un peso no deja un peso de
  margen, porque Conekta cobra su porcentaje sobre el total. Por eso la cuota
  de equilibrio no es «el costo»: se resuelve, no se suma.

Con cuota uniforme el cliente paga lo mismo en ambos métodos y lo que cambia
es el margen: con 5% y cuota de $2.53, el cliente paga $102.53 siempre, y el
negocio gana $7.53 en efectivo y $0.00 con tarjeta.

**Nada de esto se puede cerrar hasta que TAECEL diga su porcentaje.** Es el
tercer dato de la lista del bloqueo 1, y conviene pedirlo en el mismo hilo.

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
| Motor de precios / `Cotizacion` | ✅ **Hecho.** `samy_common/pricing.py`, módulo puro, 31 pruebas. Falta que Sizú elija la política y que TAECEL diga su comisión. |
| CSP en modo `enforce` | ⚠️ Ya estaba en enforce, y Alpine estaba muerto bajo ella. **El flujo de cobro ya está migrado y verificado (4 de 9 plantillas).** Quedan 5, ninguna en el camino del dinero. Ver abajo. |
| Idempotencia de extremo a extremo | ✅ **Hecha.** Clave por operación, no por petición. Ver abajo. |
| Control de saldo antes de cobrar | ✅ **Hecho.** En `create_fulfillment`, antes de que exista la orden. |
| Conciliación de pendientes | ✅ **Hecha, y corregía un bug que reembolsaba de más.** Ver abajo. |
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

## CSP: Alpine está muerto bajo la política real

La CSP de producción **ya estaba en modo enforce** y sin `unsafe-eval`
(`script-src 'self' https://pay.conekta.com`). Alpine.js evalúa sus
expresiones construyendo funciones a partir de cadenas, que es exactamente lo
que `unsafe-eval` habilita. Resultado: **cada directiva de Alpine lanza
`EvalError` y el control queda muerto.**

Lo peligroso es el síntoma. La pantalla se pinta completa, sin error visible
ni hueco en el diseño; los botones simplemente no hacen nada. El cajero no
tiene forma de notarlo.

No se dedujo leyendo el código: se reprodujo el 13/09/2026 con
`CSP_ENFORCE=True` en un navegador real. Las seis directivas de la pantalla de
login lanzaron `EvalError` y el botón de mostrar contraseña quedó inerte.

### `CSP_ENFORCE`, la mitad que faltaba

El modo solo-reporte avisa de lo que producción *bloquearía*, pero la pantalla
sigue funcionando, así que nadie ve el síntoma. `CSP_ENFORCE=True` aplica la
política de verdad en desarrollo. **Queda en `True`** mientras haya plantillas
sin migrar: es preferible verlas roto aquí que descubrirlo en el mostrador.
Se puede poner en `False` para una demo.

### La decisión: JS modular propio, no `@alpinejs/csp`

El build `@alpinejs/csp` tampoco admite expresiones en línea, así que obliga a
reescribir las mismas plantillas y encima añade una dependencia cuyos fallos
vuelven a ser silenciosos. Y al ver el inventario, lo que se usaba de Alpine
eran tres interruptores de mostrar/ocultar: no había framework del que
aprovecharse.

`core/static/js/ui.js` implementa los controles con atributos `data-*`, sin
evaluación de cadenas y sin JavaScript en línea. Es mejora progresiva: sin
JavaScript la contraseña sigue siendo un campo usable. Se reinicializa en
`htmx:afterSwap`, porque el HTML que htmx inyecta llega sin inicializar y
quedaría muerto — el mismo síntoma por otra vía.

### Migrado y verificado (4 de 9) — **el flujo de cobro completo**

| Plantilla | Qué estaba muerto | Verificación |
|---|---|---|
| `accounts/login.html` | Mostrar/ocultar contraseña. | Navegador: tipo de campo, `aria-label`, `aria-pressed` y los dos iconos alternan; cero `EvalError`. |
| `base.html` | El aviso no se podía cerrar ni se cerraba solo. | Migrado a `data-toast`. |
| `operations/pay.html` | **El panel de cobro en efectivo no se abría.** El método de pago quedaba inaccesible. | Migrado a `data-collapsible`. |
| `operations/card.html` | **«Cobrando…», el mensaje de error y el aviso de respuesta indeterminada no se mostraban nunca.** | Navegador: el estado de fallo se pinta con su texto real; cero `EvalError`. |

`card.html` era el peor de los cuatro. Los tres paneles dependían de `x-show`,
así que bajo la CSP el cajero podía cobrar **sin ver confirmación, sin ver el
error de la tarjeta y sin ver el aviso de «el cobro pudo haberse realizado»** —
justo el aviso que existe para que no cobre dos veces.

`conekta-tokenizer.js` se reescribió sin Alpine conservando intactas las
garantías del cobro: límite de 45 s (mayor que los 30 s + 20 s del servidor),
`AbortController`, **cero reintentos automáticos**, y el estado indeterminado
que manda al comprobante en vez de ofrecer reintentar. Además los cuatro
estados ahora se pintan desde un solo sitio y son excluyentes; antes eran tres
banderas independientes y nada impedía que «Cobrando…» y un error se mostraran
a la vez.

### Falta (5 plantillas, 39 directivas) — ninguna en el flujo de cobro

Alpine sigue cargado porque estas lo usan, y **bajo la CSP real siguen
muertas, igual que hoy en producción**:

| Plantilla | Directivas | Nota |
|---|---|---|
| `tools/scanner_check.html` | 20 | Herramienta de diagnóstico, no flujo de venta. |
| `billpay/reference.html` | 11 | Pago de servicios, que sigue pendiente de contrato. |
| `accounts/signup_account.html` | 6 | Alta de cuenta. |
| `operations/receipt.html` | 1 | |
| `accounts/_signup_shell.html` | 1 | Además un `<style>` en línea muerto. |

El `<script>` de Alpine se quita cuando caiga la última.

### Residuo conocido e inofensivo

Queda una violación de `style-src` en consola: el `<style>` que **htmx se
inyecta a sí mismo** para el indicador de carga. Se comprobó que no causa
ningún defecto visual — `app.css` ya trae las reglas de `.htmx-indicator` y un
indicador real computa `display: none`.

---

## Lo que no se ha hecho, a propósito

- Ninguna compra de saldo.
- Ninguna recarga, ni de prueba contra un teléfono real.
- Ningún cobro con tarjeta real.
- Ningún pago, transferencia, contratación ni aceptación legal a nombre de Sizú.
- Ningún secreto escrito en el repositorio.
