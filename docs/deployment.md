# Despliegue

## Principio: portabilidad antes que optimización

Todo corre en **contenedores Docker estándar**. La configuración entra por variables de
entorno, no por SDK del proveedor. Cambiar de nube es cambiar dónde se despliega, no
reescribir la aplicación.

---

## Qué hace falta en producción

| Componente | Qué resuelve |
|---|---|
| Runtime de contenedores | Ejecutar los 4 servicios + workers |
| PostgreSQL gestionado | 4 bases, respaldos automáticos, point-in-time recovery |
| Redis gestionado | Cache, locks de idempotencia, streams de eventos |
| Object storage | Comprobantes y adjuntos (el disco del contenedor es efímero) |
| Secret Manager | `DJANGO_SECRET_KEY`, `SERVICE_S2S_SECRET`, llaves de proveedores |
| Certificado TLS | HTTPS obligatorio |
| Logs y métricas | Los logs JSON estructurados se indexan solos |

---

## Opciones a evaluar

**No se recomienda una todavía**: los precios y los free tier cambian y deben verificarse
en el momento de contratar, no citarse de memoria.

| Proveedor | Runtime | Base de datos | A favor | En contra |
|---|---|---|---|---|
| **Google Cloud** | Cloud Run | Cloud SQL | Escala a cero; despliegue simple desde contenedor | Cloud SQL tiene costo mínimo mensual |
| **AWS** | App Runner / ECS Fargate | RDS | Ecosistema completo | Más piezas que configurar |
| **Oracle Cloud** | Container Instances | Autonomous DB | Free tier históricamente generoso | Menos documentación y comunidad |
| **Azure** | Container Apps | Azure DB for PostgreSQL | Buena integración con identidad | — |
| **Fly.io / Railway / Render** | Nativo de contenedores | Postgres gestionado | Muy simple para un MVP | Menos control fino |

### Cómo decidir (no por fama)

1. **Costo real del primer año** con el tráfico esperado, no el precio de lista.
2. **¿Escala a cero?** Una papelería opera de 8 a 20 h. Pagar servidores encendidos de
   madrugada es tirar dinero.
3. **Costo mínimo de la base de datos** — suele ser la partida dominante.
4. **Región en México o cercana** — la latencia se nota en un mostrador.
5. **Facilidad de salida** — cuánto cuesta migrar si sube el precio.

> Antes de contratar cualquier cosa: verificar precios actuales, qué es free tier
> permanente y qué es prueba temporal, y qué pasa al agotarse la prueba.

---

## Checklist antes de producción

### Configuración
- [ ] `DEBUG=False`
- [ ] `DJANGO_ALLOWED_HOSTS` con dominios concretos, sin `*`
- [ ] `DJANGO_SECRET_KEY` y `SERVICE_S2S_SECRET` generados y en Secret Manager
- [ ] `DJANGO_ADMIN_PATH` cambiado
- [ ] `CSRF_TRUSTED_ORIGINS` con el dominio real

### Seguridad
- [ ] HTTPS con certificado válido
- [ ] `SECURE_HSTS_SECONDS` bajo al principio; subir tras verificar
- [ ] Cookies `Secure` y `HttpOnly`
- [ ] CSP activa y probada
- [ ] Base de datos con `sslmode=require` y **sin IP pública**
- [ ] Los microservicios **no** accesibles desde internet

### Operación
- [ ] Migraciones aplicadas en los 4 servicios
- [ ] Health checks configurados en el balanceador
- [ ] Respaldos automáticos **con restauración probada**
- [ ] Logs centralizados
- [ ] Alertas: backlog del outbox, órdenes en `UNDER_REVIEW`, 5xx

### Proveedores
- [ ] Credenciales de **producción** (no sandbox) en Secret Manager
- [ ] `CONEKTA_MODE=PRODUCTION`, `RELOADLY_MODE=PRODUCTION`
- [ ] Webhooks apuntando al dominio de producción
- [ ] Cuentas fondeadas
- [ ] **Una transacción real de prueba, de punta a punta**

---

## Estrategia de despliegue

Los servicios son *stateless*: todo el estado está en PostgreSQL o Redis. Eso permite
despliegue sin interrupción (rolling).

**Orden importante:** migraciones primero, aplicación después. Y las migraciones deben ser
**compatibles hacia atrás** durante el despliegue, porque convivirán la versión vieja y la
nueva. Añadir una columna `NOT NULL` sin valor por defecto rompe la versión anterior
mientras termina el despliegue.

---

## Costo aproximado del MVP

Rangos orientativos, **a verificar al contratar**:

| Partida | Peso relativo |
|---|---|
| PostgreSQL gestionado | Suele ser la partida dominante |
| Runtime de contenedores | Bajo si escala a cero |
| Redis | Bajo |
| Object storage | Muy bajo al principio |
| Dominio y TLS | TLS gratis con Let's Encrypt vía Caddy |

**La comisión del proveedor de pagos (≈3.4% + $3 por transacción) será mucho mayor que
toda la infraestructura.** Ahí es donde conviene negociar, no en el servidor.
