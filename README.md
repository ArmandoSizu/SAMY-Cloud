# SAMY Cloud

Plataforma SaaS multi-tienda para **recargas telefónicas**, **pago de servicios** y **cobros con comisiones configurables**.

> **Estado del proyecto:** en construcción. Ninguna integración con proveedor externo está activa todavía.
> El sistema **no simula** operaciones: cuando faltan credenciales, el adaptador correspondiente
> reporta `NOT_CONFIGURED` y **rechaza operar**. Ver [Estado de integraciones](#estado-de-integraciones).

---

## Qué es

SAMY Cloud es el software que una tienda de barrio, papelería o miscelánea usa para vender
tiempo aire y cobrar recibos de luz y agua, desde el celular o la computadora del mostrador.

No es un prototipo: la arquitectura, el manejo del dinero y la seguridad están diseñados
para operar con dinero real desde el primer día.

---

## Arquitectura en 30 segundos

```
                    ┌──────────────────────────┐
   Navegador ─────► │  Proxy inverso (Caddy)   │  único punto de entrada
   (celular,        └────────────┬─────────────┘
    tablet, PC)                  │
                    ┌────────────▼─────────────┐
                    │     CORE PLATFORM        │  Django + HTMX
                    │  auth · tenants · RBAC   │  Backend For Frontend
                    │  UI · auditoría          │  BD: samy_core
                    └────────────┬─────────────┘
                                 │ HTTP firmado con HMAC-SHA256
              ┌──────────────────┼──────────────────┐
              ▼                  ▼                  ▼
    ┌──────────────┐   ┌──────────────┐   ┌──────────────┐
    │   PAYMENTS   │   │    TOPUPS    │   │   BILLPAY    │
    │ órdenes      │   │ recargas     │   │ CFE, agua    │
    │ comisiones   │   │ catálogo     │   │ consulta     │
    │ webhooks     │   │ operadores   │   │ referencias  │
    │ QR temporal  │   │              │   │              │
    │ BD propia    │   │ BD propia    │   │ BD propia    │
    └──────┬───────┘   └──────┬───────┘   └──────┬───────┘
           └──────────────────┼──────────────────┘
                    Redis Streams (eventos)
```

**Cuatro servicios desplegables por separado, cada uno con su propia base de datos.**
Ningún servicio puede leer las tablas de otro: son bases y usuarios distintos de PostgreSQL.

El navegador **nunca** habla con los microservicios. Solo el Core, y siempre con
peticiones firmadas. Detalle completo en [`docs/architecture.md`](docs/architecture.md).

---

## La regla del dinero

Es la regla que gobierna todo el sistema y está implementada en la máquina de estados,
no solo documentada:

```
1. Se crea la orden           CREATED
2. Se solicita el pago        PAYMENT_PENDING
3. El proveedor CONFIRMA      PAID          ◄── recién aquí se puede ejecutar
4. Se ejecuta el servicio     PROCESSING
5. El proveedor confirma      SUCCESS
```

En `libs/samy_common/samy_common/states.py`, los estados `CREATED` y `PAYMENT_PENDING`
**no tienen ninguna transición hacia `PROCESSING`**. Intentar recargar un teléfono sin
haber cobrado levanta `IllegalTransition` antes de tocar al proveedor.

Además:

- **Dinero en centavos enteros**, nunca `float`. Ver `samy_common/money.py`.
- **Idempotencia con `UNIQUE` en base de datos**, no con `if existe: ...`.
- **Transactional outbox**: el evento y el cambio de estado se guardan en la misma
  transacción, o no se guarda ninguno.

---

## Stack y por qué

| Capa | Elección | Razón |
|---|---|---|
| Backend | **Django 5.2 LTS** + DRF | Soporte de seguridad hasta abril 2028. Auth, ORM, migraciones y admin resueltos. No 6.x: la ventana de soporte pesa más que la novedad. |
| Base de datos | **PostgreSQL 17** | Constraints reales, tipos ricos, transaccionalidad seria. También en desarrollo, no SQLite. |
| Frontend | **Django Templates + HTMX + Alpine.js + Tailwind v4** | Ver nota abajo. |
| Cola / eventos | **Celery + Redis Streams** | Redis ya está en el stack. Kafka sería complejidad operativa sin beneficio a esta escala. |
| Proxy | **Caddy** | HTTPS automático en producción con una línea de configuración. |
| Contenedores | **Docker + Compose** | Portabilidad entre nubes, sin vendor lock-in. |

### Por qué no React

Se evaluó y se descartó **para este producto**, no por principio:

- La interfaz es un punto de venta: formularios, tablas, confirmaciones. HTMX resuelve
  el 95% con una fracción del código.
- Un SPA implica un segundo despliegue, un segundo build, y tokens de API en el navegador.
  Con HTMX la sesión vive en una cookie `HttpOnly` y no hay CORS.
- El lector de códigos, que es la parte genuinamente interactiva, se resuelve con
  ~150 líneas de JavaScript y la API `BarcodeDetector` del navegador.

Si en el futuro se necesita una app nativa, la API REST versionada ya está ahí.

---

## Estado de integraciones

Investigación verificada (septiembre 2026). Detalle completo en
[`docs/api-integrations.md`](docs/api-integrations.md).

| Proveedor | Para | Sandbox self-service | Estado |
|---|---|---|---|
| **Conekta** | Cobro con tarjeta | ✅ Sí, hoy mismo | 🔧 Adaptador listo · faltan llaves |
| **Efectivo en mostrador** | Cobro en efectivo | — no aplica | ✅ **Operativo** |
| **Reloadly** | Recargas (pruebas) | ✅ Sí, gratis | 🔧 Adaptador listo · faltan llaves |
| **Taecel** | Recargas (producción MX) | ❌ Requiere contrato | ⏸ Pendiente de contrato |
| **tapi / Arcus** | CFE, agua | ❌ Requiere contrato | ⏸ Pendiente de contrato |

**Hallazgos que cambian el plan:**

- **Telcel, AT&T y Movistar no ofrecen API pública.** Se opera forzosamente vía
  distribuidor autorizado.
- **CFE no tiene API pública** de consulta ni de pago. Solo vía agregador con convenio.
- **CAPDAM es de Manzanillo, Colima** (no de Durango). No expone ninguna API;
  la vía real es un agregador que ya tenga el convenio.
- **El QR de Mercado Pago está discontinuado desde julio 2023.** No es una opción.
- **CoDi no está disponible como API** para quien no sea institución financiera regulada.

---

## Cómo levantarlo

### Requisitos

- Docker Desktop (con el daemon corriendo)
- Git

Nada más. Python, PostgreSQL y Node viven dentro de los contenedores.

### Primeros pasos

```bash
# 1. Configuración
cp .env.example .env

# 2. Genera los secretos y pégalos en .env
python -c "import secrets; print('DJANGO_SECRET_KEY=' + secrets.token_urlsafe(64))"
python -c "import secrets; print('SERVICE_S2S_SECRET=' + secrets.token_urlsafe(64))"

# 3. Levanta todo
docker compose up -d --build

# 4. Migraciones (una vez por servicio: cada uno tiene su propia base)
docker compose exec core     python manage.py migrate
docker compose exec payments python manage.py migrate
docker compose exec topups   python manage.py migrate
docker compose exec billpay  python manage.py migrate

# 5. Datos iniciales: organización, tienda y usuarios de los tres roles
docker compose exec core python manage.py seed_demo

# 6. Compila el CSS
docker compose --profile frontend up tailwind
```

Abre **http://localhost:8000**

En Windows, el script `scripts/dev-setup.ps1` hace los pasos 1 a 6 de una vez.

### Verifica que está sano

```bash
curl http://localhost:8000/health/
docker compose ps
```

---

## Estructura

```
SAMY Cloud/
├── core/                    Core Platform: auth, tenants, UI, BFF
│   ├── config/              settings (base/dev/prod/test), urls, wsgi
│   ├── apps/
│   │   ├── accounts/        usuario propio (email como identificador)
│   │   ├── tenancy/         Organization · Store · Membership · RBAC
│   │   ├── audit/           bitácora inmutable
│   │   ├── dashboard/       pantallas de cajero y de dueño
│   │   ├── gateway/         clientes S2S, health, manejo de errores
│   │   └── platform_admin/  panel de SAMY Cloud
│   ├── templates/           HTML (Django Templates + HTMX)
│   └── static/              CSS fuente, logo SVG, JS del escáner
│
├── services/
│   ├── payments/            microservicio 1 · BD samy_payments
│   ├── topups/              microservicio 2 · BD samy_topups
│   └── billpay/             microservicio 3 · BD samy_billpay
│
├── libs/samy_common/        paquete compartido e instalable
│   └── samy_common/
│       ├── money.py         Money en centavos + reparto sin perder centavos
│       ├── states.py        máquinas de estado del dinero
│       ├── security/        firma HMAC S2S · enmascarado de PII
│       ├── idempotency/     registro con UNIQUE en BD
│       ├── events/          outbox transaccional + Redis Streams
│       ├── providers/       contrato base de todo adaptador externo
│       ├── http/            cliente S2S firmado con reintentos
│       └── observability/   logs JSON correlacionados
│
├── infra/
│   ├── docker/              Dockerfile común + init de PostgreSQL
│   ├── gateway/             Caddyfile
│   └── cloud/               plantillas de despliegue
│
├── docs/                    documentación técnica
├── scripts/                 utilidades de desarrollo
└── tests/                   pruebas end-to-end entre servicios
```

**Cambio respecto a la estructura propuesta inicialmente:** se agregó
`libs/samy_common/` como paquete instalable. Sin él, el manejo de dinero y de
estados tendría que copiarse en cuatro proyectos, y una divergencia en el
redondeo de una comisión produciría descuadres contables imposibles de rastrear.

---

## Documentación

| Documento | Contenido |
|---|---|
| [`architecture.md`](docs/architecture.md) | Decisiones de arquitectura y sus alternativas descartadas |
| [`security.md`](docs/security.md) | Modelo de amenazas, OWASP, PCI DSS |
| [`cloud-models.md`](docs/cloud-models.md) | SaaS, PaaS e IaaS aplicados a SAMY Cloud |
| [`payments.md`](docs/payments.md) | Máquina de estados, comisiones, webhooks, QR |
| [`topups.md`](docs/topups.md) | Catálogo, operadores, validación de números |
| [`bill-payments.md`](docs/bill-payments.md) | CFE, CAPDAM, códigos de barras |
| [`api-integrations.md`](docs/api-integrations.md) | Qué contratar, con quién y qué falta |
| [`database.md`](docs/database.md) | Esquema, índices, constraints |
| [`deployment.md`](docs/deployment.md) | Despliegue en nube y comparativa de costos |
| [`testing.md`](docs/testing.md) | Estrategia de pruebas |
| [`demo-checklist.md`](docs/demo-checklist.md) | Guion de demostración |

---

## Licencia

Propietario. Todos los derechos reservados.
