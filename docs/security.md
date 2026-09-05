# Seguridad de SAMY Cloud

No se confía en que "Django ya lo protege". Django da buenas bases, pero casi todo lo
que sigue hay que configurarlo explícitamente.

---

## 1. Modelo de amenazas

Quién podría atacar esto y qué buscaría:

| Actor | Motivación | Vector más probable |
|---|---|---|
| **Cajero deshonesto** | Cobrar sin entregar, o quedarse con efectivo | Confirmar cobros no recibidos; ver datos de otras tiendas |
| **Dueño de otra tienda** | Ver operaciones de la competencia | Manipular identificadores en la URL |
| **Atacante externo** | Dinero | Webhook falsificado; fuerza bruta al login |
| **Atacante con acceso a la red interna** | Movimiento lateral | Llamar directamente a un microservicio |
| **Curioso interno** | Datos de clientes | Leer logs o la base de datos |

Cada uno tiene un control asignado más abajo.

---

## 2. Autenticación

| Control | Implementación |
|---|---|
| Hash de contraseñas | **Argon2id** (recomendación OWASP actual), PBKDF2 solo para verificar hashes heredados |
| Longitud mínima | 12 caracteres + validadores de Django |
| Fuerza bruta | `django-axes`: 5 intentos por IP+usuario, bloqueo 15 min |
| Enumeración de cuentas | **El mismo mensaje** para "no existe" y "contraseña incorrecta" |
| Sesión | Cookie `HttpOnly`, `Secure`, `SameSite=Lax`, 8 h (un turno de caja) |
| Fijación de sesión | Django rota el identificador al autenticar |
| Recuperación | Token firmado de un solo uso, con caducidad |
| Cierre de sesión | Solo por **POST** — un `<img src="/salir/">` no debe cerrar la sesión |

---

## 3. Autorización

**Denegar por defecto.** Una vista sin permiso declarado **no se sirve**:

```python
class PermissionRequiredMixin(AccessMixin):
    required_permission: str | None = None

    def dispatch(self, request, *args, **kwargs):
        if self.required_permission is None:
            raise ImproperlyConfiguredPermission(...)   # falla ruidosamente
```

Y en la API, `HasStorePermission.has_permission()` devuelve `False` si la vista no
declaró permiso. Un olvido se convierte en un fallo visible, no en una puerta abierta.

### Las dos preguntas, siempre en este orden

1. **¿El usuario pertenece a esta tienda?** (aislamiento multi-tenant)
2. **¿Su rol incluye este permiso?** (RBAC)

Saltarse la primera es la fuga clásica de un SaaS: un cajero de la tienda A que cambia un
UUID en la URL y ve la venta de la B.

### Matriz de permisos

| Permiso | PLATFORM_ADMIN | STORE_OWNER | CASHIER |
|---|:---:|:---:|:---:|
| `operation.create` | ✅ | ✅ | ✅ |
| `operation.view_own` | ✅ | ✅ | ✅ |
| `operation.view_store` | ✅ | ✅ | ❌ |
| `operation.refund` | ✅ | ✅ | ❌ |
| `store.view_commissions` | ✅ | ✅ | ❌ |
| `store.manage_settings` | ✅ | ✅ | ❌ |
| `store.manage_employees` | ✅ | ✅ | ❌ |
| `platform.*` | ✅ | ❌ | ❌ |

**El cajero no ve comisiones ni reportes de la tienda, y no puede reembolsar.**

---

## 4. Aislamiento multi-tenant

Tres capas independientes:

1. **`StoreScopedQuerySet`** obliga a declarar el ámbito (`for_store`, `for_user`).
2. **`CurrentStoreMiddleware`** revalida la membresía **en cada petición**. El
   `store_id` en sesión indica *qué tienda se quiere usar*, no *que se tenga acceso*. Si
   se revoca el acceso a un cajero, deja de operar en la siguiente petición.
3. **Consultas por identificador filtran también por tienda** y devuelven **404, no 403**:
   no se confirma siquiera que el identificador exista.

---

## 5. Comunicación entre servicios

Los microservicios **no se confían por estar en la misma red**.

```
X-Samy-Signature: HMAC-SHA256(secreto, string_canónico)
X-Samy-Timestamp: 1757040000
X-Samy-Nonce:     a3f9...
X-Samy-Service:   core

string_canónico = v1\nMÉTODO\nRUTA\nTIMESTAMP\nNONCE\nSERVICIO\nSHA256(cuerpo)
```

| Ataque | Defensa |
|---|---|
| Suplantación | Sin el secreto no se puede forjar la firma |
| Repetición (replay) | Ventana de 5 min + **nonce de un solo uso** en Redis (`cache.add`, atómico) |
| Manipulación del cuerpo | El hash del cuerpo entra en la firma |
| Servicio no autorizado | Lista blanca explícita de llamantes |
| Ataque de tiempo | `hmac.compare_digest` en todas las comparaciones |

Un 401 nunca explica **por qué** falló la firma: eso ayudaría a afinar el siguiente intento.

---

## 6. Webhooks

Un webhook es **una instrucción de un desconocido para marcar dinero como cobrado**.

- Se verifica la firma **antes** de leer el cuerpo. Conekta usa RSA-SHA256 con la llave
  pública del panel, en el header `digest`.
- Firma inválida → se rechaza y se registra como error. Nunca "por si acaso".
- Sin llave pública configurada → **se rechaza el evento**, no se acepta sin verificar.
- Deduplicación por `event_id` en una ventana de 24 h.
- La confirmación es idempotente: un webhook reenviado encuentra la orden ya en `PAID` y
  no vuelve a emitir el evento que dispara la recarga.

---

## 7. Datos de tarjeta y PCI DSS

**Nunca se almacena:** PAN completo, CVV, banda magnética, PIN.

**Estrategia:** checkout hospedado y tokenización del proveedor. El número de tarjeta
**no pasa por nuestros servidores**, lo que mantiene el alcance en **SAQ A**, el nivel
más bajo.

Defensa en profundidad, por si alguien pasa un dato por error:

| Capa | Control |
|---|---|
| Logs | `scrub_text()` sustituye cualquier secuencia con forma de PAN |
| Logs | Claves prohibidas (`cvv`, `card_number`, `password`, `token`…) → `[REDACTED]` |
| Respuestas de proveedor | `ConektaProvider._safe()` limpia antes de persistir |
| Auditoría | `_sanitize()` filtra las claves prohibidas del `metadata` |
| Comprobantes | Referencias enmascaradas (`mask_reference`, `mask_phone`) |

---

## 8. Cabeceras y configuración de producción

```python
SECURE_SSL_REDIRECT = True
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_HSTS_SECONDS = 3600          # se sube tras verificar todo el dominio
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "strict-origin-when-cross-origin"
X_FRAME_OPTIONS = "DENY"
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
```

**CSP sin `unsafe-inline` en scripts.** HTMX y Alpine se sirven desde nuestro propio
origen, no desde un CDN: además de permitir una CSP estricta, elimina un punto de fallo
y un riesgo de cadena de suministro en un sistema que cobra dinero.

**Permissions-Policy:** `camera=(self)` — el lector de códigos la necesita. Todo lo demás
(micrófono, geolocalización, USB) se niega.

**`ALLOWED_HOSTS` sin valor por defecto:** si falta o contiene `*`, **el servicio no
arranca**. Es preferible no arrancar a arrancar inseguro.

---

## 9. Gestión de secretos

| Regla | Cómo se cumple |
|---|---|
| Nada de secretos en el repositorio | `.gitignore` excluye `.env`, `*.pem`, `*.key` |
| `SECRET_KEY` sin valor por defecto | `env.str("DJANGO_SECRET_KEY")` sin `default` |
| Documentación de qué se necesita | `.env.example` versionado, con valores vacíos |
| Producción | Secret Manager del proveedor, inyectado como variable de entorno |
| Rotación | El secreto S2S está versionado en el string canónico (`v1`) |

---

## 10. Auditoría

`AuditEvent` es **append-only**: `save()` bloquea la modificación de un registro existente
y `delete()` levanta una excepción. Un registro que se puede editar no sirve como evidencia.

Se guarda: quién (id y correo, porque el usuario puede borrarse), rol, tienda, IP, agente,
sesión, objeto, estado anterior y nuevo, `correlation_id` y momento.

**Escribir auditoría nunca tumba la operación:** si falla, se registra en el log y el
negocio continúa. Perder una línea de auditoría es malo; perder el cobro del cliente
porque la tabla estaba llena es peor.

---

## 11. Cobertura del OWASP Top 10

| Riesgo | Control principal |
|---|---|
| A01 Control de acceso roto | Denegar por defecto + revalidación de membresía + 404 en vez de 403 |
| A02 Fallos criptográficos | Argon2id, HMAC-SHA256, TLS obligatorio, sin PAN almacenado |
| A03 Inyección | ORM de Django parametrizado; sin SQL construido por concatenación |
| A04 Diseño inseguro | Máquina de estados que impide ejecutar sin cobrar |
| A05 Mala configuración | Settings separados; producción no hereda valores de desarrollo |
| A06 Componentes vulnerables | Versiones fijadas con límite superior de *major* |
| A07 Fallos de identificación | `django-axes`, sin enumeración de cuentas, rotación de sesión |
| A08 Integridad de datos | Firma S2S, verificación de webhooks, auditoría inmutable |
| A09 Fallos de registro | Logs JSON correlacionados + auditoría append-only |
| A10 SSRF | No se aceptan URLs del usuario; los destinos salen de configuración |

---

## 12. Lo que falta antes de operar con dinero real

Honestidad sobre el estado:

- [ ] Segundo factor para `STORE_OWNER` y `PLATFORM_ADMIN`
- [ ] Pentest externo
- [ ] Rotación automática del secreto S2S
- [ ] Alertas sobre patrones anómalos (recargas atípicas, montos fuera de rango)
- [ ] Respaldos probados **con restauración verificada** (un respaldo sin restaurar no es un respaldo)
- [ ] Plan de respuesta a incidentes escrito
- [ ] Revisión de cumplimiento con el proveedor de pagos
