# Modelos de servicio en la nube aplicados a SAMY Cloud

Este documento explica **IaaS, PaaS y SaaS** usando SAMY Cloud como caso concreto,
no como definiciones de libro.

La idea central es la **responsabilidad compartida**: en cada modelo, alguien más
administra una parte de la pila y tú administras el resto. Cuanto más subes en la
pila, menos infraestructura operas y menos control tienes sobre ella.

---

## La pila, y quién administra qué

```
                        On-premise      IaaS          PaaS          SaaS
                        (servidor       (máquina      (servicio     (SAMY Cloud
                         propio)         virtual)      gestionado)   para la tienda)

  Datos del negocio        TÚ             TÚ            TÚ            TÚ
  Aplicación               TÚ             TÚ            TÚ         PROVEEDOR
  Runtime (Python)         TÚ             TÚ         PROVEEDOR     PROVEEDOR
  Middleware               TÚ             TÚ         PROVEEDOR     PROVEEDOR
  Sistema operativo        TÚ             TÚ         PROVEEDOR     PROVEEDOR
  Virtualización           TÚ          PROVEEDOR     PROVEEDOR     PROVEEDOR
  Servidores físicos       TÚ          PROVEEDOR     PROVEEDOR     PROVEEDOR
  Almacenamiento           TÚ          PROVEEDOR     PROVEEDOR     PROVEEDOR
  Red                      TÚ          PROVEEDOR     PROVEEDOR     PROVEEDOR
```

**SAMY Cloud ocupa dos posiciones a la vez en este cuadro**, y esa es la parte
interesante:

- **Es SaaS** para la tienda que lo contrata.
- **Consume PaaS e IaaS** para poder existir.

---

## 1. SaaS — lo que SAMY Cloud *es*

> **Software as a Service:** el cliente usa la aplicación por internet. No instala,
> no actualiza, no administra servidores. Paga por usarla.

### Qué recibe la tienda

Doña Carmen tiene una papelería. Contrata SAMY Cloud y:

- Entra desde `https://samycloud.mx` con su correo y contraseña.
- Opera desde **el celular del mostrador, la tablet o la computadora**, indistintamente.
- Sus datos viven en la nube: si el celular se cae al piso, no perdió nada.
- Cuando publicamos una mejora, ya la tiene. No instala nada.
- Da de alta a su empleado como cajero, con permisos limitados, sin llamar a soporte.

### Qué NO administra

Ni servidores, ni base de datos, ni respaldos, ni certificados TLS, ni parches de
seguridad, ni la integración con los proveedores de pago. **Eso es nuestro trabajo, y
es exactamente lo que está pagando.**

### Cómo se materializa en el código

| Característica SaaS | Dónde vive |
|---|---|
| **Multi-tenancy** — muchas tiendas en una instancia | `core/apps/tenancy/models.py` |
| **Aislamiento de datos** — la tienda A no ve a la B | `StoreScopedQuerySet`, `CurrentStoreMiddleware` |
| **Autoservicio** — el dueño administra empleados | `core/apps/tenancy/` + RBAC |
| **Acceso multi-dispositivo** — mobile-first + PWA | `core/static/src/app.css`, manifiesto PWA |
| **Configurabilidad por cliente** — comisiones por tienda | `services/payments/apps/commissions/` |
| **Actualización centralizada** | Un despliegue actualiza a todos |

### El modelo de negocio que habilita

SaaS permite cobrar por suscripción y por transacción en vez de vender licencias.
Por eso las comisiones son configurables por tienda (`CommissionRule` +
`CommissionSplit`): se puede tener un plan básico y uno premium con distinto reparto,
sin tocar una línea de código.

---

## 2. PaaS — lo que SAMY Cloud *consume* para ejecutarse

> **Platform as a Service:** el proveedor administra el sistema operativo, el runtime
> y el escalado. Tú entregas el código y él lo ejecuta.

### Qué usaríamos como PaaS

| Componente | Servicio PaaS | Por qué PaaS y no administrarlo nosotros |
|---|---|---|
| **PostgreSQL** | Cloud SQL / RDS / Neon | Respaldos automáticos, réplicas, parches y point-in-time recovery. Operar una base de datos que guarda transacciones de dinero **no es donde queremos gastar nuestro tiempo**. Un error de respaldo aquí es catastrófico. |
| **Contenedores** | Cloud Run / App Runner | Escala a cero cuando no hay tráfico y escala solo en hora pico. Una papelería opera de 8 a 20 h; pagar servidores encendidos de madrugada es tirar dinero. |
| **Redis** | Memorystore / Upstash | Cache, locks de idempotencia y streams de eventos, sin administrar failover. |
| **Colas / Celery** | El mismo runtime de contenedores | Los workers son contenedores más. |
| **Correo** | SendGrid / SES | La entregabilidad de correo es un problema en sí mismo. |
| **Secretos** | Secret Manager | `DJANGO_SECRET_KEY`, llaves de Conekta. **Nunca en el repositorio.** |
| **Logs y métricas** | Cloud Logging / CloudWatch | Los logs JSON estructurados de `samy_common.observability` se indexan solos. |

### El compromiso

**Ganas:** no administras sistemas operativos, el escalado es automático, los respaldos
existen desde el día uno.

**Pierdes:** control fino y portabilidad. Un servicio PaaS propietario ata a ese proveedor.

**Cómo lo mitigamos:** todo corre en **contenedores Docker estándar**. Cloud Run, App
Runner, Azure Container Apps y un Kubernetes propio ejecutan la misma imagen. La
configuración entra por variables de entorno, no por SDK del proveedor. Cambiar de nube
es cambiar dónde se despliega, no reescribir la aplicación.

---

## 3. IaaS — la capa de la que dependemos indirectamente

> **Infrastructure as a Service:** el proveedor renta cómputo, red y almacenamiento
> virtualizados. Tú administras desde el sistema operativo hacia arriba.

### Dónde aparece IaaS en SAMY Cloud

En condiciones normales **no gestionamos IaaS directamente** — precisamente porque
elegimos PaaS. Aparecería si:

| Situación | Componente IaaS |
|---|---|
| Un requisito regulatorio exige control total del entorno | Máquinas virtuales (Compute Engine, EC2) |
| Necesitamos aislamiento de red estricto entre servicios | VPC, subredes, reglas de firewall |
| Almacenamiento de comprobantes y respaldos | Object storage (S3, GCS, R2) |
| Un balanceador con reglas propias | Load balancer administrado |
| Volumen suficiente para que PaaS salga más caro | VMs con Kubernetes propio |

El almacenamiento de objetos **sí lo usamos ya**: `django-storages` está configurado y
se activa con `USE_OBJECT_STORAGE=True`. Los comprobantes y adjuntos no pueden vivir en
el disco del contenedor, porque un contenedor es efímero: al reiniciarse, se pierde.

### Cuándo tendría sentido bajar a IaaS

Cuando el costo de PaaS supere el costo de operar VMs *más el sueldo de quien las
administra*. Con una decena de tiendas eso no ocurre ni de lejos. Con cientos, se
recalcula.

---

## 4. El cuadro completo de SAMY Cloud

```
   ┌─────────────────────────────────────────────────────────┐
   │  LA TIENDA (Doña Carmen)                                │
   │  Solo abre el navegador. No administra NADA.            │
   └──────────────────────────┬──────────────────────────────┘
                              │ HTTPS
   ┌──────────────────────────▼──────────────────────────────┐
   │  SAMY CLOUD  ················· es SaaS ·················│
   │                                                          │
   │   Core Platform · Payments · Topups · Billpay            │
   │   (nuestro código, en contenedores Docker)               │
   └──────────────────────────┬──────────────────────────────┘
                              │ se ejecuta sobre
   ┌──────────────────────────▼──────────────────────────────┐
   │  PaaS  ······· lo que consumimos, no administramos ·····│
   │                                                          │
   │   Contenedores gestionados · PostgreSQL gestionado ·     │
   │   Redis · Secret Manager · Logs · Correo                 │
   └──────────────────────────┬──────────────────────────────┘
                              │ que a su vez corre sobre
   ┌──────────────────────────▼──────────────────────────────┐
   │  IaaS  ········ invisible para nosotros ················│
   │                                                          │
   │   Cómputo virtualizado · Red · Almacenamiento · CDN      │
   └──────────────────────────────────────────────────────────┘
```

---

## 5. Cómo esto cambió decisiones reales del proyecto

Esto no es teoría de la materia: las tres capas explican decisiones concretas del código.

**Porque somos SaaS multi-tenant:**
`store_id` está en cada tabla de negocio, `StoreScopedQuerySet` obliga a declarar el
ámbito, y `CurrentStoreMiddleware` **revalida la membresía en cada petición**. Si a un
cajero se le retira el acceso, deja de operar en la siguiente petición, sin esperar a que
caduque su sesión. En software instalado esto no haría falta; en SaaS es la diferencia
entre un producto y una fuga de datos.

**Porque consumimos PaaS con contenedores efímeros:**
Los logs van a `stdout` en JSON, no a archivos (`samy_common/observability/logging.py`).
Las sesiones viven en Redis, no en memoria del proceso. Los archivos van a object storage.
Nada se guarda en el disco del contenedor, porque ese disco desaparece en el siguiente
despliegue.

**Porque queremos poder cambiar de nube:**
Un `Dockerfile` común y parametrizado (`infra/docker/django.Dockerfile`), configuración
por variables de entorno, y cero SDK propietario en la lógica de negocio. La misma imagen
corre en Cloud Run, en App Runner y en un Kubernetes propio.

**Porque el escalado es del proveedor:**
Los servicios son *stateless*. Todo el estado está en PostgreSQL o Redis. Levantar tres
instancias de `payments` no rompe nada: por eso la idempotencia se garantiza con
`UNIQUE` en la base de datos y no con una variable en memoria, que sería distinta en cada
instancia.

---

## 6. Comparación honesta de opciones

| Modelo | Control | Costo inicial | Esfuerzo operativo | ¿Para SAMY Cloud? |
|---|---|---|---|---|
| On-premise | Total | Alto (hardware) | Muy alto | ❌ Sin sentido para un SaaS |
| IaaS | Alto | Medio | Alto | ⚠️ Solo si lo exige un regulador |
| **PaaS** | Medio | **Bajo** | **Bajo** | ✅ **Elegido para el MVP** |
| Serverless puro | Bajo | Muy bajo | Muy bajo | ⚠️ Arranques en frío perjudican un punto de venta |

**Decisión: PaaS con contenedores.** Da el mejor equilibrio entre costo, esfuerzo y
portabilidad para este producto en esta etapa. La comparativa de proveedores concretos y
sus precios actuales va en [`deployment.md`](deployment.md).

---

## Glosario rápido

| Término | Qué significa aquí |
|---|---|
| **Multi-tenancy** | Muchas tiendas comparten la misma instancia, con datos aislados |
| **Tenant** | Cada tienda (`Store`) es un inquilino |
| **Stateless** | El servicio no guarda estado en memoria; puede reiniciarse sin perder nada |
| **Efímero** | El disco del contenedor desaparece al reiniciarse |
| **Responsabilidad compartida** | Qué asegura el proveedor y qué aseguras tú |
| **Vendor lock-in** | Quedar atado a un proveedor por usar sus APIs propietarias |
| **Escalado horizontal** | Añadir más instancias en vez de una más grande |
