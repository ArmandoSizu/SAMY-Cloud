# Proveedores de recargas con API en Mexico

Investigacion para decidir con que proveedor sale SAMY Cloud a produccion.
Fecha: **6 de septiembre de 2026**.

Hoy SAMY Cloud tiene **Reloadly en SANDBOX funcionando de verdad**, comprobado
de extremo a extremo (folio Reloadly 179212, orden CENTRO-260906-JG7K). La
pregunta no es si Reloadly sirve tecnicamente: sirve. La pregunta es si sirve
para una tiendita mexicana.

---

## Hallazgo principal

**Casi ningun agregador mexicano publica su API.** De unos diez proveedores
nacionales revisados, **cero** tienen documentacion tecnica accesible sin
tramite previo. Todos usan el mismo patron: bajar un "levantamiento
tecnologico", mandarlo por correo, esperar aprobacion de ingenieria, y recibir
manuales mas credenciales de prueba. No hay endpoints, ni esquema de
autenticacion, ni codigos de error, ni politica de idempotencia visibles.

Solo dos proveedores con cobertura Mexico publican documentacion navegable sin
contrato: **Reloadly** (internacional) y **tapi / tapila** (LatAm).

**Segundo hallazgo, para no enganarse con la variedad del mercado:** varios de
los "proveedores distintos" son la misma plataforma revendida con otra marca.
Verificado: **Multiprepago, SIPREL y PrepagoTelcel comparten el mismo telefono
(+52 442 644 5052)**, el mismo proceso de tres pasos, la misma lista de
operadores y la misma estructura de URL que TAECEL. Cotizarles a los cuatro no
es cotizarle a cuatro proveedores.

---

## Reloadly (produccion)

Lo que ya sabemos por haberlo integrado, mas lo verificado ahora.

| Criterio | Hallazgo |
|---|---|
| Cobertura MX | Telcel, Movistar y AT&T confirmados por el titulo de su pagina oficial. **Unefon y Bait: NO VERIFICADO.** |
| Paquetes vs saldo | En otros paises existen operadores tipo "Bundles". Para Mexico: **NO VERIFICADO**. |
| API | REST, publica y completa. Unico junto con tapi que se puede leer antes de firmar. |
| Autenticacion | OAuth2 client_credentials contra `auth.reloadly.com`, token Bearer por `audience`. |
| Idempotencia | **`customIdentifier` esta documentado solo como referencia interna, NO como llave de idempotencia.** No hay garantia de que un reintento no duplique. |
| Webhooks | Si, con secreto de firma, hasta 10 reintentos, timeout de 3 s. |
| Consulta de estado | `GET /topups/{id}/status`. Estados SUCCESSFUL, PROCESSING, REFUNDED, FAILED. |
| Consulta de saldo | `GET /accounts/balance`. |
| Reversos | Automaticos, no manuales. No hay endpoint de cancelacion a voluntad. |
| Monedero | **USD/EUR/GBP/ZAR/NGN/INR. No hay MXN.** Minimo de abono **100 USD**. |
| Fiscal | Proveedor extranjero. **No emite CFDI mexicano.** |
| Contrato | No requiere. Se activa desde el dashboard. |
| Sandbox | Si, gratis y aislado. Es el que usamos. |

### Por que Reloadly no es el proveedor comercial para Mexico

Dos defectos estructurales, ninguno de implementacion:

1. **El monedero es en USD.** Cobramos en pesos al tendero y fondeamos en
   dolares. En cada ciclo de fondeo asumimos spread cambiario y riesgo de tipo
   de cambio, contra un margen de recargas que en Mexico es de un digito bajo.
   El tipo de cambio se puede comer la utilidad completa.
2. **No hay CFDI.** El tendero no puede deducir. Para un producto que se vende
   a negocios formales en Mexico, eso no es un detalle.

A eso se suma que Unefon y Bait no estan confirmados, y Bait pesa mucho en el
canal de barrio.

**Reloadly se queda.** Es nuestro laboratorio de integracion y es el producto
correcto el dia que SAMY Cloud haga recargas transfronterizas. Solo no es el
proveedor de mostrador.

---

## TAECEL

| Criterio | Hallazgo |
|---|---|
| Cobertura | Telcel, Movistar, AT&T, Unefon, Iusacell, Weex, Virgin, Oui, SIMPATI, Soriana Movil, FreedomPop, unas 30 companias y OMV. **Bait no aparece nombrado.** |
| Paquetes vs saldo | **Ambos, verificado.** Menciona Amigo Sin Limite, Telcel Internet y paquetes Movistar. |
| API | REST. **Documentacion NO publica.** |
| Como se obtiene | Levantamiento tecnologico (PDF publico) a `cc@taecel.com`, revision de ingenieria, codigos de prueba, luego produccion. |
| Que pide | Razon social, domicilio, **RFC**, contacto de TI, lenguaje y base de datos, modelo Mayorista o Distribuidor, numero de PDV, diagrama. |
| Idempotencia | **NO VERIFICADO.** |
| Webhooks / reversos / consulta | **NO VERIFICADO** en los tres casos. |
| Comisiones | **No publicadas.** El folleto dice "Preguntanos por el porcentaje". |
| Fondeo | Prepago en **MXN**. Minimo inicial $50, posteriores $300 segun el FAQ; el folleto de integrador dice $1,000. *Los dos documentos se contradicen.* |
| Fiscal | RFC obligatorio. **Emite CFDI.** Solo factura lo reportado en el mes en curso; cancelar factura cuesta $20 MXN. |
| Sandbox | Si, codigos de prueba. |
| Seguridad | Solo lenguaje de marketing. TLS, firma, IP allowlist: **NO VERIFICADO**. |

---

## Los demas

**tapi / tapila** — REST con referencia navegable sin contrato, auth por
`x-api-key` + `x-authorization-token`, entorno de homologacion publico. Cubre
Mexico con Telcel, AT&T, Unefon y Virgin. Pero es infraestructura para
fintechs y bancos: cuatro semanas de integracion, condiciones comerciales
ocultas, sin mínimos ni comisiones publicadas.

**Sivetel** — Segunda opcion mexicana real. SOAP **y** REST, catalogo
comparable (unos 40 operadores, mas de 60 servicios, pines), opera en MXN.
Documentacion cerrada tras formulario.

**Movivendor** — REST/SOAP. **Unico que menciona reversos explicitamente** en
material publico y **unico que publica un uptime (99.3%)**. Documentacion en
un portal cerrado.

**Seycel** — Condiciones "dependen del proyecto". Ni el sandbox esta
garantizado.

**Red Efectiva**, **Multiprepago / SIPREL / PrepagoTelcel**, **Pagolatino** —
sin documentacion publica; los tres del medio son la misma plataforma.

**No existen como agregadores con API:** PPD (en Mexico "PPD" es el metodo de
pago del SAT, no un proveedor), Innovacomm, Datamovil, QPagos, Recargas.com.mx,
SITEL, Compra Facil, Multipagos, MoviRed, PagaPhone.

---

## Comparativa

| | Reloadly | TAECEL | tapi | Sivetel | Movivendor |
|---|---|---|---|---|---|
| Telcel / Movistar / AT&T | si | si | parcial | si | NV |
| Unefon | **NV** | si | si | NV | NV |
| Bait | **NV** | **NV** | NV | NV | NV |
| Paquetes, no solo saldo | **NV** | **si** | probable | si | NV |
| Doc publica sin contrato | **si** | no | **si** | no | portal citado |
| Idempotencia | **no documentada** | NV | NV | NV | NV |
| Webhooks | **si, firmados** | NV | NV | NV | NV |
| Reversos | solo automaticos | NV | NV | NV | mencionados |
| Moneda del monedero | **USD** | **MXN** | NV | **MXN** | NV |
| Minimo de fondeo | **100 USD** | $50–$1,000 MXN | NV | NV | NV |
| CFDI mexicano | **no** | **si** | NV | NV | NV |
| SLA numerico | NV | NV | NV | NV | **99.3%** |

NV = no verificado.

---

## Recomendacion

**Ir a produccion con TAECEL. Conservar Reloadly detras del mismo adaptador.**

No por haber sido mencionado, sino por cuatro razones que los demas no
satisfacen:

1. **La moneda y el CFDI deciden, no la calidad de la API.** El cliente es una
   tiendita que cobra en pesos y necesita comprobante fiscal mexicano.
2. **Es el unico con catalogo de paquetes y OMV verificado.** Las tienditas no
   venden solo saldo: venden "el paquete de 200 de Telcel".
3. **La barrera de entrada es la mas baja.** Sin contrato para la cuenta, alta
   en linea, minimo de cientos de pesos.
4. **Sivetel es la segunda opcion real, no tapi.** Catalogo comparable, opera
   en MXN, atiende integraciones de punto de venta.

### Lo que esta recomendacion cuesta, dicho claro

Se elige un proveedor **cuya API no se puede evaluar antes de comprometerse.**
No sabemos si TAECEL tiene idempotencia, webhooks o reversos, porque no lo
publica. Eso es riesgo real y tiene que ser la primera tarea, no un
descubrimiento tardio. **No se migra nada hasta leer sus manuales.**

### Pasos concretos

1. Crear cuenta en taecel.com (no requiere firma) y bajar el levantamiento
   tecnologico.
2. En el formulario, elegir **Mayorista** (SAMY Cloud controla la relacion con
   la tienda), no Distribuidor. Determina el modelo de comision y el
   tratamiento fiscal.
3. Pedir los manuales **antes de depositar**.
4. **Interrogatorio tecnico bloqueante.** Las mismas preguntas a TAECEL,
   Sivetel y Movivendor, por escrito:
   - Idempotencia: ¿hay llave que garantice que un reintento tras timeout no
     duplique la recarga? *Es el riesgo numero uno de esta plataforma.*
   - Reversos: ¿hay endpoint de cancelacion? ¿ventana? ¿que pasa con una
     recarga ambigua?
   - Webhooks firmados o solo polling.
   - Consulta de estado y de saldo: endpoints y latencia.
   - Seguridad: TLS minimo, firma HMAC, IP allowlist. *Si exigen allowlist
     necesitamos IP saliente fija antes de desplegar.*
   - Porcentaje real por operador, **incluido Bait**, y escalones por volumen.
   - Aclarar la contradiccion entre $300 y $1,000 de minimo.
   - SLA y horario de soporte. Ojo: el fondeo se aplica solo en horario habil
     (L-V 8-21, S-D 9-15), lo que **limita la operacion de fin de semana** si
     se agota el saldo.
5. Implementar el adaptador **detras de la misma interfaz `TopupProvider`** que
   ya usa Reloadly. Ya existe `TaecelProvider` como esqueleto honesto que
   devuelve `PENDING_CONTRACT`.
6. **Implementar idempotencia del lado de SAMY Cloud aunque digan que la
   tienen**: folio unico por intento, estado persistido, y consulta del estado
   anterior antes de cualquier reintento.
7. Facturacion: automatizar el reporte de deposito el mismo dia. Solo se puede
   facturar lo reportado en el mes en curso.

---

## Que NO se hace

* **No se inventa una API oficial de Telcel.** No existe uno publico para
  nosotros. El camino es SAMY Cloud → agregador autorizado → Telcel, y es
  perfectamente valido mientras el producto vendido corresponda al del
  operador y el agregador pueda ejecutarlo.
* **No se hace scraping de portales de recarga** como sustituto de una API.
* **No se marca TAECEL como funcional** hasta tener credenciales, manuales y
  una prueba real en su sandbox. Hoy su adaptador devuelve
  `PENDING_CONTRACT` y toda operacion se detiene antes de tocar la orden.

---

## Fuentes

TAECEL: taecel.com (`/portal/integracion-web-services`, folleto de integrador,
levantamiento tecnologico, FAQ, politicas de facturacion).
Reloadly: support.reloadly.com (getting started, webhook, payment options,
pricing), reloadly.com/blog, github.com/Reloadly/reloadly-sdk-java.
tapi: tapi.la, tapila.dev. Sivetel: sivetel.com. Seycel: seycel.mx.
Movivendor: movivendor.com. Red Efectiva: redefectiva.com.
Multiprepago/SIPREL/PrepagoTelcel: sus respectivos portales.

Verificado el 6 de septiembre de 2026.
