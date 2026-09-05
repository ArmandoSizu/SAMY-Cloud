# Arquitectura de SAMY Cloud

Este documento explica **qué se decidió, qué se descartó y por qué**. Una decisión sin
su alternativa descartada no es una decisión, es una suposición.

---

## 1. La forma del sistema

```
                         ┌──────────────────────────┐
   Navegador ──────────► │  Caddy (proxy inverso)   │
   celular / tablet / PC └────────────┬─────────────┘
                                      │  único puerto expuesto
                         ┌────────────▼─────────────┐
                         │      CORE PLATFORM       │
                         │  Django + HTMX + DRF     │
                         │                          │
                         │  · autenticación         │
                         │  · organizaciones/tiendas│
                         │  · RBAC                  │
                         │  · interfaz de usuario   │
                         │  · auditoría             │
                         │  · Backend For Frontend  │
                         │                          │
                         │  BD: samy_core           │
                         └────────────┬─────────────┘
                                      │ HTTP + HMAC-SHA256
                                      │ (red interna, nunca expuesta)
            ┌─────────────────────────┼─────────────────────────┐
            ▼                         ▼                         ▼
  ┌──────────────────┐     ┌──────────────────┐     ┌──────────────────┐
  │    PAYMENTS      │     │     TOPUPS       │     │     BILLPAY      │
  │                  │     │                  │     │                  │
  │ · órdenes        │     │ · catálogo       │     │ · billers        │
  │ · máquina estados│     │ · operadores     │     │ · referencias    │
  │ · comisiones     │     │ · recargas       │     │ · consulta       │
  │ · webhooks       │     │                  │     │ · pago           │
  │ · QR temporal    │     │                  │     │                  │
  │                  │     │                  │     │                  │
  │ BD: samy_payments│     │ BD: samy_topups  │     │ BD: samy_billpay │
  └────────┬─────────┘     └────────┬─────────┘     └────────┬─────────┘
           │                        │                        │
           └────────────────────────┼────────────────────────┘
                                    ▼
                     ┌──────────────────────────────┐
                     │  Redis Streams (eventos)     │
                     │  order.paid                  │
                     │  fulfillment.result          │
                     │  order.refund_required       │
                     └──────────────────────────────┘
```

---

## 2. Decisiones y sus alternativas descartadas

### 2.1 Microservicios reales, no tres carpetas

**Decisión:** cuatro servicios Django desplegables por separado, **cada uno con su propia
base de datos PostgreSQL y su propio usuario**.

**Alternativa descartada:** un monolito modular con apps separadas y una sola base.

**Por qué:** con una base compartida, nada impide que alguien escriba un `JOIN` entre las
tablas de recargas y las de pagos. Ese `JOIN` funciona, pasa las pruebas, y el día que se
quiere desplegar recargas por separado resulta imposible. **El acoplamiento de datos es
irreversible en la práctica.**

Con bases y usuarios separados, el `JOIN` sencillamente falla: PostgreSQL lo impide. La
separación deja de depender de la disciplina del equipo.

**Lo que cuesta:** no hay transacciones distribuidas. Una operación que toca dos servicios
usa el patrón saga con outbox transaccional. Es más código y hay que pensar en la
consistencia eventual. Se asume conscientemente.

**Cómo se materializa:** `infra/docker/postgres/init/01-create-databases.sh` crea cuatro
bases con cuatro usuarios. Ninguno tiene privilegios sobre las bases de los demás.

---

### 2.2 El Core como Backend For Frontend

**Decisión:** el navegador solo habla con el Core. Los microservicios viven en la red
interna y solo aceptan peticiones firmadas.

**Alternativa descartada:** exponer cada microservicio y que el frontend los llame
directamente.

**Por qué:**

| | BFF (elegido) | Servicios expuestos |
|---|---|---|
| Credencial en el navegador | Cookie de sesión `HttpOnly` | Token que JavaScript puede leer |
| CORS | No existe: un solo origen | Configurar CORS en 3 servicios |
| Autorización multi-tenant | Se resuelve una vez, en el Core | Se replica en 3 servicios |
| Superficie expuesta a internet | 1 servicio | 4 servicios |

El tercer punto es el decisivo. Replicar la lógica de "este usuario pertenece a esta
tienda" en tres servicios garantiza que con el tiempo diverjan, y una divergencia ahí es
una fuga de datos entre tiendas.

**Cómo se materializa:** `core/apps/gateway/clients.py` + `samy_common/http/client.py`.

---

### 2.3 Django 5.2 LTS, no Django 6.1

**Decisión:** Django **5.2 LTS**, aunque 6.1.1 sea la última versión estable.

**Por qué:** 5.2 tiene soporte de seguridad hasta **abril de 2028**. Las versiones 6.x no
LTS reciben parches durante unos 16 meses. Para un producto que se pretende comercializar
y que mueve dinero, **la ventana de soporte pesa más que tener lo más nuevo**. Actualizar
un sistema de pagos en producción no es gratis.

**Cuándo se revisa:** cuando salga Django 6.2 LTS (previsiblemente abril de 2027).

---

### 2.4 HTMX en vez de React

**Decisión:** Django Templates + HTMX + Alpine.js + Tailwind v4.

**Por qué no React:**

1. **La interfaz es un punto de venta.** Formularios, listas, confirmaciones. HTMX
   resuelve eso con una fracción del código, y sin un estado de cliente que sincronizar
   con el servidor.
2. **Un SPA implica un segundo despliegue** y una segunda cadena de build.
3. **Tokens de API en el navegador.** Con HTMX la sesión es una cookie `HttpOnly` que
   JavaScript no puede leer. Con un SPA hay que decidir dónde guardar un token, y todas
   las respuestas son peores.
4. **La parte genuinamente interactiva —el lector de códigos— son ~150 líneas de
   JavaScript** con la API `BarcodeDetector` del navegador. No justifica un framework.

**Cuándo se revisaría:** si hiciera falta una app móvil nativa o una interfaz con estado
cliente complejo (edición colaborativa, tiempo real intensivo). La API REST versionada ya
está ahí para ese día.

---

### 2.5 Un paquete compartido, no código copiado

**Decisión:** `libs/samy_common/` como paquete Python instalable en los cuatro servicios.

**Alternativa descartada:** copiar el código común en cada servicio ("los microservicios
no deben compartir código").

**Por qué:** ese principio aplica al **modelo de dominio**, no a las utilidades técnicas.
Si el redondeo de una comisión diverge entre dos servicios, aparecen descuadres contables
de centavos que son imposibles de rastrear y que un auditor detecta.

Lo que se comparte es deliberadamente **genérico y estable**: manejo de dinero, máquinas
de estado, firma HMAC, idempotencia, outbox, logging. **No se comparten modelos de
dominio**: cada servicio define los suyos.

---

### 2.6 Redis Streams, no Kafka

**Decisión:** Redis Streams con grupos de consumidores.

**Por qué:** Redis ya está en el stack (cache, sesiones, Celery). Streams da grupos de
consumidores, acuse explícito y reentrega de mensajes no confirmados — que es exactamente
lo necesario para entrega "al menos una vez".

Kafka sería correcto a partir de decenas de miles de eventos por segundo. Aquí hablamos
de miles de operaciones **diarias**. Introducirlo sería complejidad operativa y costo sin
beneficio.

**Cómo se materializa:** `samy_common/events/bus.py`. La interfaz `EventBus` deja la
puerta abierta a cambiar de implementación sin tocar la lógica de negocio.

---

### 2.7 Multi-tenancy por discriminador de fila

**Decisión:** `store_id` en cada tabla de negocio.

**Alternativas descartadas:**

| Estrategia | Por qué no |
|---|---|
| Un esquema por tienda | Las migraciones se multiplican por el número de clientes. Con cientos de tiendas, cada despliegue es una operación de horas. |
| Una base por tienda | Imposible hacer consultas agregadas de plataforma sin un ETL. |

**El riesgo que asume:** olvidar el filtro en una consulta y filtrar datos de otra tienda.

**Cómo se mitiga, en tres capas:**

1. `StoreScopedQuerySet` obliga a declarar el ámbito (`for_store`, `for_user`).
2. `CurrentStoreMiddleware` **revalida la membresía en cada petición**, no confía en la
   sesión.
3. Las consultas por identificador filtran también por tienda y devuelven **404, no 403**:
   no confirmamos ni siquiera que el identificador exista.

---

### 2.8 Dinero en centavos enteros

**Decisión:** `BigIntegerField` en centavos. `Decimal` solo en las fronteras. **`float`
está prohibido** y el propio código lo rechaza con `TypeError`.

**Por qué:** `0.1 + 0.2 != 0.3` en punto flotante binario. En un sistema de dinero eso
produce descuadres de centavos imposibles de conciliar.

**El detalle que la mayoría pasa por alto:** repartir una comisión de $10.00 entre tres
partes iguales da $3.333... El reparto ingenuo pierde o inventa un centavo.
`Money.allocate()` implementa el algoritmo de **mayor residuo**, que garantiza que la
suma de las partes sea **exactamente** el total. Además hay un `CheckConstraint` en la
base de datos que lo verifica en cada fila.

---

### 2.9 La regla del dinero, impuesta por la máquina de estados

**Decisión:** los estados `CREATED` y `PAYMENT_PENDING` **no tienen ninguna transición
hacia `PROCESSING`**.

**Por qué así y no con un `if`:** un `if` se puede olvidar en un camino nuevo. La ausencia
de una arista en el grafo de transiciones no se puede olvidar: `assert_transition()`
levanta `IllegalTransition` antes de tocar al proveedor, venga la llamada de donde venga.

Además, el servicio de recargas **no confía en el evento** `order.paid`: vuelve a
preguntarle al servicio de Pagos por el estado real de la orden antes de gastar saldo
(`_order_is_paid()` en `topups/apps/fulfillment/services.py`). Es una llamada HTTP más por
recarga; el costo de equivocarse es el monto de la recarga, cada vez.

---

### 2.10 Idempotencia con `UNIQUE`, no con `if existe`

**Decisión:** restricción `UNIQUE(scope, key)` en base de datos + `INSERT` que puede fallar.

**Por qué:** el patrón "consulta si existe, y si no, crea" tiene una ventana entre ambas
operaciones por la que caben dos peticiones concurrentes. Con dos workers y un webhook
reenviado, eso son dos recargas. La única garantía real es la restricción de la base.

**Cómo se materializa:** `samy_common/idempotency/` y el decorador
`services/payments/apps/api/idempotency.py`.

---

### 2.11 Outbox transaccional

**Problema:** al confirmar un pago hay que (a) guardar la orden como `PAID` y (b) avisar
al servicio de recargas. Si se guarda primero y el proceso muere antes de publicar, el
cliente pagó y nunca recibe su recarga.

**Decisión:** el evento se escribe en una tabla de la **misma base** y dentro de la
**misma transacción** que el cambio de estado. Un proceso aparte lo publica al bus.

**Consecuencia:** entrega "al menos una vez". Por eso todo consumidor es idempotente.

---

### 2.12 Proveedores sin credenciales que rechazan operar

**Decisión:** `BaseProvider.ensure_ready()` levanta `ProviderNotConfigured` si el
adaptador no tiene credenciales verificadas.

**Por qué:** es el mecanismo técnico que hace imposible una transacción ficticia. No es
una convención del equipo ni un comentario en el código: **no existe un camino que
produzca un `PaymentResult` exitoso sin confirmación real del proveedor.**

Además, `check_health()` no da por bueno un `READY` solo porque la variable de entorno no
esté vacía: hace una llamada real. Un `RELOADLY_CLIENT_ID` con una llave caducada reporta
`NOT_CONFIGURED`, no `READY`.

---

### 2.13 Efectivo como método de pago real

**Decisión:** `CashProvider` **sí opera**, sin credenciales externas.

**Por qué no es una simulación:** en un cobro con tarjeta la verdad la tiene el banco, y
afirmar "pagado" sin su confirmación sería inventarla. En un cobro en efectivo **la
verdad la tiene el cajero**, que es un usuario autenticado y está frente al cliente. Su
confirmación registra un hecho real, con responsable identificado.

**Salvaguardas:** exige capturar cuánto entregó el cliente, calcula el cambio, rechaza
montos insuficientes, es idempotente y alimenta el arqueo de caja del turno. Un cajero que
confirme cobros que no recibió produce un faltante visible.

**Consecuencia práctica:** el flujo vertical completo —login → recarga → orden → cobro →
comprobante— **funciona de extremo a extremo sin ninguna credencial externa**, y sin
mentir en ningún paso.

---

## 3. Estructura del repositorio y por qué se cambió

Frente a la estructura propuesta inicialmente, hay tres cambios:

| Cambio | Motivo |
|---|---|
| **Se agregó `libs/samy_common/`** | Evita que el manejo de dinero y de estados se copie en cuatro proyectos y diverja. |
| **Se agregó `infra/gateway/`** | Un proxy inverso da un solo origen (sin CORS), sirve estáticos sin pasar por Python, y deja listo el HTTPS automático. |
| **No se creó `frontend/` separado** | Las plantillas viven en `core/`. Separarlas implicaría un segundo despliegue sin ganar nada, ya que no hay SPA. |

---

## 4. Lo que todavía no está resuelto

Honestidad sobre el estado real:

| Pendiente | Estado |
|---|---|
| Credenciales de proveedores | Ninguna. Todos los adaptadores en `NOT_CONFIGURED` / `PENDING_CONTRACT`. |
| Especificación del código de barras de CFE | No es pública. Debe pedirse al agregador. |
| Suite de pruebas | Estructura lista; falta escribir la mayoría de los casos. |
| CI/CD | Pendiente. |
| Despliegue en nube | Documentado, no ejecutado. |
| Migraciones aplicadas | Pendiente de levantar Docker. |

---

## 5. Documentos relacionados

- [`security.md`](security.md) — modelo de amenazas y controles
- [`payments.md`](payments.md) — máquina de estados, comisiones, QR
- [`api-integrations.md`](api-integrations.md) — qué contratar y con quién
- [`cloud-models.md`](cloud-models.md) — SaaS, PaaS e IaaS aplicados
- [`database.md`](database.md) — esquema, índices y constraints
