# Base de datos

## Una base por microservicio

```
postgres:17
├── samy_core       (usuario: samy_core)      auth, tenants, auditoría
├── samy_payments   (usuario: samy_payments)  órdenes, cobros, comisiones
├── samy_topups     (usuario: samy_topups)    catálogo, recargas
└── samy_billpay    (usuario: samy_billpay)   billers, pagos de servicios
```

**Ningún usuario tiene privilegios sobre las bases de los demás.** Un `JOIN` entre
servicios no falla por disciplina del equipo: falla porque PostgreSQL lo impide. Ver
`infra/docker/postgres/init/01-create-databases.sh`.

En desarrollo es un contenedor con cuatro bases; en producción serán instancias
separadas. Lo que importa arquitectónicamente —que nadie pueda leer las tablas de otro—
se cumple igual en ambos casos.

**PostgreSQL también en desarrollo, no SQLite.** SQLite no tiene los mismos tipos, ni los
mismos constraints, ni el mismo comportamiento transaccional. Las diferencias aparecen
justo en producción.

---

## Invariantes impuestas por la base de datos

Estas no son validaciones de la aplicación: son `CheckConstraint`. Ni un bug ni un
`UPDATE` manual pueden violarlas.

| Constraint | Qué garantiza |
|---|---|
| `order_total_equals_base_plus_commission` | `total = base + comisión`, siempre |
| `commission_shares_sum_to_total` | El reparto suma **exactamente** la comisión |
| `order_amounts_non_negative` | No hay montos negativos donde no tienen sentido |
| `order_state_is_valid` | El estado pertenece al conjunto válido |
| `order_idempotency_unique_per_store` | Una clave no produce dos órdenes |
| `qr_expires_after_creation` | Un token no puede nacer expirado |
| `product_has_amount_or_range` | Un producto sin monto no se puede vender |
| `membership_single_default_per_user` | Máximo una tienda predeterminada por usuario |

---

## Decisiones de esquema

**UUID como clave primaria** en todo lo que se expone. Un id autoincremental revela el
volumen de operaciones y facilita enumeración.

**Dinero en `BigIntegerField`, en centavos.** Nunca `float`, nunca `DecimalField` para
almacenar. `Decimal` solo en las fronteras.

**Sin claves foráneas entre servicios.** `store_id` es un `UUIDField` suelto, no un `FK`:
la tabla de tiendas vive en otra base. Una FK entre bases de microservicios distintos es
acoplamiento de datos.

**Tablas append-only** (`AuditEvent`, `OrderEvent`): `save()` bloquea la modificación de
un registro existente y `delete()` levanta. Un registro editable no sirve como evidencia.

---

## Índices y por qué

| Índice | Consulta que sirve |
|---|---|
| `(store_id, -created_at)` | Historial de la tienda — la consulta más frecuente |
| `(store_id, state, -created_at)` | Historial filtrado por estado |
| `(created_by_id, -created_at)` | "Mis operaciones" del cajero |
| `expires_at` **parcial** (`state='PAYMENT_PENDING'`) | Barrido de expiración: solo las que pueden expirar, no todo el histórico |
| `next_attempt_at` **parcial** (`status='PENDING'`) | Drenado del outbox |
| `created_at` **parcial** (`state='UNDER_REVIEW'`) | Conciliación |
| `(provider_slug, provider_reference)` | Localizar la orden desde un webhook |
| `correlation_id` | Reconstruir una operación completa entre servicios |

Los índices parciales importan: la tabla de órdenes crece indefinidamente, pero las
pendientes de expirar son siempre pocas. Un índice completo sobre `expires_at` crecería
sin límite para responder una consulta que solo mira decenas de filas.

---

## Migraciones

Django migrations, versionadas en git. Se aplican por servicio:

```bash
docker compose exec core     python manage.py migrate
docker compose exec payments python manage.py migrate
docker compose exec topups   python manage.py migrate
docker compose exec billpay  python manage.py migrate
```

**Las migraciones sí se versionan** — son parte del esquema y deben aplicarse en el mismo
orden en todos los entornos.

---

## Respaldos

En producción, respaldos gestionados por el proveedor con point-in-time recovery.

> Un respaldo que no se ha restaurado nunca **no es un respaldo**. La restauración debe
> probarse periódicamente, no asumirse.
