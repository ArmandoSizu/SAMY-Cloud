# Linntae

> **ESTADO: integración DEMO.** No hay producción certificada. Ninguna recarga
> real se ha enviado por este proveedor.

## 1. Propósito

Linntae es un proveedor mexicano de tiempo aire, pago de servicios, pines y
peajes, con API REST y **especificación OpenAPI publicada** (v2.0.0).

Esa última parte es la diferencia importante frente a TAECEL: las rutas, los
campos y los códigos del adaptador de Linntae vienen de su documento, no de la
memoria de nadie. Lo que sigue sin conocerse está enumerado en la sección
[§15 Lo que Linntae no publica](#15-lo-que-linntae-no-publica) y está protegido
por configuración, no por buena voluntad.

Linntae entra como proveedor **adicional**. No reemplaza nada:

| Proveedor | Sigue existiendo | Para qué |
|---|---|---|
| Reloadly | Sí | Laboratorio de recargas, sandbox |
| TAECEL | Sí | Recargas comerciales MX, API en trámite |
| **Linntae** | **Nuevo** | Recargas MX, candidato principal en DEMO |
| Conekta | Sí | **Pasarela de pago al cliente.** Otra cosa |

Linntae y Conekta no compiten: Conekta cobra al cliente, Linntae entrega la
recarga. El orden es siempre pago primero, entrega después.

## 2. Ambientes

| Ambiente | URL base | Host exigido |
|---|---|---|
| DEMO | `https://apidemo.linn.mx/api/v1/` | `apidemo.linn.mx` |
| PRODUCCIÓN | `https://api.linn.mx/api/v1/` | `api.linn.mx` |

`LINNTAE_ENV` acepta cuatro escrituras y sólo cuatro: `demo` y `sandbox` (que
significan DEMO), `production` y `prod` (que significan PRODUCCIÓN). La tabla
está en `config/settings/base.py` y acepta `prod` porque
`samy_common.providers.environment` ya lo acepta para `ENVIRONMENT`: dos
variables que describen el mismo concepto con vocabularios distintos son una
trampa. Cualquier otra cosa —`pro`, `produccion`, un dedazo— **detiene el
arranque** en vez de convertirse en DEMO silenciosamente. Una prueba impide que
esa tabla y la de hosts se separen.

La pareja `LINNTAE_ENV` ↔ `LINNTAE_BASE_URL` se valida en el cliente
(`problemas_de_ambiente()`), y `demo` **no puede** hablar con `api.linn.mx` ni
al contrario. El adaptador se niega antes de abrir la conexión.

No es paranoia decorativa: cruzar esas dos variables es la forma más directa de
mandar una recarga real creyendo que es de prueba, y no produce ningún error
visible cuando ocurre — produce una recarga.

Encima de eso siguen actuando las guardas que ya existían:

* `samy_common.providers.environment.verificar_ambiente()` exige que
  `ENVIRONMENT=production` ⇔ proveedor en modo `PRODUCTION`. La suite corre con
  `ENVIRONMENT=test`, así que **ninguna prueba puede operar contra producción**.
* `ALLOW_REAL_PROVIDER_TRANSACTIONS` es el interruptor final y es independiente
  del ambiente.

## 3. Arquitectura

```
services/topups/apps/providers/linntae/
    codigos.py        códigos de Linntae -> consecuencias de dinero
    parseo.py         respuestas -> tipos de SAMY (dinero, catálogo, comisiones)
    auth.py           política del token (caché, renovación, tope de reintentos)
    client.py         transporte HTTP (timeouts, guarda de host, leer/comprar)
    conciliacion.py   averiguar qué pasó sin reintentar
    provider.py       LinntaeProvider: implementa TopupProvider
```

La separación no es organizativa, es funcional: `codigos.py` y `parseo.py` se
prueban enteros sin red, y ahí vive la lógica que decide si se cobra, si se
reembolsa o si una persona tiene que mirarlo.

Dos decisiones estructurales que conviene conocer antes de tocar el código:

**`client.leer()` y `client.comprar()` son dos métodos distintos.** No es un
`if` dentro de uno común. Las lecturas se reintentan; las compras no, y
`comprar()` literalmente no tiene bucle. Un `if` se puede invertir al
refactorizar; un bucle que no existe, no.

**`TopupProvider.estado_por_contexto()` es nueva y opcional.** Los dos ganchos
de conciliación que ya existían suponen que el proveedor sabe contestar "¿cómo
quedó la operación con tu folio X?" o "¿con mi referencia Y?". Linntae no sabe
contestar a ninguna de las dos (§9). El gancho nuevo lleva los datos de la
operación. Devuelve `None` por omisión, así que Reloadly y TAECEL conciliaron
ayer y conciliarán mañana exactamente igual.

## 4. Endpoints implementados

| Endpoint | Método | Para qué | Reintentable |
|---|---|---|---|
| `/getToken` | POST | Autenticación | Sí (lectura) |
| `/balance/getBalance` | POST | Saldo de las tres bolsas | Sí |
| `/products/taeCompanies` | GET | Operadores tradicionales | Sí |
| `/products/taeVirtualCompanies` | GET | OMV / virtuales | Sí |
| `/config/syncProducts` | GET | Catálogo completo | Sí |
| `/config/getProductsCommissions` | GET | Comisiones de **nuestra** cuenta | Sí |
| `/config/getScheme` | GET | Esquema comercial de la cuenta | Sí |
| `/purchase/tae` | POST | **Recarga. Mueve dinero.** | **NUNCA** |
| `/sale/checkTransacctionTae` | POST | Estado, primeros 60 s | Sí |
| `/sale/list` | POST | Histórico de ventas | Sí |

`/config/getScheme` no estaba en la lista pedida y se implementó igual: es de
solo lectura y es la pista más directa sobre **cómo** aplica Linntae su
comisión, que es justo la pregunta abierta de §11.

`checkTransacctionTae` lleva la doble "c" porque es la ruta real. Corregir la
errata produce un 404, y un 404 ahí significa quedarse sin poder averiguar qué
pasó con una recarga.

### No implementados a propósito

`/products/services`, `/purchase/checkBalance`, `/purchase/service`,
`/products/pin`, `/purchase/pin`, `/products/peaje`, `/purchase/peaje`,
`/deposit/*`, `/movements/*`, `/config/syncBanks`, `/config/syncSchedule`,
`/config/registerPdv`, `/sale/checkTransacctionServicio`.

El cliente está hecho para que añadirlos sea escribir un método, no rehacer
nada. `/sale/unlock` es el único que **no se va a implementar** como llamada
automática (§14).

## 5. Variables de entorno

Todas en `.env`, que ya está en `.gitignore` y debe seguir estándolo. Los
valores reales no aparecen en este documento, ni en el repositorio, ni en los
logs, ni en la imagen de Docker, ni en el frontend.

| Variable | Por omisión | Qué pasa si falta |
|---|---|---|
| `LINNTAE_ENV` | `demo` | Sólo `demo`/`sandbox`/`production`/`prod`; cualquier otra cosa impide el arranque |
| `LINNTAE_BASE_URL` | URL de DEMO | Vacía ⇒ `NOT_CONFIGURED` |
| `LINNTAE_USERNAME` | — | `NOT_CONFIGURED` |
| `LINNTAE_PASSWORD` | — | `NOT_CONFIGURED` |
| `LINNTAE_TYPE_BALANCE` | **vacío** | `DEGRADED`: el producto **no es vendible** |
| `LINNTAE_EXTRA_COMISION` | `0` | Fuera de 0..5 ⇒ la recarga no se envía |
| `LINNTAE_TOKEN_TTL_SECONDS` | `600` | — |
| `LINNTAE_CONNECT_TIMEOUT` | `5.0` | — |
| `LINNTAE_READ_TIMEOUT` | `30.0` | — |
| `LINNTAE_REINTENTOS_LECTURA` | `2` | — |
| `LINNTAE_COMMISSION_MECHANISM` | `SIN_DETERMINAR` | Margen desconocido (correcto hoy) |
| `LINNTAE_ENABLED` | `false` | Configurado pero sin autorización de gasto |
| `ALLOW_REAL_PROVIDER_TRANSACTIONS` | `false` | **Ninguna compra sale de SAMY** |
| `TOPUP_MEDIR_SALDO_PROVIDERS` | `linntae` | Sin medición del mecanismo |

`LINNTAE_TYPE_BALANCE` merece su párrafo. Linntae exige `typeBalance` como
entero obligatorio en cada compra y **no publica su enumeración**. Que `1` sea
la bolsa de plataforma es una inferencia de sus ejemplos, no un dato. Mientras
esté vacío, `check_health()` devuelve `DEGRADED` y el producto no se puede
vender — y eso es lo correcto: un producto vendible cuyo `typeBalance` falta
produciría la secuencia prohibida, cobrar al cliente y descubrir después que la
recarga no se puede mandar.

## 6. Códigos de Linntae

Los códigos viajan **dentro de un HTTP 200**. Un 200 no significa éxito;
significa que la conversación funcionó. El éxito es `code == 0`.

### En una compra

| `code` | Significado | Consecuencia en SAMY |
|---|---|---|
| 0 | Correcta | `EJECUTADA` → `SUCCEEDED` |
| 1 | Datos, producto, teléfono o ganancia inválidos | `NO_EJECUTADA` → `FAILED` |
| 2 | Tipo de saldo no disponible | `NO_EJECUTADA` → `FAILED` |
| 3 | Saldo plataforma insuficiente | `NO_EJECUTADA` → `FAILED` |
| 22 | La compañía presenta fallas | **`INDETERMINADA`** → revisión |
| 23 | Sistema en mantenimiento | `NO_EJECUTADA_TRANSITORIA` → `FAILED` |
| 24 | Venta duplicada | **`DUPLICADA`** → revisión |
| 25 | Autorizada, código en recuperación (pines) | `EJECUTADA` |
| *desconocido* | — | **`INDETERMINADA`** |

**Por qué 22 y 23 se tratan distinto.** `code 23` aparece también en los
endpoints de solo lectura (`/products/*`), y eso prueba que es una puerta
global del sistema: si está en mantenimiento, la venta no entró. `code 22`
aparece solo en los endpoints de compra y nombra una compañía concreta; es el
desenlace de un intento y la especificación no dice en qué momento falló.

El empate se rompe hacia la duda porque el costo no es simétrico: dar por
fallida una recarga que sí se aplicó significa reembolsar a un cliente que ya
tiene su saldo, y ese dinero no se recupera. Dar por indeterminada una que
falló cuesta una consulta.

### HTTP

| HTTP | En lectura | En compra |
|---|---|---|
| 401 | Renovar token, un reintento | No ejecutada (la seguridad corre antes de la lógica) |
| 403 con `country-XX-403` | `GEOBLOQUEO`, sin reintentos | No ejecutada |
| 403 otro | `PERMISOS`, sin reintentos | No ejecutada |
| 422 | No ejecutada | No ejecutada |
| 500 | Fallo de lectura, reintentable | **INDETERMINADA** |
| 503 | Reintentable | **INDETERMINADA** |

Linntae usa **el mismo 403** para un token inválido y para un bloqueo
geográfico. Confundirlos produce o un bucle de renovación de token o una
integración caída hasta que alguien reinicie; el mensaje
(`Error country-US-403`) es lo único que los distingue.

Y `code 4` **no tiene un significado único**: en `/getToken` son credenciales
malas, en `/sale/unlock` es que no existe la venta, en `/purchase/pin` es que
la venta de pines está bloqueada. Se traduce por endpoint, nunca globalmente.

## 7. Política de reintentos

| Operación | Reintentos | Por qué |
|---|---|---|
| Lecturas (saldo, catálogo, comisiones, ventas) | 2 + 1, con espera creciente | No hay dinero en juego |
| `401` en lectura | Renovar token **una** vez | Un segundo 401 se propaga |
| **Compra** | **Ninguno** | Reintentar sin saber qué pasó es como se recarga dos veces |
| `403` geográfico | Ninguno | Desde la misma IP el resultado será el mismo |
| Credenciales rechazadas | Ninguno | Linntae bloquea cuentas por intentos fallidos |

El bucle que `auth.py` evita explícitamente es `401 → token nuevo → 401 → …`.
Contra un proveedor que puede bloquear la cuenta por abuso, eso no es una
molestia: es cómo se pierde el acceso.

Los fallos de red en una compra **no son todos iguales**:

* `ConnectTimeout` / `ConnectError`: la conexión no se abrió, el cuerpo no
  salió. Es seguro afirmar que nada ocurrió.
* Cualquier otro (`ReadTimeout`, `WriteTimeout`, conexión cortada): ocurre
  después de empezar a enviar. El resultado es **desconocido**.

Antes de una compra el token se renueva si le queda menos de un minuto de
vida. Cuesta una llamada y ahorra que una caducidad rutinaria se convierta en
una revisión manual.

## 8. Idempotencia

**No se envía `Idempotency-Key` a `/purchase/tae`.** Su especificación la
documenta únicamente en `/purchase/pin` (8 a 128 caracteres). Mandarla en
tiempo aire suponiendo que también funciona sería construir la protección
contra duplicados sobre una cabecera que quizá ignoran.

La protección es de SAMY y no depende de Linntae:

* un `TopupFulfillment` por orden, con `idempotency_key` único en la base;
* máquina de estados: solo `QUEUED` o `PENDING_PAYMENT` pueden ejecutarse;
* `select_for_update()` en cada transición, así que dos workers no aplican dos
  transiciones sobre el mismo estado;
* verificación independiente del pago contra el servicio de Pagos antes de
  gastar saldo;
* sin failover automático entre proveedores, nunca.

Un doble clic del cajero, un evento duplicado o un worker reiniciado no
producen dos llamadas a Linntae.

Y hay una protección extra que es de Linntae y nos conviene: sus bloqueos por
venta reciente (15 minutos hasta $50, 3 horas por encima) hacen que un segundo
envío idéntico reciba `code 24` en vez de aplicarse.

## 9. Política de reconciliación

Cuando `/purchase/tae` no da respuesta concluyente —timeout, 500, 503,
`code 22`, `code 24`— la recarga queda en `UNDER_REVIEW` y **lo único legítimo
es preguntar**.

Linntae acepta dos preguntas, y ninguna recibe la autorización de la compra:

1. `POST /sale/checkTransacctionTae` — identifica por `idOffer` + `phoneNumber`
   y sirve los **primeros 60 segundos**. Fuera de esa ventana no se llama: una
   respuesta cuyo significado no está definido no es información.
2. `POST /sale/list` — histórico, filtrable por fecha y `reference` (el
   teléfono). Es la fuente para todo lo demás.

El emparejamiento contra el histórico exige **las tres**: mismo teléfono (por
dígitos), mismo importe, y dentro de ±15 minutos del envío. Sin la tercera, una
recarga legítima de esta mañana al mismo teléfono por el mismo monto se
emparejaría con la de esta tarde.

### La regla de oro

> Se cierra como exitosa o como fallida **solo con un registro que lo diga**.
> La **ausencia** de registro no cierra nada.

Porque la ausencia tiene dos lecturas —la venta no existió, o el histórico
todavía no la refleja— y la especificación no dice en cuánto tiempo se indexa
una venta. Entre las dos no se elige: queda en revisión. Cuesta que una persona
lo mire; la alternativa cuesta reembolsar a clientes que sí recibieron su saldo.

Dos candidatas indistinguibles tampoco cierran: elegir una sería elegir al azar
de qué operación es la evidencia.

### El folio de Linntae no es único

En el propio ejemplo de su especificación, dos ventas distintas —$200 a un
teléfono y $10 a otro, con doce minutos de diferencia— comparten el folio
`12311057912`. El identificador único de una venta es `id`. Emparejar por folio
cerraría una recarga con la evidencia de otra.

## 10. Catálogo

Linntae es el catálogo **técnico**. El catálogo **comercial** de SAMY (68
productos, `CommercialOperator` / `CommercialFamily` / `CommercialProduct` /
`CommercialProductVersion`) no se toca y sigue siendo lo que ve el cajero.

```
/config/syncProducts  ->  ProviderCatalogItem  ->  [revisión humana]  ->  venta
                                    ^
                          ProviderProductMapping
```

La especificación declara `products` de **dos formas distintas** —el ejemplo
como lista de objetos de una llave, el esquema como objeto; y la sección de
virtuales se llama `RECARGA VIRTUAL` en una y `VIRTUALES` en la otra—. El
parser acepta las dos, y clasifica las secciones **por la forma de sus filas**
(una compañía trae `idOperator` y `offers`; un servicio, pin o peaje trae
`sku`), no por su nombre. Lo que no reconoce levanta `RespuestaIlegible` en vez
de devolver una lista vacía: una lista vacía se propaga como "el proveedor no
vende nada" y termina desactivando catálogo.

### El mapping exige identidad exacta

```
operador + familia + código/SKU + importe
```

Un mapping por precio **no es un mapping**. Telcel vende con el mismo precio y
la misma marca cosas distintas según sea *recarga de saldo* o *paquete Amigo Sin
Límite*; emparejar "$100 = $100" manda al cliente el producto equivocado y le
cobra el correcto.

Estados posibles, y solo el primero permite cobrar:

| Estado | Significado |
|---|---|
| `MAPPED` | Mapping habilitado, sin observaciones, identidad completa |
| `REVIEW_REQUIRED` | Hay candidato o mapping, falta aprobación humana |
| `NOT_AVAILABLE` | Linntae no tiene nada que corresponda |

El reporte se saca con `manage.py reporte_catalogo_proveedor --proveedor linntae`
y dice **en qué paso** falló cada fila: si falta un alias del operador, un alias
de la familia, o un importe que Linntae no ofrece.

Una particularidad afortunada de Linntae: `POST /purchase/tae` **no tiene
parámetro de monto**. El importe vive dentro del `idOffer`, así que
`amount_in_sku` es literalmente cierto y no hay forma de contradecirlo mandando
un monto distinto al del SKU.

## 11. Comisiones

**No hay ningún porcentaje de Linntae escrito en el repositorio.** La fuente es
`GET /config/getProductsCommissions`, que devuelve las tasas de *nuestra*
cuenta, y se guarda en `ProviderCommission` con su texto crudo y la fecha.

Los números que Linntae dio comercialmente (≈6% en operadores tradicionales,
≈5% en virtuales, $1–$5 de comisión extra en recargas, $4–$14 de utilidad en
servicios) son referencia para negociar, no configuración. Su propia
especificación muestra ejemplos con **5.5%**, lo que confirma que depende de la
cuenta.

Las tasas llegan como texto (`"5.5%"`) y se convierten a puntos base enteros
por `Decimal`. Cuando una tasa no cabe exacta en puntos base, la fila queda
marcada `exacta_en_bps = False` en vez de redondearse en silencio.

### La pregunta abierta: el mecanismo

Saber el porcentaje **no es** saber el costo. El mismo 6% da tres costos
distintos para una recarga de $100:

| Mecanismo | Costo de $100 |
|---|---|
| `DESCUENTO_POR_TRANSACCION` | $94.00 |
| `BONO_AL_FONDEAR` (es el de TAECEL) | $94.34 |
| `COMISION_ACREDITADA_APARTE` | $100.00 |
| `SIN_DETERMINAR` | **no se sabe** |

Lo que la especificación de Linntae **sugiere**: su esquema se llama
`"1.-COMISION SOBRE VENTA (tiempo aire y pago de servicios)"`, su consulta de
saldo devuelve `plataforma` y `comision` como bolsas separadas, y su histórico
de movimientos muestra cargos a `SALDO PLATAFORMA` por importes **mayores** que
el valor facial (un CFE de $100 aparece como 111). Todo eso apunta a
`COMISION_ACREDITADA_APARTE`.

Apuntar no es saber, así que la configuración dice `SIN_DETERMINAR` y el motor
de precios reporta **margen desconocido** en vez de afirmar un número. Y si el
mecanismo resultara ser el de la comisión aparte, la consecuencia es fuerte y
hay que verla antes de fijar precios: una recarga de $100 en efectivo sin cuota
deja **cero** de margen inmediato, y con tarjeta pierde la comisión completa de
Conekta ($7.43).

### Cómo se resuelve: midiendo

`TOPUP_MEDIR_SALDO_PROVIDERS=linntae` hace que se lea el saldo **antes y
después** de cada recarga y se guarde en `TopupFulfillment.economia`. Con dos
números se distinguen los tres mecanismos:

* la bolsa de plataforma baja $100 → comisión abonada aparte;
* baja $94.34 → bono al fondear;
* baja $94.00 → descuento por transacción.

Cuando esté medido: se pone `LINNTAE_COMMISSION_MECHANISM` con el valor
observado y se saca `linntae` de `TOPUP_MEDIR_SALDO_PROVIDERS`.

## 12. Seguridad

Lo que **no** se registra nunca: contraseña, token completo, datos de tarjeta,
llave privada de Conekta, número telefónico completo.

* `TokenLinntae` define su propio `__repr__` y muestra solo longitud y
  `supportId`. Sin eso, cualquier `repr()` en un traceback o en un log
  estructurado dejaría la credencial escrita en disco — con `dataclass` eso
  pasa sin que nadie lo escriba a propósito.
* La petición a `/getToken` no registra su cuerpo ni sus llaves.
* Las demás registran ruta, HTTP, `code` y las **llaves** del cuerpo, no sus
  valores. El teléfono se registra enmascarado (`31****4567`).
* La llave de caché del token es un hash de `base_url` + usuario. El usuario no
  se escribe en ninguna llave de Redis.
* Solo HTTPS. **No existe** una bandera para desactivar la verificación de
  certificados: es la clase de opción que alguien enciende "un momento para
  probar" y se queda encendida.
* Timeouts explícitos de conexión y de lectura; sesión `httpx` reutilizable.
* Las credenciales viven solo en `.env`. No van a la imagen de Docker, ni al
  frontend, ni al repositorio.
* **El navegador jamás habla con Linntae.** Solo el backend de SAMY.

Nota aparte: `POST /config/registerPdv` devuelve una **contraseña en texto
claro** en su respuesta. No se usa ese endpoint; si alguna vez se usa, su
respuesta no puede registrarse.

## 13. Pasos para la prueba DEMO

Lo que ya está hecho no necesita repetirse. Lo que falta:

1. **Poner credenciales en `.env`** (`LINNTAE_USERNAME`, `LINNTAE_PASSWORD`).
   Las pone Sizú; no se piden por chat ni se escriben en el repositorio.
2. **Diagnóstico de solo lectura**, que no escribe nada y no llama a ningún
   endpoint de compra:

   ```
   docker compose exec topups python manage.py linntae_diagnostico
   docker compose exec topups python manage.py linntae_diagnostico --json
   ```

   Devuelve: salud, las tres bolsas de saldo, el esquema, las comisiones
   normalizadas, los operadores del catálogo y las ofertas de Telcel con su
   `idOffer`.
3. **Confirmar `typeBalance` con Linntae** y ponerlo en `LINNTAE_TYPE_BALANCE`.
   Sin esto el proveedor queda `DEGRADED` y nada es vendible.
4. **Guardar las comisiones**:

   ```
   docker compose exec topups python manage.py sincronizar_comisiones_proveedor --proveedor linntae
   ```
5. **Importar el catálogo técnico** (requiere `READY`, o sea el paso 3):

   ```
   docker compose exec topups python manage.py importar_catalogo_proveedor --proveedor linntae --seco
   docker compose exec topups python manage.py importar_catalogo_proveedor --proveedor linntae
   ```
6. **Emparejar y revisar**:

   ```
   docker compose exec topups python manage.py emparejar_catalogo_proveedor --proveedor linntae --seco
   docker compose exec topups python manage.py reporte_catalogo_proveedor --proveedor linntae
   ```

   Todo mapping nace en `REVIEW_REQUIRED` y `enabled=False`. Aprobar es un acto
   humano. Los alias de familia (`CommercialFamily.provider_aliases["linntae"]`)
   los escribe una persona: sin alias el emparejador no adivina, reporta
   `FAMILIA_NO_RECONOCIDA`.
7. **Autorización explícita de Sizú** y `ALLOW_REAL_PROVIDER_TRANSACTIONS=true`
   + `LINNTAE_ENABLED=true`.
8. **Una recarga DEMO**, previo repaso de: operador, producto, `idOffer`, monto,
   número enmascarado, `extraComision`, `typeBalance`, ambiente, saldo
   disponible y resultado esperado.
9. **Leer `TopupFulfillment.economia`** y determinar el mecanismo de comisión
   (§11).

## 14. Lo que nunca debe hacerse

* **Nunca reintentar `/purchase/tae` automáticamente.** Ni tras timeout, ni
  tras 500, ni tras 503, ni tras `code 22`, ni tras `code 24`.
* **Nunca llamar a `/sale/unlock` automáticamente.** Sus bloqueos por venta
  reciente son una protección nuestra. Quitarlos después de un resultado dudoso
  es la receta exacta para la segunda recarga. Queda reservado a
  `PLATFORM_ADMIN`, después de conciliar y con confirmación explícita.
* **Nunca cerrar como fallida una recarga por ausencia de registro.**
* **Nunca hacer failover a otro proveedor** después de una operación incierta.
* **Nunca ejecutar la recarga antes de que el pago esté confirmado.**
* **Nunca mandar `Idempotency-Key` a `/purchase/tae`** suponiendo que funciona.
* **Nunca sumar la bolsa de comisión al saldo de plataforma** para decidir si
  se puede vender.
* **Nunca hardcodear un porcentaje de comisión.**
* **Nunca usar `verify=False`**, ni registrar token, contraseña o teléfono
  completo.
* **Nunca activar producción** sin `LINNTAE_ENV=production`, host
  `api.linn.mx`, `ENVIRONMENT=production` y autorización explícita.

## 15. Lo que Linntae no publica

Estas son las preguntas abiertas. Ninguna está resuelta por suposición en el
código; cada una tiene una guarda.

| Pregunta | Estado | Guarda actual |
|---|---|---|
| Enumeración de `typeBalance` | **Desconocida** | `LINNTAE_TYPE_BALANCE` vacío ⇒ no vendible |
| Cómo aplica la comisión | **Desconocido** | `SIN_DETERMINAR` ⇒ margen desconocido |
| Vida del token | No publicada | TTL nuestro, corto, + invalidación por 401 |
| Forma real de `syncProducts` | Dos formas declaradas | El parser acepta ambas |
| Forma real de `getProductsCommissions` | Dos formas declaradas | El parser acepta ambas |
| En cuánto se indexa una venta en `/sale/list` | No publicado | La ausencia no cierra nada |
| Clave de `data` para el identificador de trx | No publicada | No se usa `data` |
| Países / IPs permitidas | **No acordado** | `PROVIDER_GEO_BLOCKED` sin reintentos |
| ¿`extraComision` cambia lo que se carga a plataforma? | No detallado | Fijo en 0 |

El geobloqueo importa para una decisión que todavía no se ha tomado: **la región
de nube**. Su especificación documenta `HTTP 403` con `Error country-US-403`, o
sea que rechazan por país de origen. Antes de elegir región hay que confirmar
con Linntae países permitidos, si exigen IP fija o whitelist, y si hace falta
servidor mexicano. Desarrollando en local desde México esto no se ve; en el
primer despliegue sí.

## 16. Producción

No está activada y no se activa por accidente. Hacen falta, a la vez:

1. `LINNTAE_ENV=production`
2. `LINNTAE_BASE_URL=https://api.linn.mx/api/v1/` (la guarda de host exige que
   coincidan)
3. `ENVIRONMENT=production` (si no, `verificar_ambiente()` lo rechaza)
4. `LINNTAE_ENABLED=true`
5. `ALLOW_REAL_PROVIDER_TRANSACTIONS=true`
6. `LINNTAE_TYPE_BALANCE` confirmado **para la cuenta productiva**
7. Mappings productivos revisados y habilitados por una persona
8. Comisiones productivas sincronizadas y mecanismo determinado
9. Región de nube compatible con su política geográfica
10. Saldo fondeado

Mientras el README diga "Linntae DEMO integration", producción no está
certificada. No se cambia esa frase hasta que lo esté.

## 17. PRODUCCIÓN: lecturas verificadas (2026-09-13)

Con el **segundo** juego de credenciales productivas, la cuenta autentica y las
seis lecturas responden `HTTP 200`. Lo que sigue son cifras de PRODUCCIÓN y no
se mezclan con las de DEMO.

| Dato | Valor |
|---|---|
| `supportId` | `105225` |
| Host | `api.linn.mx` |
| Esquema | id `1` — *1.-COMISION SOBRE VENTA (tiempo aire y pago de servicios)* |
| Saldo plataforma | `$200.00 MXN` |
| Saldo comisión | `$0.00 MXN` (la bolsa existe y está en cero) |
| Saldo servicios | no viene en la respuesta |
| Catálogo | 64 compañías, 760 ofertas, 256 con SKU |
| `taeCompanies` | 8 operadores, 96 ofertas |
| `taeVirtualCompanies` | 56 operadores, 665 ofertas |
| Comisiones | 320 filas, guardadas en `ProviderCommission` con fecha |
| 401 / 403 / 500 | ninguno |

`config/getProductsCommissions` **funciona en producción** — en DEMO devolvía
`HTTP 500`. Las tasas: 6% por operador para TELCEL, TELCEL SIN LIMITES, TELCEL
INTERNET, MOVISTAR, AT&T, UNEFON, VIRGIN y OUI MOVIL; 5% por SKU para los 56
operadores virtuales; y sin tasa legible para las ~230 filas de servicios,
tarjetas de regalo y peaje.

TELCEL trae 10 denominaciones: `idOffer` 96, 1, 2, 3, 452, 4, 5, 6, 7, 8 para
$10, $20, $30, $50, $80, $100, $150, $200, $300 y $500.

### Lo que esto cambia sobre el mecanismo de comisión, y lo que no

Dos datos nuevos apuntan en la misma dirección: el esquema de la cuenta se
llama literalmente *COMISIÓN SOBRE VENTA* y el saldo llega partido en dos
bolsas, con la de comisión en `$0.00` antes de la primera venta. Eso es
compatible con `COMISION_ACREDITADA_APARTE` y **no** con un descuento por
transacción.

Sigue siendo inferencia. `LINNTAE_COMMISSION_MECHANISM` se queda en
`SIN_DETERMINAR` hasta medirlo: saldo de las dos bolsas antes y después de una
recarga real. `TOPUP_MEDIR_SALDO_PROVIDERS=linntae` ya lo captura en
`TopupFulfillment.economia`. Poner el mecanismo por parecido sería fijar
precios con un margen que nadie comprobó.

### Discrepancias entre las tres puertas del catálogo

También en producción, y las caza el propio diagnóstico:

* UNEFON (`idOperator 3`): `syncProducts` 11 ofertas, `taeCompanies` 10.
* FLASH MOBILE (`idOperator 30`): `syncProducts` 15, `taeVirtualCompanies` 16.
* Turbo Cel (`idOperator 269`): `syncProducts` 3, `taeVirtualCompanies` 4.

El emparejamiento comercial lee `syncProducts`. Una oferta que solo existe por
la otra puerta se vería vendible y fallaría con el cobro ya hecho, así que
ninguno de esos tres operadores se habilita sin resolver la diferencia con
Linntae.

## 18. Primer intento contra PRODUCCIÓN, que falló (2026-09-13)

El **primer** juego de credenciales productivas fue rechazado. Queda escrito
porque lo que se descartó entonces sigue valiendo: ahorra repetir el
diagnóstico si vuelve a pasar.

### Lo que se observó

| Dato | Valor |
|---|---|
| URL | `https://api.linn.mx/api/v1/getToken` |
| HTTP | `200` |
| Latencia | 411 ms |
| `server` | `cloudflare` |
| Cuerpo | `{"code": 4, "message": "Datos de usuario o contraseña incorrectos"}` |

Los otros seis endpoints de lectura **no se ejecutaron**: sin token no hay
llamada que hacer. No se intentó ninguna contraseña alternativa ni ningún
formato distinto de usuario: contra un proveedor que puede bloquear cuentas
por intentos fallidos, insistir es peor que fallar, y adivinar una credencial
no es diagnosticar.

### Lo que el resultado descarta

Un `code 4` dentro de un HTTP 200 es una respuesta de negocio, no de
transporte. Eso descarta, con evidencia y no por suposición:

* **Geobloqueo.** El detector de `country-XX-403` no se activó y la respuesta
  llegó con cuerpo JSON normal. La IP de salida llega a Linntae.
* **HTTP 401/403.** No hubo ninguno. Cloudflare no interpuso nada.
* **Host equivocado.** La URL es exactamente la productiva y la guarda de host
  la aceptó.
* **Truncamiento de la contraseña al leer el `.env`.** Es la sospecha obvia
  cuando una contraseña trae `#`, porque en un `.env` ese carácter suele
  iniciar un comentario. Se comprobó sin imprimir el valor: `django-environ`
  entrega 20 caracteres, ASCII, sin comillas, sin espacios al borde, **y el
  `#` sigue dentro**. Lo que hay en el archivo es lo que sale por la red.

### Cómo se resolvió

Linntae emitió un segundo juego de credenciales para la misma cuenta y ese sí
autentica (sección 17). La causa exacta del rechazo del primero no la sabemos
y no hace falta saberla: era de su lado, como decía la evidencia.

Los tres candados siguen cerrados y `linntae_diagnostico` los imprime en cada
corrida: ambiente del servicio, autorización para mover dinero y `typeBalance`.
Autenticar contra producción no abrió ninguno, que es exactamente lo que debía
pasar.
