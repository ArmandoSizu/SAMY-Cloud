"""La configuracion de base que solo se activa en produccion.

Es justo el tipo de codigo que se rompe sin que nadie lo note: en desarrollo
nunca se ejecuta, y la primera vez que corre de verdad es en el despliegue, a
las once de la noche, con el profesor esperando.
"""

from __future__ import annotations

import environ
import pytest

from samy_common.db import configurar_base_de_datos

URL_DEV = "postgres://samy_topups:topups_dev_password@localhost:5432/samy_topups"


def _configurar(monkeypatch, **entorno):
    for clave in (
        "DB_SOCKET",
        "DB_HOST",
        "DB_PORT",
        "DB_SSLMODE",
        "DB_USER",
        "DB_PASS",
        "TOPUPS_DATABASE_URL",
        "TOPUPS_DB_NAME",
    ):
        monkeypatch.delenv(clave, raising=False)
    for clave, valor in entorno.items():
        monkeypatch.setenv(clave, valor)
    return configurar_base_de_datos(
        environ.Env(),
        variable_url="TOPUPS_DATABASE_URL",
        url_por_omision=URL_DEV,
        nombre_por_omision="samy_topups",
    )


class TestSinSocketNadaCambia:
    """Lo primero que hay que garantizar: no romper lo que ya funcionaba."""

    def test_sin_db_socket_se_usa_la_url_de_siempre(self, monkeypatch) -> None:
        config = _configurar(monkeypatch)
        assert config["NAME"] == "samy_topups"
        assert config["HOST"] == "localhost"
        assert config["PORT"] == 5432

    def test_la_url_del_entorno_manda_sobre_la_de_omision(self, monkeypatch) -> None:
        config = _configurar(
            monkeypatch,
            TOPUPS_DATABASE_URL="postgres://otro:clave@otrohost:6543/otra_base",
        )
        assert config["NAME"] == "otra_base"
        assert config["HOST"] == "otrohost"

    def test_un_db_socket_vacio_no_cuenta_como_socket(self, monkeypatch) -> None:
        """Una variable definida pero vacia es el caso que rompe estas cosas.

        Cloud Run deja variables vacias con facilidad -un substitutions mal
        puesto, un flag sin valor- y tratar "" como "hay socket" produce una
        conexion a la ruta vacia, cuyo error no menciona ni sockets ni
        Cloud SQL.
        """
        config = _configurar(monkeypatch, DB_SOCKET="   ")
        assert config["HOST"] == "localhost"


class TestConSocketDeCloudSql:
    SOCKET = "/cloudsql/proyecto:us-central1:samy-sql"

    def test_se_arma_desde_las_piezas(self, monkeypatch) -> None:
        config = _configurar(
            monkeypatch,
            DB_SOCKET=self.SOCKET,
            DB_USER="samy",
            DB_PASS="una-contrasena",
        )
        assert config["ENGINE"] == "django.db.backends.postgresql"
        assert config["NAME"] == "samy_topups"
        assert config["USER"] == "samy"
        assert config["PASSWORD"] == "una-contrasena"

    def test_el_socket_va_en_host_y_el_puerto_queda_vacio(self, monkeypatch) -> None:
        """Es el error clasico y el mas caro de diagnosticar.

        psycopg decide que es un socket de dominio Unix porque HOST empieza
        con '/'. Si ademas se pasa un puerto, o si la ruta se pone en OPTIONS
        y en HOST queda 127.0.0.1, el fallo es "connection refused" contra
        localhost: un mensaje que manda a revisar la red y no la configuracion.
        """
        config = _configurar(monkeypatch, DB_SOCKET=self.SOCKET, DB_PASS="x")
        assert config["HOST"] == self.SOCKET
        assert config["HOST"].startswith("/cloudsql/")
        assert config["PORT"] == ""

    def test_cada_servicio_conserva_SU_base(self, monkeypatch) -> None:
        """Las cuatro bases viven en una instancia; mezclarlas seria el desastre.

        El nombre no sale del socket ni de la URL: sale del valor por omision
        de cada servicio, que es distinto para cada uno.
        """
        monkeypatch.setenv("DB_SOCKET", self.SOCKET)
        nombres = {
            variable: configurar_base_de_datos(
                environ.Env(),
                variable_url=variable,
                url_por_omision="postgres://x:y@localhost:5432/z",
                nombre_por_omision=nombre,
            )["NAME"]
            for variable, nombre in (
                ("CORE_DATABASE_URL", "samy_core"),
                ("PAYMENTS_DATABASE_URL", "samy_payments"),
                ("TOPUPS_DATABASE_URL", "samy_topups"),
                ("BILLPAY_DATABASE_URL", "samy_billpay"),
            )
        }
        assert nombres == {
            "CORE_DATABASE_URL": "samy_core",
            "PAYMENTS_DATABASE_URL": "samy_payments",
            "TOPUPS_DATABASE_URL": "samy_topups",
            "BILLPAY_DATABASE_URL": "samy_billpay",
        }
        # Y son cuatro bases DISTINTAS. Si algun dia alguien "simplifica" el
        # helper y las cuatro acaban apuntando al mismo NAME, esto lo caza.
        assert len(set(nombres.values())) == 4

    def test_el_nombre_se_puede_fijar_por_servicio(self, monkeypatch) -> None:
        config = _configurar(
            monkeypatch,
            DB_SOCKET=self.SOCKET,
            TOPUPS_DB_NAME="samy_topups_demo",
        )
        assert config["NAME"] == "samy_topups_demo"

    def test_sin_contrasena_no_se_inventa_ninguna(self, monkeypatch) -> None:
        """Cadena vacia, no None ni un valor de relleno.

        Un default plausible aqui haria que un secreto mal montado se viera
        como un fallo de autenticacion de PostgreSQL y no como lo que es.
        """
        config = _configurar(monkeypatch, DB_SOCKET=self.SOCKET)
        assert config["PASSWORD"] == ""


class TestConHostDeRed:
    """Azure Database for PostgreSQL Flexible Server, o cualquier TCP."""

    HOST = "samy-postgres.postgres.database.azure.com"

    def test_se_conecta_por_tcp_con_puerto(self, monkeypatch) -> None:
        config = _configurar(monkeypatch, DB_HOST=self.HOST, DB_PASS="x")
        assert config["HOST"] == self.HOST
        assert config["PORT"] == "5432"
        assert config["NAME"] == "samy_topups"

    def test_el_tls_es_obligatorio_por_omision(self, monkeypatch) -> None:
        """Sin esto la contrasena y cada recarga viajarian en texto claro.

        Y el fallo no seria visible: la conexion funcionaria igual. Azure
        ademas rechaza las conexiones sin cifrar, asi que el valor por omision
        tiene que ser el seguro.
        """
        config = _configurar(monkeypatch, DB_HOST=self.HOST, DB_PASS="x")
        assert config["OPTIONS"]["sslmode"] == "require"

    def test_bajar_el_tls_exige_escribirlo_a_mano(self, monkeypatch) -> None:
        config = _configurar(
            monkeypatch, DB_HOST=self.HOST, DB_PASS="x", DB_SSLMODE="disable"
        )
        assert config["OPTIONS"]["sslmode"] == "disable"

    def test_el_socket_gana_sobre_el_host(self, monkeypatch) -> None:
        """Si estan los dos, manda el socket y NO se pide sslmode.

        Un socket de dominio Unix no sale de la maquina, asi que 'require'
        sobre el no aporta nada y en algunos casos rompe la conexion.
        """
        config = _configurar(
            monkeypatch,
            DB_SOCKET="/cloudsql/p:r:i",
            DB_HOST=self.HOST,
            DB_PASS="x",
        )
        assert config["HOST"] == "/cloudsql/p:r:i"
        assert config["PORT"] == ""
        assert "sslmode" not in config["OPTIONS"]


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__])
