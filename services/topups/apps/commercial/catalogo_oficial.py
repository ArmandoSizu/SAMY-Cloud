"""La oferta oficial verificada, como datos.

Cada fila de aqui se leyo en una fuente oficial el 6 de septiembre de 2026 y
lleva su URL. Lo que no se pudo verificar NO esta: no hay una sola
denominacion, vigencia o cifra de datos puesta "porque suele ser asi".

La investigacion completa, con las contradicciones encontradas y lo que quedo
sin verificar, esta en ``docs/catalogo-recargas-mexico.md``.

QUE SE CARGA Y QUE NO
----------------------

* **Telcel** entra completo salvo tres paquetes. PASL 20 y PASL 50 tienen
  folio IFT pero su PDF vigente no se pudo abrir; PASL 400 aparece en
  telcel.com con codigo SLA400 pero **Telcel no publica folio para el**, a
  diferencia de los otros once. Sin tarifa verificable no se vende: entran en
  REVIEW_REQUIRED.

* **Movistar** entra como denominacion **sin promesa de beneficios**. Sus
  PDF oficiales y su HTML publican cifras distintas para el mismo monto (el
  de $500 da 14 GB en uno y 10 GB en el otro), y ademas los beneficios
  dependen de la oferta en la que este la linea -Captacion, Portabilidad,
  Rollover, Preplan-, que nosotros no sabemos. Prometer GB aqui seria
  adivinar.

* **AT&T** igual, y por una razon aun mas clara: dos paginas oficiales
  publican tablas de GB distintas para los mismos precios, con diferencias de
  hasta el triple. Se venden los montos; los GB quedan en revision.

* **Unefon** entra solo con Prepago Unefon, que es la unica familia con
  respaldo explicito para lineas nuevas ("todas las activaciones... se activan
  automaticamente en la nueva oferta Unefon"). Unefon Ilimitado queda en
  revision porque dos paginas oficiales discrepan sobre si sigue vigente.

Ninguno de estos productos es vendible todavia, y no por esto: es porque
ningun proveedor configurado tiene mapping para ellos. Ver la regla en
``services.disponibilidad()``.
"""

from __future__ import annotations

from typing import Any, Final

FECHA_VERIFICACION: Final[str] = "2026-09-06"

IFT: Final[str] = "https://tarifas.ift.org.mx/ift_visor/"

OPERADORES: Final[list[dict[str, Any]]] = [
    {"code": "TELCEL", "name": "Telcel", "display_priority": 10, "is_primary": True},
    {"code": "MOVISTAR", "name": "Movistar", "display_priority": 20},
    {"code": "ATT", "name": "AT&T", "display_priority": 30},
    {"code": "UNEFON", "name": "Unefon", "display_priority": 40},
]

FAMILIAS: Final[list[dict[str, Any]]] = [
    # --- Telcel -----------------------------------------------------------
    {
        "operator": "TELCEL",
        "code": "SALDO",
        "name": "Saldo / Recarga Amigo",
        "description": "Recarga de saldo con minutos, SMS y datos incluidos.",
        "display_priority": 10,
    },
    {
        "operator": "TELCEL",
        "code": "AMIGO_SIN_LIMITE",
        "name": "Amigo Sin Limite",
        "description": "Paquete con redes sociales y llamadas ilimitadas.",
        "display_priority": 20,
    },
    {
        "operator": "TELCEL",
        "code": "INTERNET_AMIGO",
        "name": "Internet / Datos",
        "description": "Solo datos. No incluye minutos ni SMS.",
        "display_priority": 30,
    },
    {
        "operator": "TELCEL",
        "code": "INTERNET_POR_TIEMPO",
        "name": "Otros paquetes",
        "description": "Internet ilimitado por horas.",
        "display_priority": 40,
    },
    # --- Los demas --------------------------------------------------------
    {
        "operator": "MOVISTAR",
        "code": "RECARGA",
        "name": "Recarga Movistar",
        "description": "Recarga de saldo.",
        "display_priority": 10,
    },
    {
        "operator": "ATT",
        "code": "RECARGA",
        "name": "Recarga AT&T",
        "description": "Recarga de saldo.",
        "display_priority": 10,
    },
    {
        "operator": "UNEFON",
        "code": "PREPAGO",
        "name": "Prepago Unefon",
        "description": "Recarga con datos diarios, minutos y SMS ilimitados.",
        "display_priority": 10,
    },
]

#: Textos que se repiten. Se guardan literales, tal como los publica el
#: operador: recortar "con destino a Estados Unidos, Canada y Puerto Rico" a
#: "ilimitados" cambia lo que se promete.
_TELCEL_LLAMADAS = "Ilimitadas en Mexico con destino a EUA y Canada"
_TELCEL_SMS = "Ilimitados en Mexico con destino a EUA y Canada"
_TELCEL_URL_ASL = (
    "https://www.telcel.com/personas/amigo/tarifas-y-opciones/amigo-sin-limite"
)
_TELCEL_URL_PASL = (
    "https://www.telcel.com/personas/amigo/paquetes/paquetes-amigo-sin-limite.html"
)
_TELCEL_URL_PIA = "https://www.telcel.com/personas/amigo/paquetes/mb-para-tu-amigo"
_TELCEL_URL_IPT = "https://www.telcel.com/personas/amigo/paquetes/internet-por-tiempo"


#: Amigo Sin Limite, esquema de RECARGA. Fuente: PDF del registro tarifario
#: del IFT presentados por Radiomovil Dipsa. Redes = Facebook, Messenger, X,
#: Instagram y Snapchat; WhatsApp ilimitado siempre.
_TELCEL_SALDO: Final[list[tuple[int, int, int, str]]] = [
    # (pesos, dias de vigencia, MB de navegacion, redes incluidas)
    (10, 1, 50, "WhatsApp ilimitado"),
    (20, 2, 100, "WhatsApp ilimitado. Facebook, Messenger y X: 200 MB"),
    (30, 3, 160, "WhatsApp ilimitado. Facebook, Messenger y X: 300 MB"),
    (50, 7, 400, "WhatsApp ilimitado. 5 redes sociales: 750 MB"),
    (80, 12, 800, "WhatsApp y 5 redes sociales ilimitadas"),
    (100, 15, 1536, "WhatsApp y 5 redes sociales ilimitadas"),
    (150, 25, 2560, "WhatsApp y 5 redes sociales ilimitadas"),
    (200, 30, 3584, "WhatsApp y 5 redes sociales ilimitadas"),
    (300, 30, 5632, "WhatsApp y 5 redes sociales ilimitadas"),
    (500, 30, 6144, "WhatsApp y 5 redes sociales ilimitadas"),
]

#: Paquete Amigo Sin Limite. El codigo es el que se manda por SMS y esta
#: publicado en texto en telcel.com; el folio es el del registro tarifario.
_TELCEL_PASL: Final[list[tuple[int, str, int | None, int | None, str, str]]] = [
    # (pesos, codigo SMS, dias, MB, folio IFT, estado)
    (10, "SL10", 1, 50, "1133826", "AVAILABLE"),
    (20, "SL20", None, None, "1960425", "REVIEW_REQUIRED"),
    (30, "SL30", 3, 160, "1960468", "AVAILABLE"),
    (50, "SL50", None, None, "1960506", "REVIEW_REQUIRED"),
    (80, "SL80", 12, 800, "1960544", "AVAILABLE"),
    (100, "SL100", 15, 1536, "1960586", "AVAILABLE"),
    (150, "SL150", 25, 2560, "1960631", "AVAILABLE"),
    (200, "SL200", 30, 3584, "1960657", "AVAILABLE"),
    (270, "SLA270", 30, 2560, "2089410", "AVAILABLE"),
    (300, "SL300", 30, 5632, "1960676", "AVAILABLE"),
    (400, "SLA400", None, None, "", "REVIEW_REQUIRED"),
    (500, "SL500", 30, 8192, "1960715", "AVAILABLE"),
]

#: Paquete Internet Amigo. SOLO DATOS: no lleva minutos ni SMS, y eso tiene
#: que quedar dicho en el ticket para que nadie lo confunda con Amigo Sin
#: Limite al mismo precio.
_TELCEL_PIA: Final[list[tuple[int, str, int, int, str]]] = [
    # (pesos, codigo, dias, MB, folio IFT)
    (10, "Int10", 1, 70, "2503420"),
    (20, "Int20", 2, 140, "2503423"),
    (30, "Int30", 3, 220, "2503431"),
    (50, "Int50", 7, 600, "2503434"),
    (80, "Int80", 13, 700, "2503440"),
    (100, "Int100", 15, 1844, "2503445"),
    (150, "Int150", 25, 3072, "2503450"),
    (200, "Int200", 30, 4096, "2503454"),
    (300, "Int300", 30, 5120, "2503458"),
    (500, "Int500", 30, 10240, "2503464"),
]

#: Internet por Tiempo. Confirmado por partida doble: los tres precios
#: coinciden entre telcel.com y los PDF del IFT.
_TELCEL_IPT: Final[list[tuple[int, str, int, str]]] = [
    # (pesos, nombre, horas, folio IFT)
    (10, "Internet por Tiempo 1 hora", 1, "373319"),
    (15, "Internet por Tiempo 2 horas", 2, "340658"),
    (25, "Internet por Tiempo 4 horas", 4, "1685746"),
]

#: Movistar: SOLO denominaciones. Sus fuentes oficiales publican beneficios
#: distintos para el mismo monto y ademas dependen de la oferta en la que este
#: la linea, que no conocemos. Se vende el monto, no una promesa.
_MOVISTAR: Final[list[int]] = [10, 20, 30, 50, 60, 80, 100, 120, 150, 200, 300, 500]

#: AT&T: SOLO denominaciones, por la contradiccion entre /planes/prepago/ y
#: /quiero-ser-prepago/ (hasta el triple de diferencia en los GB).
_ATT: Final[list[int]] = [10, 20, 30, 50, 100, 150, 200, 300]

#: Unefon Prepago. Los MB son DIARIOS y no acumulables: se reinician cada 24 h.
#: Por eso no se cargan en data_mb, que representa datos del periodo: decir
#: "6300 MB" cuando en realidad son 300 al dia durante 21 dias seria enganoso.
_UNEFON: Final[list[tuple[int, int, int]]] = [
    # (pesos, dias, MB diarios)
    (10, 1, 100),
    (15, 1, 100),
    (20, 2, 100),
    (30, 3, 100),
    (50, 10, 300),
    (70, 14, 300),
    (100, 21, 300),
    (120, 23, 300),
    (150, 28, 300),
    (200, 35, 300),
    (300, 42, 300),
    (500, 50, 300),
    (1000, 60, 300),
]


def productos() -> list[dict[str, Any]]:
    """Todas las filas del catalogo comercial, listas para cargar.

    Se construyen aqui y no en el comando para que las pruebas puedan
    comprobar los datos sin tocar la base.
    """
    filas: list[dict[str, Any]] = []

    for pesos, dias, mb, redes in _TELCEL_SALDO:
        filas.append(
            {
                "operator": "TELCEL",
                "family": "SALDO",
                "commercial_name": f"Recarga Amigo {pesos}",
                "price_cents": pesos * 100,
                "official_source": _TELCEL_URL_ASL,
                "official_tariff_reference": "",
                "status": "AVAILABLE",
                "version": {
                    "validity_days": dias,
                    "data_mb": mb,
                    "calls": _TELCEL_LLAMADAS,
                    "sms": _TELCEL_SMS,
                    "benefits": redes,
                    "restrictions": (
                        "Las redes sociales solo operan en Mexico. Los MB no son "
                        "acumulables y se pierden al vencer."
                    ),
                },
            }
        )

    for pesos, codigo, dias, mb, folio, estado in _TELCEL_PASL:
        pendiente = estado == "REVIEW_REQUIRED"
        filas.append(
            {
                "operator": "TELCEL",
                "family": "AMIGO_SIN_LIMITE",
                "commercial_name": f"Amigo Sin Limite {pesos}",
                "price_cents": pesos * 100,
                "official_source": _TELCEL_URL_PASL,
                "official_tariff_reference": folio,
                "status": estado,
                "status_reason": (
                    "Telcel no publica folio tarifario para este paquete."
                    if pendiente and not folio
                    else "El PDF vigente del folio no se pudo verificar."
                    if pendiente
                    else ""
                ),
                # Sin cifras verificadas no se inventa una version.
                "version": None
                if pendiente
                else {
                    "validity_days": dias,
                    "data_mb": mb,
                    "calls": _TELCEL_LLAMADAS,
                    "sms": _TELCEL_SMS,
                    # El codigo de activacion (SL100, Int50...) es de Telcel y
                    # es publico, pero al cajero no le sirve: no lo teclea
                    # nadie en este flujo. Queda en el catalogo tecnico, no en
                    # la tarjeta que el cliente ve por encima del hombro.
                    "benefits": "WhatsApp y redes sociales ilimitadas",
                    "restrictions": (
                        "Sujeto a la politica de uso justo de Telcel. No disponible "
                        "para usuarios con planes en modalidad controlado."
                    ),
                },
            }
        )

    for pesos, codigo, dias, mb, folio in _TELCEL_PIA:
        filas.append(
            {
                "operator": "TELCEL",
                "family": "INTERNET_AMIGO",
                "commercial_name": f"Internet Amigo {pesos}",
                "price_cents": pesos * 100,
                "official_source": _TELCEL_URL_PIA,
                "official_tariff_reference": folio,
                "status": "AVAILABLE",
                "version": {
                    "validity_days": dias,
                    "data_mb": mb,
                    # Explicito, no vacio: que no lleve minutos es justo lo que
                    # hay que decirle al cliente antes de cobrarle.
                    "calls": "No incluye minutos",
                    "sms": "No incluye SMS",
                    "benefits": "Solo datos de navegacion",
                    "restrictions": "Paquete de datos. No incluye minutos ni mensajes.",
                },
            }
        )

    for pesos, nombre, horas, folio in _TELCEL_IPT:
        filas.append(
            {
                "operator": "TELCEL",
                "family": "INTERNET_POR_TIEMPO",
                "commercial_name": nombre,
                "price_cents": pesos * 100,
                "official_source": _TELCEL_URL_IPT,
                "official_tariff_reference": folio,
                "status": "AVAILABLE",
                "version": {
                    "validity_days": None,
                    "data_mb": None,
                    "calls": "No incluye minutos",
                    "sms": "No incluye SMS",
                    "benefits": f"Navegacion ilimitada durante {horas} h",
                    "restrictions": (
                        "Solo en territorio nacional. Sujeto a politica de uso justo."
                    ),
                },
            }
        )

    for pesos in _MOVISTAR:
        filas.append(
            {
                "operator": "MOVISTAR",
                "family": "RECARGA",
                "commercial_name": f"Recarga Movistar {pesos}",
                "price_cents": pesos * 100,
                "official_source": "https://www.movistar.com.mx/recarga-movistar",
                "official_tariff_reference": "",
                "status": "AVAILABLE",
                "version": {
                    "validity_days": None,
                    "data_mb": None,
                    "calls": "",
                    "sms": "",
                    "benefits": "",
                    "restrictions": (
                        "Los beneficios dependen de la oferta comercial en la que "
                        "este la linea. Consultar con Movistar."
                    ),
                },
            }
        )

    for pesos in _ATT:
        filas.append(
            {
                "operator": "ATT",
                "family": "RECARGA",
                "commercial_name": f"Recarga AT&T {pesos}",
                "price_cents": pesos * 100,
                "official_source": "https://www.att.com.mx/planes/prepago/",
                "official_tariff_reference": "",
                "status": "AVAILABLE",
                "version": {
                    "validity_days": None,
                    "data_mb": None,
                    "calls": "Ilimitados Mexico y EUA",
                    "sms": "Ilimitados",
                    "benefits": "",
                    "restrictions": (
                        "AT&T publica cifras de datos distintas en dos paginas "
                        "oficiales. Los GB no se prometen en el comprobante."
                    ),
                },
            }
        )

    for pesos, dias, mb_diarios in _UNEFON:
        filas.append(
            {
                "operator": "UNEFON",
                "family": "PREPAGO",
                "commercial_name": f"Recarga Unefon {pesos}",
                "price_cents": pesos * 100,
                "official_source": "https://unefon.com.mx/paquetes/prepago-unefon.php",
                "official_tariff_reference": "",
                "status": "AVAILABLE",
                "version": {
                    "validity_days": dias,
                    # Los MB son diarios, no del periodo: no caben en data_mb
                    # sin mentir sobre lo que el cliente recibe.
                    "data_mb": None,
                    "calls": "Ilimitados Mexico y EUA",
                    "sms": "Ilimitados Mexico y EUA",
                    "benefits": f"{mb_diarios} MB diarios durante {dias} dias",
                    "restrictions": (
                        "Los datos son diarios y no acumulables: se reinician cada "
                        "24 horas. No permite compartir conexion."
                    ),
                },
            }
        )

    return filas
