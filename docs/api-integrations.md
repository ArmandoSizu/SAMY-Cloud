# Integraciones con proveedores externos

**Investigación realizada: septiembre de 2026.**

Este documento responde a una sola pregunta: **qué se puede integrar hoy, qué requiere
contrato, y qué exactamente hay que conseguir para activar cada cosa.**

Cada afirmación está marcada:

- **[V]** — verificado contra la fuente oficial (URL al final)
- **[NV]** — no verificable públicamente

> **Estado global: ninguna integración externa está activa.**
> Todos los adaptadores existen y reportan `NOT_CONFIGURED` o `PENDING_CONTRACT`.
> Ninguna operación devuelve resultados simulados.

---

## Resumen ejecutivo

| Necesidad | Proveedor recomendado | ¿Puedo empezar hoy? | Bloqueante |
|---|---|---|---|
| Cobro en efectivo | *(ninguno)* | ✅ **Ya funciona** | — |
| Cobro con tarjeta | **Conekta** | ✅ Sí, sandbox self-service | Solo crear cuenta |
| Recargas (desarrollo) | **Reloadly** | ✅ Sí, sandbox gratis | Solo crear cuenta |
| Recargas (producción MX) | **Taecel** | ❌ No | Contrato + levantamiento técnico |
| CFE / agua | **tapi**, Taecel, Arcus | ❌ No | Contrato comercial + fondeo |

**Recomendación: empezar por Conekta + Reloadly.** Es la única combinación donde se
puede escribir y probar código real sin firmar nada. Iniciar en paralelo el trámite
con Taecel y con un agregador de servicios, porque tardan semanas.

---

## 1. Cobro al cliente (pasarelas de pago)

### Comparativa

| | Sandbox self-service | Comisión tarjeta | Webhook firmado | SDK Python | Notas |
|---|---|---|---|---|---|
| **Conekta** | ✅ **Sí** [V] | 3.4% + $3.00 MXN [V] | ✅ RSA-SHA256, header `digest` [V] | Oficial, v9.0.0 [V] | **Elegido** |
| Mercado Pago | ✅ Sí, 15 cuentas prueba [V] | 3.49% inmediato / 2.95% a 30d [V] | Existe `x-signature`; algoritmo [NV] | Oficial [V] | **QR discontinuado desde jul-2023** [V] |
| Stripe MX | ✅ Sí | 3.6% + $3.00 MXN [V] | ✅ HMAC | Oficial | +2% conversión de divisa |
| Openpay (BBVA) | ✅ Sí [V] | [NV] — página es JS, ilegible | [NV] | ✅ Oficial [V] | Respaldo BBVA; pedir comisiones por escrito |
| Clip | ⚠️ **Contradictorio** [V] | 2.99% + $1 MXN | [NV] | [NV] | Dispersión en 24 h o ~4 min con Cuenta Clip [V] |

> La contradicción de Clip: su página `/reference/pruebas` describe un sandbox con hasta
> 6 credenciales, mientras que `/page/preguntas-frecuentes` dice literalmente *"No tenemos
> sandbox ni tarjetas de prueba"*. Ambas son páginas oficiales suyas. Requiere aclaración
> directa antes de considerarlo.

### Por qué Conekta

1. **Es el único con sandbox self-service documentado y comisión pública.** Se puede
   escribir código contra su API hoy.
2. **Publica el algoritmo de firma de sus webhooks** (RSA-SHA256, header `digest`, base64).
   Sin eso no se puede confiar en un webhook que marca dinero como cobrado. Mercado Pago
   firma, pero no documenta el algoritmo con la misma claridad.
3. **Checkout hospedado + tokenización**, lo que mantiene el PAN fuera de nuestros
   servidores y reduce el alcance PCI DSS a SAQ A.
4. SDK Python oficial mantenido (v9.0.0, julio 2026).

### Cómo activarlo

```
1. Crear cuenta en https://panel.conekta.com
2. Mantener el panel en "Modo de prueba"
3. Copiar llave privada, llave pública y llave pública de webhook
4. Ponerlas en .env:
     CONEKTA_PRIVATE_KEY=...
     CONEKTA_PUBLIC_KEY=...
     CONEKTA_WEBHOOK_PUBLIC_KEY=...
5. Configurar el webhook a https://TU-DOMINIO/api/v1/webhooks/conekta/
```

Adaptador: `services/payments/apps/providers/conekta.py`

### Requisitos para operar comercialmente (Conekta) [V]

**Persona física:** INE o pasaporte vigente >6 meses, comprobante de domicilio <3 meses,
constancia de situación fiscal <3 meses, CLABE.

**Persona moral:** lo anterior + acta constitutiva + poder del representante legal.

> ⚠️ El giro "fintech / servicios financieros" es de **alto riesgo** para toda pasarela.
> Espera revisión KYC reforzada y documentación adicional. Esto aplica a todas, no solo
> a Conekta.

### Sobre CoDi y QR bancario

**CoDi no está disponible como API para quien no sea una institución financiera regulada
ante Banxico** [V]. Ninguna pasarela lo ofrece self-service.

Consecuencia de diseño: el "QR de cobro" de SAMY Cloud **no es** un QR interbancario.
Es un token temporal que identifica la orden y abre el checkout del proveedor en el
teléfono del cliente. El cobro lo confirma la pasarela, no el QR. Está documentado así
en el código (`PaymentQrToken`) para que nadie lo confunda.

Banxico está homologando SPEI/CoDi/DiMo con fecha límite diciembre 2026 [V, prensa].
Conviene revisar si eso abre alguna vía.

---

## 2. Recargas de tiempo aire

### El hallazgo principal

**Telcel no ofrece API pública** [V]. Su página de distribuidores solo tiene un formulario
de contacto para la red DAT; cero mención de API, webservice o documentación técnica.
Movistar, AT&T y Unefon: no se encontró API pública directa [NV].

**Conclusión: toda recarga se vende obligatoriamente a través de un distribuidor o
agregador autorizado.** No existe integración directa con los operadores.

### Comparativa de agregadores

| | Docs públicas | Sandbox self-service | Requisitos | Cobertura MX |
|---|---|---|---|---|
| **Reloadly** | ✅ Abiertas [V] | ✅ **Sí, gratis, saldo de prueba** [V] | Solo registro | Telcel, Movistar, AT&T [V] |
| **Taecel** | ❌ Solo tras alta [V] | Sí, tras aprobación | Levantamiento técnico por correo | Telcel, Movistar, Unefon, AT&T, Iusacell, Weex, Virgin, Oui [V] |
| **Sivetel** | ❌ Solo tras aprobación [V] | Sí, tras aprobación | Formulario → documento → aprobación | [NV] |
| **DingConnect** | ✅ Referenciadas [V] | No confirmado | Registro + fondeo [V] | [NV] |

No se pudo verificar API ni documentación de: PagoFacil, Innovación en Recargas, Punto a
Punto, Qiubo, Datalogic, Sr Pago, MST/Mundo Sin Tiempo, Recargas Latinoamérica **[NV]**.
No asumir que no existen; asumir que no son self-service.

### Estrategia adoptada

**Reloadly para desarrollar y demostrar** — sandbox gratis el mismo día, OAuth2,
`topups-sandbox.reloadly.com`, catálogo consultable por API.

**Taecel para producción en México** — mejor cobertura local y precio, pero **el trámite
debe iniciarse en paralelo desde ya** porque toma semanas.

Por eso existe la abstracción `TopupProvider`: cambiar de proveedor será escribir un
adaptador, no reescribir el microservicio.

### Cómo activar Reloadly

```
1. Crear cuenta gratuita en https://www.reloadly.com
2. Developers → API Settings → crear aplicación
3. Copiar Client ID y Client Secret a .env:
     RELOADLY_CLIENT_ID=...
     RELOADLY_CLIENT_SECRET=...
4. docker compose exec topups python manage.py sync_catalog
```

Adaptador: `services/topups/apps/providers/reloadly.py`

### Qué falta para Taecel

1. Alta como distribuidor en https://taecel.com/portal/integracion-web-services
2. Completar el levantamiento tecnológico que envían por correo
3. Recibir la documentación oficial del web service (**no es pública**)
4. Recibir `TAECEL_KEY` y `TAECEL_NIP`
5. Fondear saldo prepagado
6. Implementar los métodos siguiendo **su** documentación

Adaptador: `services/topups/apps/providers/taecel.py` — existe, reporta
`PENDING_CONTRACT` y lista estos requisitos. **No llama a endpoints supuestos.**

### Modelo comercial

Ningún distribuidor publica requisitos legales. El patrón real es **contrato mercantil +
fondeo prepagado**: se compra saldo por adelantado y cada recarga lo descuenta. No se
encontró requisito de licencia CNBV para reventa de tiempo aire [NV] — confirmar con el
distribuidor antes de operar.

---

## 3. Pago de servicios (CFE, agua)

### El hallazgo principal

**CFE no tiene API pública** de consulta de adeudo ni de pago. Las búsquedas solo
devuelven portales de consumidor (CFE Contigo, cfe.gob.mx) y sitios no oficiales. CFE no
publica un portal para desarrolladores.

**Corrección importante sobre CAPDAM:** es la **Comisión de Agua Potable, Drenaje y
Alcantarillado de Manzanillo, Colima** [V] — no de Durango, como suele asumirse. (El
organismo de Durango es AMD, Aguas del Municipio de Durango.) Su sitio bloquea el acceso
automatizado y **no expone ninguna API** [NV].

Los organismos operadores municipales de agua en México casi nunca exponen API. La vía
real es siempre un agregador que ya tenga el convenio firmado con ellos.

### Agregadores con cobertura de CFE

| | Docs | Cobertura declarada | Estado |
|---|---|---|---|
| **tapi** (tapi.la) | `developers.tapila.cloud` públicas [V] | CFE, agua, gas, internet, seguros [V] | Credenciales bajo contrato |
| **Taecel** | Solo tras alta | Luz, agua, gas [V] | Contrato |
| **Sivetel** | Solo tras aprobación | Luz, agua, gas [V] | Contrato |
| **Arcus** (Mastercard) | `docs.arcusfi.com` indexadas, contenido [NV] | México [V] | Adquirida por Mastercard en 2021 [V] |

### El código de barras del recibo CFE

Lo único **verificable públicamente**: el **número de servicio de CFE tiene 12 dígitos** [V].

**No son verificables públicamente:** la simbología exacta (¿CODE128? ¿Interleaved 2 of 5?),
la longitud total del código, ni la estructura de sus campos. **No existe especificación
oficial abierta.**

**Decisión de diseño en consecuencia:** el modelo `ReferenceFormat` guarda la
especificación como **configuración por biller**, no como código. El campo
`specification_source` documenta de dónde salió cada una; mientras diga "pendiente", nadie
debe asumir que está verificada.

> **No se escribió un parser del recibo de CFE.** Inventarlo produciría un parser que
> parece funcionar, pasa pruebas con datos inventados, y falla con recibos reales
> recortando mal la referencia — es decir, **pagándole el recibo a otra persona**.
> La especificación debe pedirse al agregador como parte del due diligence.

### Qué falta para activar

1. Contacto comercial con tapi (o Taecel / Sivetel / Arcus)
2. Contrato firmado y alta como comercio (RFC, acta constitutiva)
3. Documentación oficial de su API
4. Credenciales de sandbox y producción
5. Fondeo prepagado
6. **Especificación del código de barras del recibo CFE**

Adaptador: `services/billpay/apps/providers/tapi.py`

### Lo que NO se va a hacer

**Scraping del portal de CFE.** Es frágil, viola sus términos de servicio, y —lo decisivo
para este sistema— **no da confirmación fiable de pago**. Un sistema que mueve dinero
necesita una respuesta autoritativa del proveedor, no el HTML de una página que puede
cambiar mañana.

---

## 4. Riesgos que no conviene minimizar

1. **Nada de recargas ni de pago de servicios es integrable sin contrato y fondeo
   prepagado.** Planear semanas, no días. Empezar los trámites ya.
2. **CoDi no está disponible** como API para no-bancos.
3. **El giro fintech dispara revisión KYC reforzada** en toda pasarela.
4. **El QR de Mercado Pago está discontinuado** desde julio de 2023.
5. **Verificar el organismo de agua de la ciudad objetivo.** CAPDAM es Manzanillo.

---

## Fuentes

**Pasarelas**
- [Conekta — precios](https://www.conekta.com/pricing) · [llaves de prueba](https://developers.conekta.com/docs/api-keys-pruebas) · [firma de webhooks](https://developers.conekta.com/docs/autenticaci%C3%B3n-webhooks) · [documentos de activación](https://help.conekta.com/hc/es-419/articles/360018236973--Qu%C3%A9-documentos-necesito-para-activar-mi-cuenta-) · [SDK Python](https://pypi.org/project/conekta/)
- [Mercado Pago — cuentas de prueba](https://www.mercadopago.com.mx/developers/es/docs/your-integrations/test/accounts) · [comisiones](https://www.mercadopago.com.mx/herramientas-para-vender/check-out) · [discontinuación del QR](https://www.mercadopago.com.mx/developers/es/news/2023/07/24/QR-Code-discontinuation)
- [Stripe MX — precios](https://stripe.com/mx/pricing)
- [Openpay — API](https://documents.openpay.mx/docs/api/index.html) · [registro sandbox](https://sandbox-dashboard.openpay.mx/login/register)
- [Clip — pruebas](https://developer.clip.mx/reference/pruebas) · [FAQ](https://developer.clip.mx/page/preguntas-frecuentes)
- [Guía CoDi — Banxico](https://www.banxico.org.mx/sistemas-de-pago/d/%7BA21AA19F-C855-9E64-98A4-832D9A51B2B0%7D.pdf)

**Recargas**
- [Telcel — distribuidores](https://www.telcel.com/personas/trabaja-con-telcel/distribuidor)
- [Reloadly — inicio](https://support.reloadly.com/getting-started-send-your-first-api-request) · [docs](https://docs.reloadly.com/airtime) · [operadores México](https://operators.reloadly.com/telcel-movistar-att-mexico-airtime-api/)
- [Taecel — integración](https://taecel.com/portal/integracion-web-services)
- [Sivetel](https://sivetel.com/Sitio/recargas-web-service) · [DingConnect](https://www.dingconnect.com/es-ES/Api/Integration)

**Pago de servicios**
- [tapi — servicios](https://tapi.la/servicios/) · [developers](https://developers.tapila.cloud/docs/)
- [Arcus — API México](https://docs.arcusfi.com/es/api/3.mx/tutorial/) · [Mastercard adquiere Arcus](https://investor.mastercard.com/investor-news/investor-news-details/2021/Mastercard-Expands-Support-of-Latin-America-Real-Time-Payments-with-Acquisition-of-Arcus/default.aspx)
- [Número de servicio CFE](https://app-cfe.mx/numero-de-servicio-cfe/) · [CAPDAM Manzanillo](https://www.capdam.gob.mx/Tramites)
