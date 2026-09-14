"""Configuracion de base de datos para los cuatro servicios.

EL PROBLEMA QUE RESUELVE
------------------------

Los cuatro servicios leen su base de una sola variable con TODO dentro::

    CORE_DATABASE_URL=postgres://usuario:contrasena@host:5432/samy_core

Funciona perfectamente en desarrollo. En Cloud Run no, y no por un detalle
tecnico sino por donde acaba escrita la contrasena: una variable de entorno de
Cloud Run se guarda en la configuracion del servicio, y esa configuracion la
ve cualquiera que pueda leer el servicio -la consola web, ``gcloud run
services describe``, el historial de revisiones-. La contrasena de PostgreSQL
quedaria ahi, en texto plano, para siempre.

Secret Manager resuelve exactamente eso, pero solo puede inyectar un VALOR
suelto, no fabricar una URL. De ahi este modulo: permite armar la conexion
desde piezas, y que la unica pieza secreta -la contrasena- venga de un secreto.

QUE CAMBIA Y QUE NO
-------------------

Nada, mientras no exista ``DB_SOCKET``. Sin esa variable el comportamiento es
el de siempre: se lee la URL completa. Desarrollo, las pruebas y docker
compose siguen igual, byte por byte.

Con ``DB_SOCKET`` presente -que es la señal de "estoy en Cloud Run detras del
proxy de Cloud SQL"- la conexion se arma de las piezas y la URL se ignora.

UNA ACLARACION HONESTA
----------------------

Un secreto montado con ``--set-secrets`` **tambien llega como variable de
entorno** dentro del contenedor. Lo que se gana no es esconderlo del proceso,
que necesita leerlo: es que el valor deja de estar guardado en la
configuracion del servicio. Lo que se guarda ahi es una referencia al secreto,
con su control de acceso y sus versiones. Se puede rotar sin volver a
desplegar, y se puede auditar quien lo leyo. Eso es lo que mejora, y no
conviene contarlo como mas de lo que es.
"""

from __future__ import annotations

from typing import Any

__all__ = ["configurar_base_de_datos"]


def configurar_base_de_datos(
    env: Any,
    *,
    variable_url: str,
    url_por_omision: str,
    nombre_por_omision: str,
) -> dict[str, Any]:
    """Devuelve el diccionario ``DATABASES["default"]`` del servicio.

    :param env: el ``environ.Env`` del modulo de ajustes que llama.
    :param variable_url: p. ej. ``"CORE_DATABASE_URL"``. El camino de siempre.
    :param url_por_omision: valor de desarrollo, tal como estaba.
    :param nombre_por_omision: p. ej. ``"samy_core"``. Solo se usa en el
        camino del socket, y existe para que cada servicio siga apuntando a SU
        base aunque las cuatro vivan en la misma instancia de Cloud SQL.
    """
    socket = env.str("DB_SOCKET", default="").strip()
    host = env.str("DB_HOST", default="").strip()

    if not socket and not host:
        # Camino de siempre. Ni una diferencia respecto a antes.
        return env.db_url(variable_url, default=url_por_omision)

    # Camino de Cloud SQL. El nombre de la base se puede fijar por servicio
    # (CORE_DB_NAME, PAYMENTS_DB_NAME...) pero casi nunca hara falta: el valor
    # por omision ya es el correcto.
    prefijo = variable_url.split("_DATABASE_URL")[0]

    config: dict[str, Any] = {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": env.str(f"{prefijo}_DB_NAME", default=nombre_por_omision),
        "USER": env.str("DB_USER", default="samy"),
        "PASSWORD": env.str("DB_PASS", default=""),
        "OPTIONS": {},
    }

    if socket:
        # Socket de dominio Unix (el proxy de Cloud SQL). psycopg espera la
        # RUTA en HOST y el puerto VACIO. Poner aqui un host de red y un puerto
        # es el error clasico: da "connection refused" contra 127.0.0.1 y manda
        # a buscar un problema de red que no existe.
        config["HOST"] = socket
        config["PORT"] = ""
        return config

    # Host de red (Azure Database for PostgreSQL Flexible Server, o cualquier
    # PostgreSQL administrado al que se llega por TCP).
    config["HOST"] = host
    config["PORT"] = env.str("DB_PORT", default="5432")

    # TLS obligatorio por omision, y esto NO es una preferencia.
    #
    # Azure rechaza las conexiones sin cifrar, asi que sin esto el fallo seria
    # inmediato. Pero el valor por omision es 'require' incluso fuera de Azure
    # a proposito: si algun dia se apunta a un PostgreSQL que SI acepta texto
    # claro, la contrasena y cada recarga viajarian sin cifrar por una red que
    # no controlamos, y no habria ningun error que lo delatara. Se puede bajar
    # con DB_SSLMODE, pero hay que escribirlo a mano.
    config["OPTIONS"]["sslmode"] = env.str("DB_SSLMODE", default="require")
    return config
