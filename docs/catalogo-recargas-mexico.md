# Catalogo de recargas de Mexico

Investigacion de la oferta comercial **vigente** de los cuatro operadores que
SAMY Cloud vendera primero. Fecha de verificacion: **6 de septiembre de 2026**.

Este documento no es un adorno: es la fuente de la que sale
`CommercialProduct`. Cada producto que se cargue al catalogo comercial tiene
que poder senalar una fila de aqui, con su URL oficial y su fecha.

---

## El hallazgo que cambia el diseno

Los cuatro operadores **se contradicen a si mismos en sus propias paginas
oficiales**. No es ruido de extraccion; se verifico varias veces:

* **AT&T** publica dos tablas de GB distintas para los mismos precios en dos
  paginas oficiales. `/planes/prepago/` dice que $150 da 3 GB;
  `/planes/prepago/quiero-ser-prepago/` dice 9 GB. Triple diferencia.
* **Movistar** publica en PDF que $500 da 14 GB y en HTML que da 10 GB.
* **Telcel** vende con el mismo precio y la misma marca dos cosas distintas:
  la *recarga* Amigo Sin Limite de $500 da 6,144 MB, y el *paquete* Amigo Sin
  Limite 500 da 8,192 MB.
* **Unefon** presenta su oferta Ilimitado como vigente en una pagina y como
  campana vencida en mayo de 2026 en otra.

De ahi salen tres decisiones de arquitectura que no son burocracia:

1. **`official_verified` y `verified_at` son obligatorios.** Un producto sin
   fuente y sin fecha no se puede vender, porque no hay forma de saber si lo
   que dice el ticket es lo que el operador entrega.
2. **`REVIEW_REQUIRED` tiene que existir como estado.** Cuando dos fuentes
   oficiales discrepan, el producto no se vende: se revisa. Elegir en
   silencio una de las dos cifras es inventar.
3. **El catalogo comercial NO se puede derivar automaticamente del sitio del
   operador.** Requiere lectura humana y una decision registrada. Por eso
   `CommercialProduct` es una tabla nuestra y no un espejo de nadie.

**Fuente mas fiable encontrada:** el registro tarifario del IFT
(`tarifas.ift.org.mx`) publica los PDF que los operadores presentan ante el
regulador. Son fuente primaria, traen folio, y resultaron consistentes entre
lecturas independientes, a diferencia del HTML comercial. Telcel es el caso
mas claro: sus paginas publican las tablas **dentro de imagenes JPG**, pero si
publican el folio IFT y el enlace al PDF.

> Aviso de metodo: durante la investigacion, una extraccion automatica del
> HTML de Telcel devolvio una tabla de aspecto convincente que la pagina **no
> contiene** (decia 30 dias donde el PDF oficial dice 15). Se descarto. Queda
> anotado porque es exactamente el error que este catalogo no puede permitirse.

---

## Orden comercial

Telcel es el operador principal. El orden de aparicion se resuelve con
`display_priority`, no con codigo especial por operador:

| Operador | display_priority |
|---|---|
| Telcel | 10 |
| Movistar | 20 |
| AT&T | 30 |
| Unefon | 40 |
| Otros (Bait, Virgin, Oui, FreedomPop...) | 100 |

---

## TELCEL

Fuente primaria: PDF del registro tarifario del IFT presentados por
Radiomovil Dipsa, S.A. de C.V. Los codigos SMS de activacion si estan en
texto en telcel.com.

### Familia: Amigo Sin Limite — RECARGA (esquema de cobro)

Es recarga de saldo: se recarga $X y la linea adquiere beneficios.

| Recarga | Vigencia | MB navegacion | Min/SMS | Redes |
|---|---|---|---|---|
| $10 | 1 dia | 50 MB | Ilimitados | WhatsApp |
| $20 | 2 dias | 100 MB | Ilimitados | FB+Msgr+X 200 MB |
| $30 | 3 dias | 160 MB | Ilimitados | FB+Msgr+X 300 MB |
| $50 | 7 dias | 400 MB | Ilimitados | 5 redes 750 MB |
| $80 | 12 dias | 800 MB | Ilimitados | 5 redes ilim. |
| $100 | 15 dias | 1,536 MB | Ilimitados | 5 redes ilim. |
| $150 | 25 dias | 2,560 MB | Ilimitados | 5 redes ilim. |
| $200 | 30 dias | 3,584 MB | Ilimitados | 5 redes ilim. |
| $300 | 30 dias | 5,632 MB | Ilimitados | 5 redes ilim. |
| $500 | 30 dias | 6,144 MB | Ilimitados | 5 redes ilim. |

"5 redes" = Facebook, Messenger, X, Instagram, Snapchat. WhatsApp ilimitado
siempre. Minutos y SMS ilimitados en Mexico con destino a EUA y Canada.

### Familia: Paquete Amigo Sin Limite (PASL)

| Paquete | Codigo | Precio | Vigencia | MB nav. | Folio IFT | Estado |
|---|---|---|---|---|---|---|
| PASL 10 | SL10 | $10 | 1 dia | 50 MB | 1133826 | verificado |
| PASL 20 | SL20 | $20 | — | — | 1960425 | **REVIEW_REQUIRED** |
| PASL 30 | SL30 | $30 | 3 dias | 160 MB | 1960468 | verificado |
| PASL 50 | SL50 | $50 | — | — | 1960506 | **REVIEW_REQUIRED** |
| PASL 80 | SL80 | $80 | 12 dias | 800 MB | 1960544 | verificado |
| PASL 100 | SL100 | $100 | 15 dias | 1,536 MB | 1960586 | verificado |
| PASL 150 | SL150 | $150 | 25 dias | 2,560 MB | 1960631 | verificado |
| PASL 200 | SL200 | $200 | 30 dias | 3,584 MB | 1960657 | verificado |
| PASL 270 | SLA270 | $270 | 30 dias | 2,560 MB | 2089410 | verificado |
| PASL 300 | SL300 | $300 | 30 dias | 5,632 MB | 1960676 | verificado |
| PASL 400 | SLA400 | — | — | — | *sin folio* | **REVIEW_REQUIRED** |
| PASL 500 | SL500 | $500 | 30 dias | 8,192 MB | 1960715 | verificado |

**PASL 400 no se carga al catalogo.** El codigo `SLA400` aparece en
telcel.com, pero Telcel no publica folio IFT para el, a diferencia de los
otros once. Sin tarifa verificable no se vende.

**PASL 270 da menos datos que PASL 200 y cuesta mas.** No es un error de
lectura: se corrobora con el documento de Portabilidad+, que otorga +5.5 GB al
270 y +2.5 GB al 300 dejando ambos en 8 GB. El 270 se diferencia por el
beneficio OTT, no por datos. Anotado para que nadie lo "corrija".

### Familia: Paquete Internet Amigo (PIA) — vitrina "Mas Datos"

Solo datos: **no incluyen minutos ni SMS**. "Mas Datos" es el nombre de la
vitrina en telcel.com, no una familia distinta.

| Paquete | Codigo | Precio | Vigencia | MB nav. |
|---|---|---|---|---|
| PIA 10 | Int10 | $10 | 1 dia | 70 MB |
| PIA 20 | Int20 | $20 | 2 dias | 140 MB |
| PIA 30 | Int30 | $30 | 3 dias | 220 MB |
| PIA 50 | Int50 | $50 | 7 dias | 600 MB |
| PIA 80 | Int80 | $80 | 13 dias | 700 MB |
| PIA 100 | Int100 | $100 | 15 dias | 1,844 MB |
| PIA 150 | Int150 | $150 | 25 dias | 3,072 MB |
| PIA 200 | Int200 | $200 | 30 dias | 4,096 MB |
| PIA 300 | Int300 | $300 | 30 dias | 5,120 MB |
| PIA 500 | Int500 | $500 | 30 dias | 10,240 MB |

No existe PIA 270 ni PIA 400.

### Familia: Internet por Tiempo

| Producto | Precio | Duracion | Datos | Folio IFT |
|---|---|---|---|---|
| 1 hora ilimitado | $10 | 1 hora | Ilimitados (0.5 Mbps tras 4,096 MB) | 373319 |
| 2 horas ilimitado | $15 | 2 horas | Ilimitados | 340658 |
| 4 horas ilimitado | $25 | 4 horas | Ilimitados | 1685746 |

Precios confirmados por partida doble: coinciden telcel.com y los PDF del IFT.

### No verificado en Telcel

* PASL 20, PASL 50: PDF vigente no accesible.
* PASL 400: sin folio publicado.
* Asociacion de Amazon Prime por denominacion. El PDF del beneficio OTT
  (folio 2377037) **no nombra ningun OTT**. No se promete en el ticket.
* Tope de Canada del PASL 100.
* Si la actualizacion del 10/04/2026 cambio valores de los PIA distintos
  al 100 (el 100 salio identico; los demas es inferencia).
* "Amigo Mas Juegos": existe una imagen en telcel.com, no el producto.

---

## MOVISTAR

La familia comercial de cara al cliente es **"Recarga Movistar"**. Debajo
conviven varias ofertas juridicamente distintas segun como llego la linea:
Captacion (OC), Portabilidad (OP), Rollover (ML), Preplan (UQ) y Elige+.

**Esto importa para el catalogo:** un mismo monto entrega beneficios
distintos segun la oferta en la que este la linea. SAMY Cloud no sabe en cual
esta el cliente. Por eso el ticket **no debe prometer GB ni vigencia** para
Movistar: solo el monto recargado.

### Movistar Prepago Captacion (OC) y Portabilidad (OP)

Tablas identicas en ambos PDF oficiales.

| Monto | Datos | FB/Messenger | Vigencia |
|---|---|---|---|
| $10 | 100 MB | 200 MB | 1 dia |
| $20 | 200 MB | 400 MB | 2 dias |
| $30 | 300 MB | 600 MB | 3 dias |
| $50 | 500 MB | 1.5 GB | 7 dias |
| $80 | 2 GB | 2 GB | 12 dias |
| $100 | 2 GB | Ilimitado | 15 dias |
| $150 | 3.5 GB | Ilimitado | 26 dias |
| $200 | 5 GB | Ilimitado | 30 dias |
| $300 | 8 GB | Ilimitado | 30 dias |
| $500 | 14 GB | Ilimitado | 30 dias |

Minutos y SMS ilimitados en Mexico con destino a EUA, Canada y Puerto Rico.
Roaming solo desde $100. Facebook y Messenger **no incluyen video**.

### Contradiccion abierta

La tabla publicada en `/recarga-movistar` (HTML) difiere sistematicamente de
los PDF: $50 con 750 MB en vez de 500, $500 con 10 GB en vez de 14. Parece
otra oferta (cliente existente), pero **no se pudo confirmar**. Mientras no se
resuelva, las denominaciones de Movistar se cargan como monto sin promesa de
beneficios.

Denominaciones que existen con certeza: **$10, $20, $30, $50, $60, $80, $100,
$120, $150, $200, $300, $500**.

---

## AT&T

### Conceptos que NO deben mezclarse

Texto oficial de AT&T; son cosas distintas aunque compartan precio:

* **Recarga de saldo**: dinero. Se consume a tarifa por evento.
* **Paquete de beneficios**: minutos/SMS ilimitados + GB + redes.
* **Renovacion**: recargar **48 h antes** de vencer para acumular megas.
* **Datos adicionales**: se compran con saldo, aparte del paquete.

Una recarga del mismo monto puede aplicarse como saldo o como paquete.

### AT&T Prepago (paquetes de beneficios)

**Dos paginas oficiales publican GB distintos.** Se reportan ambas.

| Precio | `/quiero-ser-prepago/` | `/planes/prepago/` | Vigencia |
|---|---|---|---|
| $10 | 100 MB | 100 MB | 1 dia |
| $20 | 400 MB | 200 MB | 1 dia |
| $30 | 600 MB | 300 MB | 3 dias |
| $50 | 1.4 GB | 750 MB | 5 dias |
| $100 | 4 GB | 2 GB | 14 dias |
| $150 | 9 GB | 3 GB | 25 dias |
| $200 | 12 GB | 4 GB | 30 dias |
| $300 | 18 GB | 6 GB | 30 dias |

Explicacion probable **no confirmada**: la promocion "Recargas Mi AT&T"
(folio IFT 2516588, vigente al 10/12/2026) da doble de megas en $10–$120 y
triple en $150–$300. Las bandas coinciden, pero $10 y $50 rompen el patron.

**Decision para el catalogo:** AT&T entra como **denominacion sin promesa de
GB**, y los productos con GB quedan en `REVIEW_REQUIRED` hasta confirmar en
tienda o en Mi AT&T. Cobrar prometiendo 9 GB cuando podrian ser 3 es
exactamente lo que este proyecto no hace.

Restricciones verificadas: activacion de chip requiere recarga de $50 o mas;
a 500 MB diarios la velocidad baja a 400 Kbps; a 3 GB mensuales a 128 Kbps.

### AT&T Pago Unico y AT&T Go

Estructuras verificadas y sin contradiccion (ver investigacion completa). No
entran en la primera version del catalogo comercial: Pago Unico es pago
anticipado con recarga automatica y AT&T Go es exclusivo de eSIM por app.

### No verificado en AT&T

* Cual de las dos tablas de GB rige.
* Todos los precios de servicios/datos adicionales (`/servicios-adicionales.html` da 403).
* AT&T Por Segundo: existe nombrada en el legal, todas sus paginas dan 403.
* Rango de renovacion: tres textos oficiales dicen $30–$300, $30–$1,000 y "$30 o mas".

---

## UNEFON

### Prepago Unefon — la oferta base

Confirmado por texto oficial: *"todas las activaciones de algun chip Unefon
incluyendo chip con beneficios de cualquier monto se activan automaticamente
en la nueva oferta Unefon"*. Es la unica familia con respaldo explicito para
lineas nuevas.

| Recarga | Vigencia | MB diarios | Min/SMS |
|---|---|---|---|
| $10 | 1 dia | 100 MB/dia | Ilimitados MX+EUA |
| $15 | 1 dia | 100 MB/dia | Ilimitados |
| $20 | 2 dias | 100 MB/dia | Ilimitados |
| $30 | 3 dias | 100 MB/dia | Ilimitados |
| $50 | 10 dias | 300 MB/dia | Ilimitados |
| $70 | 14 dias | 300 MB/dia | Ilimitados |
| $100 | 21 dias | 300 MB/dia | Ilimitados |
| $120 | 23 dias | 300 MB/dia | Ilimitados |
| $150 | 28 dias | 300 MB/dia | Ilimitados |
| $200 | 35 dias | 300 MB/dia | Ilimitados |
| $300 | 42 dias | 300 MB/dia | Ilimitados |
| $500 | 50 dias | 300 MB/dia | Ilimitados |
| $1,000 | 60 dias | 300 MB/dia | Ilimitados |

Los MB son **diarios y no acumulables**, se reinician cada 24 h. Sin
tethering. Las dos fuentes cuadran entre si (el sitio ejemplifica
"$100 = 21 dias = 6,300 megas", que es 21 x 300).

### Unefon Ilimitado — NO se carga todavia

$10/dia con cobro automatico cada 24 h, datos ilimitados con reduccion de
velocidad. **Dos paginas oficiales se contradicen** sobre si sigue vigente:
una dice "a partir del 04 de julio de 2025" (abierta) y otra la presenta como
campana del 4 al 28 de mayo de 2026 (vencida). Ademas se comercializa via
portabilidad y no se confirmo que un chip nuevo pueda activarse en ella.

Queda en `REVIEW_REQUIRED`.

### Historicos — NO deben existir en el catalogo

* **Unefon Completo** y **Plan Unefon Control**: sus URLs ahora devuelven la
  home. Producto retirado.
* **Unefon Ilimitado v2**: "disponible hasta el 6 de agosto 2022".
* Promocion "50 dias todo ilimitado": vigencia 19–22 de junio de 2026, vencida.

### No verificado en Unefon

* Ningun folio IFT pudo confirmarse contra el registro: los PDF de Unefon
  accesibles son de 2018 y 2022. El propio `registro-ift.php` de Unefon esta
  desactualizado y no contiene los folios que citan sus paginas de producto.
* Si un chip nuevo sin portabilidad puede activarse en Ilimitado.
* Escalera de dias de Ilimitado salvo $150 y $500 (el resto es aritmetica).

---

## Que se puede cargar hoy al catalogo comercial

| Operador | Familias cargables | Estado |
|---|---|---|
| Telcel | Amigo Sin Limite (recarga), PASL (9 de 12), Internet Amigo, Internet por Tiempo | verificado contra IFT |
| Movistar | denominaciones sin promesa de beneficios | contradiccion HTML/PDF abierta |
| AT&T | denominaciones sin promesa de GB | contradiccion entre dos paginas oficiales |
| Unefon | Prepago Unefon completo | verificado en sitio oficial, sin respaldo IFT reciente |

**Nada de esto es vendible todavia**, porque vendible exige ademas un
proveedor con capacidad real de ejecutarlo. Ver
`docs/proveedores-recargas-mexico.md` y la regla de `SELLABLE` en
`docs/topups-architecture.md`.

---

## Fuentes

Telcel: telcel.com y tarifas.ift.org.mx (folios citados por producto arriba).
Movistar: movistar.com.mx, incluidos los PDF de Oferta Comercial de Captacion,
Portabilidad, Rollover y Preplan.
AT&T: att.com.mx (`/planes/prepago/`, `/quiero-ser-prepago/`, `/pago-unico/`,
`/att-go/`, `/cambiate-ahora/con-prepago/`).
Unefon: unefon.com.mx (`/paquetes/prepago-unefon.php`, `/unefon_ilimitado.php`,
`/por-segundo.php`, `/legales/lineamientos-generales-recargas-saldo.php`).

Verificado el 6 de septiembre de 2026. **Las ofertas cambian.** Antes de
activar la venta de un producto hay que revisar su fecha de verificacion.
