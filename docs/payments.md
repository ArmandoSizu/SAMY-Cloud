# Microservicio de Pagos y Comisiones

Es el servicio crítico: **el único dueño del dinero**. Recargas y pago de servicios no
saben cobrar; piden una orden aquí y esperan a que quede `PAID`.

---

## 1. La máquina de estados

```
                    ┌─────────┐
                    │ CREATED │
                    └────┬────┘
              ┌──────────┼──────────┐
              ▼          ▼          ▼
     ┌────────────────┐  │    ┌───────────┐
     │PAYMENT_PENDING │  │    │ CANCELLED │  (final)
     └────┬───────┬───┘  │    └───────────┘
          │       │      │
          │       ▼      │
          │  ┌─────────┐ │
          │  │ EXPIRED │ │  (final)
          │  └─────────┘ │
          ▼              ▼
       ┌──────────────────┐
       │       PAID       │  ◄── ÚNICA puerta hacia la ejecución
       └────┬─────────┬───┘
            │         │
            ▼         ▼
   ┌────────────┐  ┌────────────────┐
   │ PROCESSING │  │ REFUND_PENDING │
   └──┬──────┬──┘  └───────┬────────┘
      │      │             ▼
      ▼      ▼        ┌──────────┐
 ┌─────────┐ ┌──────┐ │ REFUNDED │  (final)
 │ SUCCESS │ │FAILED│ └──────────┘
 └─────────┘ └──┬───┘
   (final)      └──► REFUND_PENDING   (cobró y no entregó → devolución automática)

  UNDER_REVIEW ◄── cualquier resultado indeterminado. NUNCA se resuelve adivinando.
```

### La regla, expresada como ausencia

```python
ORDER_TRANSITIONS = {
    OrderState.CREATED:         frozenset({PAYMENT_PENDING, PAID, CANCELLED}),
    OrderState.PAYMENT_PENDING: frozenset({PAID, CANCELLED, EXPIRED, UNDER_REVIEW}),
    OrderState.PAID:            frozenset({PROCESSING, REFUND_PENDING}),
    ...
}
```

**`CREATED` y `PAYMENT_PENDING` no tienen ninguna arista hacia `PROCESSING`.**

Un `if` se puede olvidar en un camino nuevo. La ausencia de una arista no. Cualquier
intento levanta `IllegalTransition` **antes de tocar al proveedor**.

### Por qué `UNDER_REVIEW` existe

Un timeout después de enviar la petición **no es un fallo**: es un resultado desconocido.
El cargo pudo haberse creado. Marcarlo como `FAILED` y dejar que el cajero reintente
produce un doble cobro.

`UNDER_REVIEW` significa "no sé, y no voy a adivinar". Una tarea de conciliación consulta
al proveedor por la clave de idempotencia y resuelve con la respuesta real.

---

## 2. El flujo completo, paso a paso

```
CAJERO                CORE            PAYMENTS          PROVEEDOR       TOPUPS
  │                     │                 │                 │              │
  │ elige recarga       │                 │                 │              │
  ├────────────────────►│ crea fulfillment (PENDING_PAYMENT) ──────────────►│
  │                     │ crea orden ────►│ CREATED                        │
  │                     │                 │ calcula comisión               │
  │◄────────── muestra desglose ──────────┤                                │
  │                     │                 │                                │
  │ confirma            │                 │                                │
  ├────────────────────►│ inicia cobro ──►│ PAYMENT_PENDING                │
  │                     │                 ├────────────────►│              │
  │                     │                 │◄──── checkout / QR ────────────┤
  │◄────── muestra QR o cobra efectivo ───┤                                │
  │                     │                 │                                │
  │ cliente paga        │                 │◄═══ webhook FIRMADO ═══════════┤
  │                     │                 │ verifica firma                 │
  │                     │                 │ PAID  ← única puerta           │
  │                     │                 │ escribe outbox (misma transacción)
  │                     │                 │                                │
  │                     │                 │ ─── order.paid ───────────────►│
  │                     │                 │                    verifica el pago
  │                     │                 │◄─── consulta estado orden ─────┤
  │                     │                 │                    ejecuta recarga
  │                     │                 │◄── fulfillment.result ─────────┤
  │                     │                 │ SUCCESS                        │
  │◄────── comprobante ─┤◄────────────────┤                                │
```

**El paso decisivo:** entre `order.paid` y la ejecución, Topups **vuelve a preguntar** por
el estado real de la orden. No confía en el evento: un evento puede llegar duplicado,
retrasado o manipulado; una consulta firmada al dueño del dato, no.

---

## 3. Comisiones

Dos conceptos **separados a propósito**:

### `CommissionRule` — cuánto se le cobra al cliente

| Tipo | Cálculo |
|---|---|
| `FIXED` | Monto fijo en centavos |
| `PERCENTAGE` | Porcentaje del monto base |
| `MIXED` | Fijo + porcentaje |

Con `min_cents` y `max_cents` opcionales, para que un porcentaje no produzca una comisión
absurda en montos grandes ni irrisoria en pequeños.

### `CommissionSplit` — cómo se reparte

Pesos **enteros** entre tienda, plataforma y reserva del proveedor.

**Por qué enteros y no porcentajes:** el reparto usa `Money.allocate()`, que reparte los
centavos sobrantes por mayor residuo y garantiza que la suma sea **exactamente** la
comisión. Con porcentajes decimales aparecen diferencias de un centavo que en conciliación
contable son un problema real.

```python
Money(1000).allocate([60, 30, 10])   # $6.00 / $3.00 / $1.00, suma exacta $10.00
Money(1000).allocate([1, 1, 1])      # $3.34 / $3.33 / $3.33, suma exacta $10.00
```

Hay además un `CheckConstraint` en la base de datos que verifica la suma en cada fila.

### Resolución de reglas

De más específica a más general; la primera que exista gana:

```
tienda + producto  →  tienda + servicio  →  organización + servicio  →  plataforma
```

Siempre hay una regla por defecto: **una venta nunca se cae por falta de configuración**.

### El cálculo se congela

El resultado se copia a `CommissionEntry` al crear la orden, junto con el id y la
descripción de la regla. Si mañana el dueño cambia el porcentaje, las órdenes de ayer
siguen mostrando lo que **efectivamente se cobró**. Recalcular una comisión histórica es
falsear la contabilidad.

### Ejemplo

```
Servicio:            $300.00     base_cents        = 30000
Comisión (mixta):     $10.00     commission_cents  =  1000
                     ────────
Total al cliente:    $310.00     total_cents       = 31000

Reparto (60/30/10):
  Tienda             $6.00       store_share_cents    = 600
  SAMY Cloud         $3.00       platform_share_cents = 300
  Reserva proveedor  $1.00       provider_share_cents = 100
                     ─────
                     $10.00      ← suma exacta, verificada por constraint
```

---

## 4. Idempotencia

**Garantía por `UNIQUE` en base de datos**, no por comprobación en Python.

```
1. INSERT del registro (scope, key) con estado IN_PROGRESS
2. ¿Choca con el UNIQUE?
   ├─ Sí, y la primera terminó   → devuelve SU respuesta guardada
   ├─ Sí, y sigue en curso       → 409, reintenta en unos segundos
   └─ Sí, con cuerpo distinto    → 422, clave reusada incorrectamente
3. Ejecuta y guarda la respuesta
```

El patrón "consulta si existe, y si no, crea" tiene una ventana entre ambas operaciones.
Con dos workers y un webhook reenviado, eso son dos recargas.

Los **fallos también se registran**: reintentar con la misma clave tras un error de
negocio devuelve el mismo error, no ejecuta de nuevo.

---

## 5. QR / token temporal de cobro

Sustituye al papelito con el número de cuenta pegado en la pared: cada venta genera su
propio código, atado a esa orden y a ese monto.

| Propiedad | Cómo se garantiza |
|---|---|
| Un solo uso | `UPDATE ... WHERE consumed_at IS NULL` — atómico en la base, no un `if` |
| Expira (3–5 min) | `expires_at`, con el rango forzado en código |
| No adivinable | 32 bytes de `secrets.token_urlsafe` |
| No reconstruible | Se guarda **solo el hash SHA-256**, igual que una contraseña |
| Único por orden | `nonce` propio de 16 bytes |

### Advertencia importante

**Este token no es un QR interbancario.** Un QR real de SPEI o CoDi solo puede emitirlo
una institución financiera regulada ante Banxico. Este token identifica la orden y abre
el checkout del proveedor en el teléfono del cliente; **el cobro lo confirma el
proveedor, no el QR**.

Está documentado así en el propio modelo (`PaymentQrToken`) para que nadie lo confunda
al leer el código.

---

## 6. Webhooks

```
1. Llega POST /api/v1/webhooks/conekta/
2. Se verifica la firma RSA-SHA256 (header `digest`) ANTES de leer el cuerpo
   └─ ¿Inválida? → 400, se registra como error, no se procesa
3. Deduplicación por event_id (ventana 24 h)
4. Se localiza la orden por provider_reference
5. confirm_payment() → PAID
   └─ Idempotente: si ya está PAID, no emite el evento otra vez
6. 200 OK
```

**Nunca se procesa un webhook sin verificar.** Sin llave pública configurada, el evento
se **rechaza**: aceptar sin verificar equivale a dejar que cualquiera marque órdenes
como pagadas.

---

## 7. Proveedores

| Proveedor | Métodos | Estado |
|---|---|---|
| `cash` | Efectivo | ✅ **Operativo** |
| `conekta` | Tarjeta, transferencia | 🔧 Adaptador listo, faltan llaves |

### Por qué el efectivo sí opera

En un cobro con tarjeta la verdad la tiene el banco; afirmar "pagado" sin su confirmación
sería inventarla. En efectivo **la verdad la tiene el cajero**, usuario autenticado que
está frente al cliente. Su confirmación registra un hecho real.

Salvaguardas: exige capturar cuánto entregó el cliente, calcula el cambio, **rechaza
montos insuficientes**, es idempotente y alimenta el arqueo de caja del turno.

---

## 8. Tareas periódicas

| Tarea | Frecuencia | Qué evita |
|---|---|---|
| `drain_outbox` | 5 s | Que un pago confirmado no llegue al servicio que ejecuta |
| `expire_stale_orders` | 60 s | Que una orden quede pendiente para siempre |
| `reconcile_indeterminate` | 2 min | Que un timeout deje dinero en un limbo |
| `purge_idempotency` | Diaria | Que el índice UNIQUE se degrade |

**`expire_stale_orders` consulta al proveedor antes de expirar.** Si el cliente pagó en
el último segundo y el webhook se retrasó, expirar sin preguntar le cobraría sin
entregarle nada. Preguntar cuesta una llamada; equivocarse cuesta el dinero de un cliente.
