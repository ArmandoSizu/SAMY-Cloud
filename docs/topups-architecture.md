# Arquitectura de Recargas

Como una frase de mostrador — *"ponme $100 de Telcel"* — se convierte en una
recarga entregada, y por que hay tantas capas entre una cosa y la otra.

```
        Catalogo comercial          lo que SAMY Cloud decide vender
                 |
   Version del producto comercial   lo que ofrecia el dia que se vendio
                 |
      Mapping de proveedor          que identificador usa cada proveedor
                 |
        Provider Router             por donde sale, sin failover
                 |
      Adaptador TopupProvider       ReloadlyProvider, TaecelProvider
                 |
          API externa               Reloadly, Taecel
```

---

## Por que dos catalogos

Hay dos, y confundirlos es el error que esta arquitectura existe para impedir.

| | Catalogo tecnico (`apps.catalog`) | Catalogo comercial (`apps.commercial`) |
|---|---|---|
| Que es | Lo que el proveedor dice que vende | Lo que SAMY Cloud decide vender |
| Quien lo llena | `provider.fetch_catalog()`, solo | Una persona, contra la fuente oficial |
| Como se ve | `Recarga $89.85 MXN` | `Amigo Sin Limite 100` |
| Para que sirve | Probar la tuberia de extremo a extremo | El mostrador |
| Se puede cobrar | Solo como prueba tecnica | Si cumple las cuatro condiciones |

El catalogo tecnico **se conserva**. Es con lo que se comprobo el flujo
completo (`CENTRO-260906-JG7K`, folio Reloadly 179212) y sigue siendo el
laboratorio: idempotencia, eventos, recuperacion, comprobantes. Lo que cambia
es que deja de ser la experiencia normal del cajero, porque `$89.85` no es un
producto que nadie pida en una tiendita mexicana.

---

## La regla que gobierna todo

```
OFICIAL != EJECUTABLE
```

Que Telcel publique oficialmente un paquete no significa que nosotros podamos
entregarlo. Hacen falta las cuatro:

```
official_verified          lo leimos en la fuente oficial y sabemos cuando
+ mapping valido           algun proveedor sabe que identificador mandar
+ proveedor disponible     ese proveedor responde AHORA
+ ambiente correcto        el mapping es del ambiente en el que operamos
= SELLABLE
```

Vive en `apps/commercial/services.py::disponibilidad()`, y **el resultado no
se guarda en la base**. Guardarlo seria comodo e indexable, y seria una
mentira con fecha de caducidad: el dia que el proveedor se cae, la columna
seguiria diciendo `AVAILABLE` y el cajero cobraria algo que nadie va a
entregar. Se calcula siempre, contra el estado del momento.

Hay una prueba que fija esto: el mismo producto, sin tocar la base, responde
distinto segun la salud de los proveedores.

### Los ocho estados

Cinco los decide una persona y se guardan; tres se deducen y no se persisten
nunca.

| Estado | Quien lo pone | Se puede cobrar |
|---|---|---|
| `AVAILABLE` | calculado | **si, el unico** |
| `DISABLED` | persona | no |
| `EXPIRED` | persona o fecha | no |
| `REVIEW_REQUIRED` | persona o sincronizacion | no |
| `SANDBOX_ONLY` | persona | no |
| `PROVIDER_NOT_MAPPED` | calculado | no |
| `PROVIDER_OFFLINE` | calculado | no |
| `UNAVAILABLE` | calculado | no |

---

## Por que las versiones

Los operadores cambian GB, vigencias y beneficios sin avisar y sin renombrar
el producto. Si esos datos vivieran en `CommercialProduct`, actualizarlos
reescribiria el pasado: un ticket de hace tres meses empezaria a decir que
llevaba 9 GB cuando se vendio con 3.

Por eso la orden guarda un **snapshot** de la version vendida
(`CommercialProductVersion.snapshot()`), con su contenido y no con una
referencia. El comprobante historico siempre representa lo que realmente se
vendio ese dia, aunque el producto cambie manana.

---

## Por que NO hay failover automatico

Es la decision menos intuitiva de todo el modulo, asi que conviene el detalle.

Un envio de recarga puede terminar de **tres** formas, no dos: exito, fallo, y
**no se sabe**. La tercera es la que importa. Un timeout no significa que la
recarga no se aplico; significa que no llego la respuesta. La recarga puede
estar perfectamente entregada del otro lado.

Si ante ese "no se sabe" el sistema reintentara con otro proveedor, el
desenlace mas probable no es "se salvo la venta": son **dos recargas al mismo
telefono y una sola cobrada**. La diferencia la pone la tienda, y no se
recupera, porque el saldo ya esta en el telefono de alguien que se fue.

Por eso, en `apps/commercial/router.py`:

* No existe funcion publica que devuelva "el siguiente proveedor". Hay una
  prueba que lo comprueba por introspeccion del modulo.
* `ruta_para_reintento()` **exige** el proveedor original y levanta
  `CambioDeProveedorProhibido` si ese mapping ya no sirve — incluso cuando hay
  otro proveedor perfectamente disponible al lado.
* Cambiar de proveedor es una decision de una persona, no de un `except`.

**La idempotencia vale mas que la disponibilidad.** Una venta perdida se
vuelve a hacer; una recarga duplicada se paga.

---

## Sincronizacion sin sorpresas

`ProviderProductMapping.provider_fingerprint` guarda como se veia el producto
del proveedor la ultima vez que se verifico. Si la siguiente sincronizacion lo
encuentra distinto — otro precio, otro identificador —, el mapping pasa a
`REVIEW_REQUIRED` **en vez de actualizarse solo**.

Un cambio silencioso ahi significa cobrar una cosa y entregar otra.

---

## Como encaja con el resto

```
  Cajero
    |
    |  1. elige operador -> familia -> monto      (core, BFF)
    v
  Core  --crea la recarga-->  Topups        (PENDING_PAYMENT)
    |
    |--crea la orden------->  Payments      (CREATED)
    |
    |  2. cobra
    v
  Conekta  --tokeniza y cobra-->  Payments  (PAYMENT_PENDING -> PAID)
                                     |
                                     |  order.paid  (outbox -> Redis Streams)
                                     v
                                  Topups
                                     |  ProviderRouter elige la ruta
                                     v
                              TopupProvider  -->  API externa
                                     |
                                     |  fulfillment.result
                                     v
                                  Payments   (PROCESSING -> SUCCESS)
                                     |
                                     v
                                Comprobante  (con el snapshot del producto)
```

La regla del dinero se cumple en las dos puntas: `confirm_payment()` es la
unica puerta hacia `order.paid`, y la recarga arranca en `PENDING_PAYMENT` y
**solo** pasa a `QUEUED` cuando ese evento llega. No hay ningun camino que
entregue antes de cobrar.

Los webhooks de Conekta son obligatorios en produccion porque el estado de un
pago cambia de forma asincrona: 3D Secure, antifraude, capturas diferidas,
reembolsos y contracargos ocurren DESPUES de la respuesta inmediata. La verdad
se construye con tres fuentes — respuesta inmediata, conciliacion y webhook
firmado — y ninguna sobra.

---

## Estado hoy

| Pieza | Estado |
|---|---|
| Catalogo comercial | 68 productos cargados y verificados |
| Telcel | 4 familias, 35 productos (3 en revision) |
| Movistar, AT&T, Unefon | denominaciones sin promesa de beneficios |
| Mappings de proveedor | **ninguno** |
| Productos vendibles hoy | **0** |
| Reloadly | `READY` en SANDBOX |
| Taecel | `PENDING_CONTRACT` |

Cero vendibles es el resultado correcto, no un fallo: el catalogo esta
verificado pero ningun proveedor configurado sabe ejecutar estos productos.
La pantalla de caja lo dice con "Temporalmente no disponible" y la de
administracion con "Sin proveedor operativo".

Para que dejen de estar bloqueados hace falta el proveedor comercial. Ver
`docs/proveedores-recargas-mexico.md`.


---

## Del catálogo del proveedor al catálogo de venta

Hay **tres** catálogos, no dos, y confundirlos es el error que esta sección
existe para prevenir.

```
  apps.catalog                  Lo que el proveedor sabe vender hoy.
        |                       Se llena solo. Puede decir "$89.85".
        |
  ProviderCatalogItem           FOTOGRAFÍA del catálogo del proveedor.
        |                       Familia + nombre + SKU + importe, con fecha.
        |                       Evidencia para poder revisar un mapping.
        |
  ProviderProductMapping        El puente. Nace SIEMPRE deshabilitado.
        |                       Lo aprueba una persona, no el emparejador.
        |
  CommercialProduct             Lo que SAMY Cloud vende, como lo pide el
                                cliente: "un Amigo Sin Límite de $100".
```

### La regla del emparejamiento

    UN MAPPING POR PRECIO NO ES UN MAPPING.

Una coincidencia exige las **cuatro** partes de la identidad:

    operador + familia + código/SKU + importe

El caso real que lo justifica: Telcel vende, con la misma marca y el mismo
precio de $100, cosas distintas según sea *recarga de saldo* o *paquete Amigo
Sin Límite*. Un emparejador que compare importes elige el equivocado, el
cliente paga una cosa y recibe otra, y el dinero ya se movió.

Las tres primeras partes se comparan **por nombre, no por parecido**. No hay
distancia de edición ni «contiene». Cuando el proveedor llama distinto a una
familia, hace falta un alias que **escribe una persona** en
`CommercialFamily.provider_aliases`. Sin alias no se adivina: el reporte dice
`FAMILIA_NO_RECONOCIDA` y nombra las familias que el proveedor sí declara.

### Los dos comandos

```bash
# 1. Traer el catálogo real del proveedor. Falla si el proveedor no está
#    operativo, y entonces NO escribe una sola fila.
manage.py importar_catalogo_proveedor --proveedor taecel [--seco]

# 2. Proponer mappings. En seco por omisión.
manage.py emparejar_catalogo_proveedor --proveedor taecel [--guardar]
```

El reporte del segundo trae una línea que conviene leer siempre:

```
  Amigo Sin Limite 100  -> SL100  [Amigo Sin Limite / Amigo Sin Limite 100]
      por precio habría elegido también: TAE100, PIA100
```

Esos son los SKUs que un emparejador por importe habría aceptado. Verlos es lo
que convierte la regla en algo evidente en vez de una norma que alguien
relajará el día que tenga prisa.

### Lo que ninguno de los dos comandos puede hacer

Volver vendible un producto. `guardar()` escribe `enabled=False` y
`status=REVIEW_REQUIRED`, y hay una prueba
(`GuardarNoAutoriza.test_el_producto_sigue_sin_ser_vendible`) que falla si eso
cambia. La autorización es un acto humano en el panel de plataforma,
comparando nuestra fila con la del proveedor.

### Cuando el proveedor cambia algo

`ProviderCatalogItem.fingerprint` es la huella de la identidad, no de la fila
entera: si incluyera `raw`, cualquier campo decorativo que el proveedor
agregara marcaría todos los mappings para revisión y la señal se volvería
ruido. Si la huella cambia, los mappings que dependían de ese SKU pasan a
`REVIEW_REQUIRED` y dejan de vender. **No se actualizan solos.**

Un producto que el proveedor deja de traer se marca `active=False`, no se
borra: una fila retirada sigue explicando por qué un mapping dejó de servir.
