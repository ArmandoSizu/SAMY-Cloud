# Guion de demostración

Objetivo: mostrar en 10 minutos que esto **no es una pantalla bonita**, sino un sistema
que hace cumplir reglas de dinero.

---

## Antes de empezar

```bash
docker compose ps                      # todo "healthy"
curl http://localhost:8000/health/     # {"status":"ok",...}
```

Ten abierta una terminal con `docker compose logs -f payments` para mostrar los logs
correlacionados en vivo.

---

## 1. Identidad y acceso (1 min)

- Abre `http://localhost:8000` — pantalla de login **propia**, no la de Django.
- Muestra el mismo login **en el celular**: es mobile-first de verdad.
- Entra como cajero.

**Punto a destacar:** hay tres roles y el rol se resuelve **por tienda**, no
globalmente. La misma persona puede ser dueño en una tienda y cajero en otra.

---

## 2. RBAC real (1 min)

- Como cajero, intenta entrar a `/plataforma/` → **403**.
- Cierra sesión, entra como dueño → el dashboard cambia: ahora hay ingresos y comisiones.

**Punto a destacar:** no es un menú oculto. El permiso se verifica en el servidor, y las
vistas sin permiso declarado **no se sirven** (denegar por defecto).

---

## 3. El flujo vertical completo (4 min)

Dashboard → **Recargas** → selecciona operador → captura número → elige monto →
confirma → **cobra en efectivo** → comprobante.

**Puntos a destacar mientras ocurre:**

- El catálogo viene del proveedor. Si Reloadly no ofrece $75, **$75 no aparece**.
- Captura un número inválido (9 dígitos) → error claro, en español, del lado del servidor.
- En la confirmación se ve el desglose: monto, comisión, total.
- Al confirmar el efectivo, exige **cuánto entregó el cliente** y calcula el cambio.
  Intenta confirmar con menos del total → lo rechaza.
- El comprobante trae folio, tienda, fecha, referencia enmascarada y estado.

---

## 4. La regla del dinero (2 min) — **el momento clave**

Muestra el código, no las diapositivas:

```python
# libs/samy_common/samy_common/states.py
ORDER_TRANSITIONS = {
    OrderState.CREATED:         frozenset({PAYMENT_PENDING, PAID, CANCELLED}),
    OrderState.PAYMENT_PENDING: frozenset({PAID, CANCELLED, EXPIRED, UNDER_REVIEW}),
    OrderState.PAID:            frozenset({PROCESSING, REFUND_PENDING}),
    ...
}
```

> "Fíjense en lo que **no** está: desde `CREATED` no hay ninguna arista hacia
> `PROCESSING`. No es un `if` que se pueda olvidar en un camino nuevo — es la forma del
> grafo. Intentar recargar sin cobrar levanta una excepción antes de tocar al proveedor."

Y el servicio de recargas **no confía** en el evento de pago: vuelve a preguntarle al
servicio de Pagos por el estado real antes de gastar saldo.

---

## 5. Honestidad de las integraciones (1 min)

Entra como administrador de plataforma → **Proveedores**.

Muestra la tabla de estado: cada adaptador con `NOT_CONFIGURED` o `PENDING_CONTRACT`, y
**la lista exacta de lo que falta** para activarlo.

> "Ninguna integración externa está activa, y el sistema lo dice. No hay ni un solo
> camino que devuelva un pago o una recarga ficticia: `ensure_ready()` levanta antes de
> tocar la orden."

Intenta cobrar con tarjeta → 503 con mensaje claro, y **la orden no avanza de estado**.

---

## 6. Trazabilidad (1 min)

- Muestra los logs: JSON con `correlation_id` que une toda la operación.
- Entra al historial → cada operación con su estado y su comprobante.
- Muestra la auditoría: quién, cuándo, desde dónde, estado anterior y nuevo.

> "`AuditEvent.save()` bloquea la modificación y `delete()` levanta una excepción. Un
> registro que se puede editar no sirve como evidencia."

---

## Preguntas probables

| Pregunta | Respuesta corta |
|---|---|
| ¿Por qué no React? | Es un punto de venta: formularios y tablas. HTMX evita un segundo despliegue y tokens en el navegador. La API REST ya está para el día que haga falta. |
| ¿Son microservicios de verdad? | Cuatro bases de datos con cuatro usuarios distintos. Un `JOIN` entre servicios **falla**: lo impide PostgreSQL, no la disciplina del equipo. |
| ¿Y si el proveedor no responde? | La orden va a `UNDER_REVIEW`, nunca a `FAILED`. Una tarea consulta el estado real por la clave de idempotencia antes de decidir. |
| ¿Por qué el efectivo sí funciona? | Porque no hay un tercero cuya confirmación falsificar: la verdad la tiene el cajero autenticado, con arqueo y auditoría. Es como opera cualquier POS. |
| ¿Cuánto falta para producción? | Credenciales de proveedores, suite de pruebas completa, CI/CD y despliegue. Está en `docs/api-integrations.md` con nombre y apellido. |
