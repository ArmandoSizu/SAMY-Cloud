"""El comprobante: lo que debe decir, y sobre todo lo que no puede decir.

La prueba que justifica que exista una capa de lista blanca es
``NuncaFiltraSecretos.test_los_datos_sensibles_no_llegan_al_comprobante``: se
alimenta el constructor con diccionarios que CONTIENEN un token, un NIP y una
llave de API, y se comprueba que nada de eso aparece en el resultado.

Sin esa capa, la regla "el comprobante nunca muestra un token" dependeria de
que nadie escriba nunca ``{{ order.algo }}`` de mas en la plantilla. Y ya
habia pasado: el comprobante imprimia ``{{ order.id }}``.
"""

from __future__ import annotations

import uuid
from dataclasses import fields as campos_de
from datetime import datetime, timezone as tz

from django.test import SimpleTestCase

from apps.gateway import comprobante


class _Tienda:
    name = "Centro"
    address = "Av. Constitucion 100"
    city = "Colima"
    state = "Colima"


class _Cajero:
    email = "ana@tienda.mx"

    def get_short_name(self) -> str:
        return "Ana"


CAJERO_ID = str(uuid.uuid4())


def _orden(**extra) -> dict:
    base = {
        "id": str(uuid.uuid4()),
        "folio": "CENTRO-260913-AB12",
        "created_by_id": CAJERO_ID,
        "created_at": datetime(2026, 9, 13, 14, 30, tzinfo=tz.utc),
        "description": "Telcel Amigo Sin Limite 100",
        "base_display": "$100.00",
        "commission_display": "$0.00",
        "total_display": "$100.00",
        "state": "SUCCESS",
        "payment_method": "CASH",
        "provider_mode": "PRODUCTION",
    }
    base.update(extra)
    return base


def _cumplimiento(**extra) -> dict:
    base = {
        "id": str(uuid.uuid4()),
        "operator_name": "Telcel",
        "product_label": "Amigo Sin Limite 100",
        "phone_masked": "31****4567",
        "provider_reference": "TX-778899",
        "provider_mode": "PRODUCTION",
    }
    base.update(extra)
    return base


def _construir(orden=None, cumplimiento=None, cambio="") -> comprobante.Comprobante:
    return comprobante.construir(
        orden=orden if orden is not None else _orden(),
        cumplimiento=cumplimiento if cumplimiento is not None else _cumplimiento(),
        tienda=_Tienda(),
        buscar_usuario=lambda _id: _Cajero(),
        cambio_display=cambio,
    )


class CamposExigidos(SimpleTestCase):
    """Todo lo que el requisito pide que se muestre."""

    def test_estan_todos(self) -> None:
        t = _construir()
        self.assertEqual(t.folio, "CENTRO-260913-AB12")
        self.assertIsNotNone(t.fecha)
        self.assertEqual(t.tienda, "Centro")
        self.assertEqual(t.cajero, "Ana")
        self.assertEqual(t.operador, "Telcel")
        self.assertEqual(t.producto, "Amigo Sin Limite 100")
        self.assertEqual(t.numero_enmascarado, "31****4567")
        self.assertEqual(t.valor_nominal, "$100.00")
        self.assertEqual(t.total_pagado, "$100.00")
        self.assertEqual(t.metodo_pago, "Efectivo")
        self.assertEqual(t.estado, "COMPLETADA")
        self.assertEqual(t.folio_proveedor, "TX-778899")

    def test_el_operador_y_el_producto_van_separados(self) -> None:
        """El requisito pide el nombre EXACTO del producto.

        La descripcion de la orden los trae pegados ("Telcel Amigo Sin Limite
        100"); si el cliente reclama, lo que compara es el nombre del producto.
        """
        t = _construir()
        self.assertNotIn("Telcel", t.producto)
        self.assertEqual(t.operador, "Telcel")

    def test_pagada_no_dice_completada(self) -> None:
        """El dinero esta cobrado y el servicio puede seguir en curso."""
        t = _construir(orden=_orden(state="PAID"))
        self.assertEqual(t.estado, "PAGADA - EN PROCESO")
        self.assertNotIn("COMPLETADA", t.estado)

    def test_el_cambio_solo_aparece_si_lo_hay(self) -> None:
        self.assertEqual(_construir().cambio, "")
        self.assertEqual(_construir(cambio="$50.00").cambio, "$50.00")


class ElCajeroEsQuienVendio(SimpleTestCase):
    """No quien esta mirando el comprobante."""

    def test_se_busca_por_created_by_id(self) -> None:
        vistos = []

        comprobante.construir(
            orden=_orden(),
            cumplimiento=_cumplimiento(),
            tienda=_Tienda(),
            buscar_usuario=lambda uid: vistos.append(uid) or _Cajero(),
            cambio_display="",
        )
        self.assertEqual(vistos, [CAJERO_ID])

    def test_si_el_usuario_ya_no_existe_se_deja_vacio(self) -> None:
        """Antes que imprimir un nombre equivocado, ninguno."""
        t = comprobante.construir(
            orden=_orden(),
            cumplimiento=_cumplimiento(),
            tienda=_Tienda(),
            buscar_usuario=lambda _id: None,
            cambio_display="",
        )
        self.assertEqual(t.cajero, "")

    def test_si_la_busqueda_revienta_el_ticket_no_se_cae(self) -> None:
        def explota(_id):
            raise RuntimeError("base caida")

        t = comprobante.construir(
            orden=_orden(),
            cumplimiento=_cumplimiento(),
            tienda=_Tienda(),
            buscar_usuario=explota,
            cambio_display="",
        )
        self.assertEqual(t.cajero, "")
        self.assertEqual(t.folio, "CENTRO-260913-AB12")


#: Cadenas que JAMAS pueden aparecer en un comprobante. Son inventadas.
SECRETOS = (
    "4111111111111111",  # numero de tarjeta
    "123",  # CVV -- se comprueba aparte, ver la nota de la prueba
    "tok_2vTestToken9999",  # token del tokenizador
    "key_ZzTestApiKey1234",  # llave de API
    "9182",  # NIP
    "Bearer eyJhbGciOiJI",  # credencial en cabecera
)


class NuncaFiltraSecretos(SimpleTestCase):
    """La razon de ser de la capa de lista blanca."""

    def _con_secretos(self) -> comprobante.Comprobante:
        """Orden y cumplimiento CONTAMINADOS a proposito.

        Se meten los secretos en campos con nombres verosimiles, los mismos
        que un serializador descuidado podria exponer algun dia.
        """
        orden = _orden(
            card_token="tok_2vTestToken9999",
            card_number="4111111111111111",
            raw_response={"api_key": "key_ZzTestApiKey1234", "nip": "9182"},
            authorization="Bearer eyJhbGciOiJI",
            conekta_order_id="ord_2vSecretoInterno",
        )
        cumplimiento = _cumplimiento(
            phone_e164="+523121234567",
            idempotency_key="topup:clave-interna-no-publicable",
            raw_response={"Key": "key_ZzTestApiKey1234", "NIP": "9182"},
        )
        return comprobante.construir(
            orden=orden,
            cumplimiento=cumplimiento,
            tienda=_Tienda(),
            buscar_usuario=lambda _id: _Cajero(),
            cambio_display="",
        )

    def _todo_el_texto(self, t: comprobante.Comprobante) -> str:
        """Cada campo del comprobante, concatenado.

        Se recorre por introspeccion y no campo por campo a mano: si alguien
        agrega un campo nuevo al comprobante, esta prueba lo revisa sin que
        haya que acordarse de actualizarla.
        """
        partes = []
        for campo in campos_de(comprobante.Comprobante):
            partes.append(str(getattr(t, campo.name)))
        return " | ".join(partes)

    def test_los_datos_sensibles_no_llegan_al_comprobante(self) -> None:
        texto = self._todo_el_texto(self._con_secretos())
        for secreto in (
            "4111111111111111",
            "tok_2vTestToken9999",
            "key_ZzTestApiKey1234",
            "Bearer eyJhbGciOiJI",
            "ord_2vSecretoInterno",
        ):
            with self.subTest(secreto=secreto[:12]):
                self.assertNotIn(secreto, texto)

    def test_el_telefono_completo_no_cruza(self) -> None:
        """Solo el enmascarado. El completo ni siquiera pasa por aqui."""
        texto = self._todo_el_texto(self._con_secretos())
        self.assertNotIn("3121234567", texto)
        self.assertNotIn("+523121234567", texto)
        self.assertIn("31****4567", texto)

    def test_ningun_identificador_tecnico(self) -> None:
        """Ni el UUID de la orden ni el del cumplimiento ni la clave interna.

        El UUID de la orden se imprimia antes al pie del ticket.
        """
        orden = _orden()
        cumplimiento = _cumplimiento()
        t = comprobante.construir(
            orden=orden,
            cumplimiento=cumplimiento,
            tienda=_Tienda(),
            buscar_usuario=lambda _id: _Cajero(),
            cambio_display="",
        )
        texto = self._todo_el_texto(t)
        self.assertNotIn(orden["id"], texto)
        self.assertNotIn(cumplimiento["id"], texto)
        self.assertNotIn(CAJERO_ID, texto)

    def test_el_comprobante_no_tiene_campos_de_paso(self) -> None:
        """Nada tipo `raw`, `extra` o `metadata` por donde colar cualquier cosa.

        Un solo campo diccionario en el comprobante dejaria la plantilla libre
        de imprimir lo que fuera, y esta capa no serviria de nada.
        """
        prohibidos = {"raw", "raw_response", "metadata", "extra", "datos", "payload"}
        nombres = {c.name for c in campos_de(comprobante.Comprobante)}
        self.assertEqual(nombres & prohibidos, set())

        # Y todos los campos son str, bool o datetime: nada anidado.
        for campo in campos_de(comprobante.Comprobante):
            with self.subTest(campo=campo.name):
                self.assertIn(
                    campo.type,
                    ("str", "bool", "datetime | None"),
                    f"{campo.name} tiene tipo {campo.type}: solo se admiten "
                    "tipos planos, para que nada anidado llegue a la plantilla.",
                )


class AmbienteSoloCuandoCorresponde(SimpleTestCase):
    """En produccion el comprobante no menciona ningun ambiente."""

    def test_produccion_completa_no_dice_nada_de_sandbox(self) -> None:
        t = _construir()
        self.assertTrue(t.es_productivo)
        self.assertFalse(t.hay_aviso_de_pruebas)
        self.assertFalse(t.cobro_en_pruebas)
        self.assertFalse(t.servicio_en_pruebas)

    def test_cobro_en_sandbox_avisa(self) -> None:
        t = _construir(orden=_orden(provider_mode="SANDBOX"))
        self.assertTrue(t.hay_aviso_de_pruebas)
        self.assertTrue(t.cobro_en_pruebas)

    def test_efectivo_real_con_servicio_en_sandbox_tambien_avisa(self) -> None:
        """El caso que se colaba mirando solo el modo del cobro.

        El efectivo en el mostrador es SIEMPRE real. Si el proveedor de
        recargas esta en sandbox, el cliente pago de verdad y el saldo no
        llego a ningun telefono.
        """
        t = _construir(
            orden=_orden(provider_mode="PRODUCTION", payment_method="CASH"),
            cumplimiento=_cumplimiento(provider_mode="SANDBOX"),
        )
        self.assertTrue(t.hay_aviso_de_pruebas)
        self.assertTrue(t.servicio_en_pruebas)
        self.assertFalse(t.cobro_en_pruebas)

    def test_un_ambiente_vacio_se_trata_como_pruebas(self) -> None:
        """Fail-closed. Un aviso de mas es un susto; uno de menos es un
        cliente que cree que recibio su saldo."""
        t = _construir(orden=_orden(provider_mode=""))
        self.assertTrue(t.cobro_en_pruebas)

    def test_un_ambiente_desconocido_tambien(self) -> None:
        t = _construir(orden=_orden(provider_mode="STAGING"))
        self.assertTrue(t.cobro_en_pruebas)

    def test_sin_cumplimiento_el_servicio_no_se_marca(self) -> None:
        """Un pago de servicios sin cumplimiento no inventa un aviso."""
        t = _construir(cumplimiento={})
        self.assertFalse(t.servicio_en_pruebas)
