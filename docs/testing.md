# Estrategia de pruebas

## Principio

En un sistema que mueve dinero, la pregunta no es "¿pasa la prueba?" sino
**"¿qué pasa cuando algo falla a la mitad?"**. Las pruebas más valiosas de este
proyecto no son las del camino feliz.

## Pirámide

```
        ╱ E2E ╲            pocas, sobre el flujo vertical completo
      ╱─────────╲
    ╱ Integración ╲        API + base de datos real + proveedor simulado
  ╱─────────────────╲
╱   Unitarias         ╲    dinero, estados, comisiones, validaciones
```

## Qué se prueba en cada nivel

### Unitarias (rápidas, sin base de datos)

| Módulo | Casos críticos |
|---|---|
| `samy_common.money` | Redondeo comercial; `allocate()` nunca pierde ni inventa centavos; rechaza `float` |
| `samy_common.states` | **`CREATED` no puede ir a `PROCESSING`**; estados finales no admiten transición |
| `samy_common.phone` | Formatos reales de captura; ladas de 2 y 3 dígitos; prefijos 044/045/01 |
| `samy_common.security.signing` | Firma válida; firma alterada; timestamp fuera de ventana; nonce repetido |
| `commissions.engine` | Fija, porcentual, mixta, con mínimo y máximo; el reparto siempre suma la comisión |

### Integración (con PostgreSQL real)

- **Idempotencia:** dos peticiones concurrentes con la misma clave crean **una** orden.
- **Aislamiento multi-tenant:** la tienda A recibe 404 al consultar una orden de la B.
- **Máquina de estados:** intentar ejecutar un servicio sin pagar levanta `IllegalTransition`.
- **Outbox:** si la publicación falla, el evento sigue pendiente y se reintenta.
- **Constraints:** la base rechaza una orden cuyo total no cuadre con base + comisión.
- **Proveedor sin credenciales:** la orden **no** avanza de estado y se responde 503.

### End-to-end

El flujo vertical completo: login → dashboard → recargas → seleccionar → confirmar →
cobrar en efectivo → comprobante. Con verificación de que el estado final en base de
datos es el correcto.

## Casos que NO deben faltar

Estos son los que separan un sistema de dinero de una demo:

1. **Webhook duplicado** → una sola orden pagada, un solo evento emitido.
2. **Webhook con firma inválida** → rechazado, orden intacta.
3. **Timeout tras enviar la petición** → la orden va a `UNDER_REVIEW`, **nunca** a
   `FAILED`, y la conciliación la resuelve consultando al proveedor.
4. **Recarga fallida con cobro exitoso** → la orden pasa sola a `REFUND_PENDING`.
5. **QR expirado** → no se puede consumir; y antes de expirar se consulta al proveedor
   por si el pago entró en el último segundo.
6. **QR reutilizado** → el segundo intento falla (garantizado por `UPDATE ... WHERE
   consumed_at IS NULL`, no por un `if`).

## Ejecución

```bash
docker compose exec payments pytest -v
docker compose exec payments pytest --cov=apps --cov-report=term-missing
docker compose exec core     pytest -v
```

## Objetivos de cobertura

| Área | Mínimo |
|---|---|
| `samy_common.money` y `states` | 100% — es la aritmética del dinero |
| Servicios de aplicación (`services.py`) | 90% |
| Adaptadores de proveedor | 80% (con respuestas grabadas, nunca inventadas) |
| Vistas y serializadores | 70% |

> Las respuestas de proveedor usadas en pruebas deben ser **capturas reales** de su
> sandbox, no ejemplos inventados. Una prueba contra una respuesta imaginaria valida
> la imaginación, no la integración.
