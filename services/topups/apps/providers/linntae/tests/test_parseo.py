"""El dinero de Linntae llega como texto, y su especificacion se contradice.

Estas pruebas fijan las dos cosas que el parser tiene que hacer bien y que se
hacen mal por omision:

1. ``"$3,314.00"`` no se convierte con ``float()``. Nunca.
2. El catalogo y las comisiones llegan en DOS formas distintas, porque el
   ejemplo y el esquema de su propia especificacion no coinciden. Las dos se
   aceptan, y lo que no se reconoce levanta en vez de devolver una lista
   vacia.
"""

from __future__ import annotations

from decimal import Decimal

from django.test import SimpleTestCase

from apps.providers.linntae import codigos, parseo
from apps.providers.linntae.codigos import Consecuencia, Endpoint


class DineroComoTexto(SimpleTestCase):
    """``"$3,314.00"`` -> 331400 centavos, por Decimal y solo por Decimal."""

    def test_el_ejemplo_literal_de_la_especificacion(self) -> None:
        plataforma = parseo.a_money("$3,314.00")
        comision = parseo.a_money("$96.95")
        assert plataforma is not None and comision is not None
        self.assertEqual(plataforma.cents, 331_400)
        self.assertEqual(comision.cents, 9_695)

    def test_formas_que_una_plantilla_puede_producir(self) -> None:
        casos = {
            "3314": 331_400,
            "3,314": 331_400,
            "3314.5": 331_450,
            "$0.00": 0,
            " $1,000.00 ": 100_000,
            "$1 000.00": 100_000,
            "1000.00 MXN": 100_000,
            10: 1_000,
            Decimal("10.50"): 1_050,
        }
        for crudo, esperado in casos.items():
            with self.subTest(crudo=crudo):
                leido = parseo.a_money(crudo)
                assert leido is not None
                self.assertEqual(leido.cents, esperado)

    def test_negativos_con_signo_y_entre_parentesis(self) -> None:
        for crudo in ("-$100.00", "($100.00)", "-100"):
            with self.subTest(crudo=crudo):
                leido = parseo.a_money(crudo)
                assert leido is not None
                self.assertEqual(leido.cents, -10_000)

    def test_lo_ilegible_es_none_y_nunca_cero(self) -> None:
        """La distincion que importa: None es "no lo dijo", cero es "no tengo".

        Si esta prueba se cae devolviendo cero, la guarda de saldo leeria
        "el proveedor no tiene fondos" donde en realidad no se entendio su
        respuesta. Bloquearia ventas de una cuenta con dinero.
        """
        for crudo in ("", "   ", "N/D", "no disponible", None, "$", "1.2.3", True):
            with self.subTest(crudo=crudo):
                self.assertIsNone(parseo.a_money(crudo))

    def test_un_float_no_se_multiplica_por_cien(self) -> None:
        """Si Linntae mandara el saldo como numero JSON, llega como float.

        ``Money`` rechaza floats a proposito. El parser lo convierte por la
        via del texto decimal mas corto que lo representa, que es lo mas fiel
        posible con un dato que ya llego dañado. Lo que NO hace es
        ``int(f * 100)``, que es de donde salen los centavos fantasma.
        """
        leido = parseo.a_money(3314.0)
        assert leido is not None
        self.assertEqual(leido.cents, 331_400)

        # El caso clasico: 1.1 * 100 == 110.00000000000001 en binario.
        leido = parseo.a_money(1.1)
        assert leido is not None
        self.assertEqual(leido.cents, 110)


class Porcentajes(SimpleTestCase):
    def test_el_ejemplo_de_la_especificacion(self) -> None:
        tasa = parseo.a_tasa("5.5%")
        assert tasa is not None
        self.assertEqual(tasa.bps, 550)
        self.assertEqual(tasa.tasa_pct, Decimal("5.5"))
        self.assertTrue(tasa.exacta_en_bps)

    def test_un_numero_pelado_es_porcentaje(self) -> None:
        """Su propio esquema documenta ``comision`` con ``default: 5`` = 5%."""
        tasa = parseo.a_tasa(6)
        assert tasa is not None
        self.assertEqual(tasa.bps, 600)

    def test_no_se_interpreta_0_055_como_5_5_por_ciento(self) -> None:
        """Adivinar entre dos lecturas cambia el margen cien veces.

        ``0.055`` se lee como 0.055%, que es lo que dice. Si alguien quiso
        decir 5.5%, el sitio para arreglarlo es la configuracion, no una
        heuristica que a veces acierta.
        """
        tasa = parseo.a_tasa("0.055")
        assert tasa is not None
        self.assertEqual(tasa.tasa_pct, Decimal("0.055"))
        self.assertEqual(tasa.bps, 5)
        self.assertFalse(tasa.exacta_en_bps)

    def test_una_tasa_que_no_cabe_en_puntos_base_se_declara_redondeada(self) -> None:
        tasa = parseo.a_tasa("5.555%")
        assert tasa is not None
        self.assertFalse(tasa.exacta_en_bps)
        self.assertEqual(tasa.tasa_pct, Decimal("5.555"))

    def test_fuera_de_rango_o_ilegible_es_none(self) -> None:
        for crudo in ("-1%", "101%", "muchisimo", "", None, True):
            with self.subTest(crudo=crudo):
                self.assertIsNone(parseo.a_tasa(crudo))


class SaldoDeLinntae(SimpleTestCase):
    def test_las_tres_bolsas_se_leen_separadas_y_no_se_suman(self) -> None:
        """Sumarlas daria mas de lo que se puede gastar en una recarga.

        La recarga sale de ``plataforma``. Que la bolsa de comision sea
        liquida es una pregunta que su especificacion no contesta, asi que
        sumarla seria vender contra dinero que quiza no se puede usar.
        """
        saldos = parseo.leer_saldos(
            {"code": 0, "plataforma": "$3,314.00", "comision": "$96.95"}
        )
        assert saldos.plataforma is not None
        self.assertEqual(saldos.plataforma.cents, 331_400)
        self.assertIsNone(saldos.servicios)
        self.assertEqual(saldos.crudos["plataforma"], "$3,314.00")

    def test_una_bolsa_ilegible_no_tumba_la_lectura(self) -> None:
        saldos = parseo.leer_saldos({"code": 0, "plataforma": "$10.00", "comision": "N/D"})
        assert saldos.plataforma is not None
        self.assertIsNone(saldos.comision)


#: Ejemplo LITERAL de ``/config/syncProducts`` de la especificacion: products
#: es una LISTA de objetos de una sola llave, y la seccion de virtuales se
#: llama "RECARGA VIRTUAL".
CATALOGO_FORMA_EJEMPLO = {
    "code": 0,
    "products": [
        {
            "TIEMPO AIRE": [
                {
                    "idOperator": 1,
                    "name": "Telcel",
                    "imageUrl": "https://ejemplo.test/telcel.png",
                    "offers": [
                        {"idOffer": 96, "amount": 10, "description": "1 DIA 50MB - WHATSAPP"},
                        {"idOffer": 1, "amount": 20, "description": ""},
                    ],
                }
            ]
        },
        {
            "RECARGA VIRTUAL": [
                {
                    "idOperator": 37,
                    "name": "Bait",
                    "offers": [{"idOffer": 628, "amount": 30, "description": "Recarga BAIT $30.00"}],
                }
            ]
        },
        {
            "SERVICIOS": [
                {
                    "sku": 1175,
                    "name": "AGUA CDMX (SACMEX)",
                    "amoutProvider": 6,
                }
            ]
        },
    ],
}

#: Forma del ESQUEMA de la misma especificacion: products es un OBJETO y la
#: seccion de virtuales se llama "VIRTUALES".
CATALOGO_FORMA_ESQUEMA = {
    "code": 0,
    "products": {
        "TIEMPO AIRE": [
            {
                "idOperator": 1,
                "name": "Telcel",
                "offers": [{"idOffer": 96, "amount": 10, "description": "x"}],
            }
        ],
        "VIRTUALES": [
            {
                "idOperator": 37,
                "name": "Bait",
                "offers": [{"idOffer": 628, "amount": 30, "description": "y"}],
            }
        ],
    },
}


class CatalogoEnSusDosFormas(SimpleTestCase):
    """La especificacion declara dos formas distintas. Se aceptan las dos.

    Si estas pruebas se caen, la sincronizacion de catalogo devolveria una
    lista vacia contra un proveedor que si tiene productos, y una lista vacia
    se propaga como "desactivalo todo".
    """

    def test_forma_del_ejemplo(self) -> None:
        catalogo = parseo.leer_catalogo(CATALOGO_FORMA_EJEMPLO)
        self.assertEqual(len(catalogo.companias), 2)
        self.assertEqual(catalogo.total_ofertas, 3)
        self.assertEqual(len(catalogo.con_sku), 1)
        self.assertEqual(catalogo.secciones_desconocidas, ())

        telcel = next(c for c in catalogo.companias if c.nombre == "Telcel")
        self.assertEqual(telcel.id_operator, 1)
        self.assertEqual(telcel.seccion, "TIEMPO AIRE")
        oferta = next(o for o in telcel.ofertas if o.id_offer == 96)
        assert oferta.monto is not None
        self.assertEqual(oferta.monto.cents, 1_000)

    def test_forma_del_esquema(self) -> None:
        catalogo = parseo.leer_catalogo(CATALOGO_FORMA_ESQUEMA)
        self.assertEqual(len(catalogo.companias), 2)
        self.assertEqual(catalogo.total_ofertas, 2)
        # La seccion se llama distinto en cada forma. Se guarda como llego y
        # no se normaliza a una de las dos: clasificar por NOMBRE de seccion
        # es justo lo que esta prueba existe para no tener que hacer.
        self.assertEqual(
            {c.seccion for c in catalogo.companias}, {"TIEMPO AIRE", "VIRTUALES"}
        )

    def test_las_secciones_se_clasifican_por_forma_no_por_nombre(self) -> None:
        """Una seccion con nombre nuevo se clasifica igual si trae idOperator."""
        catalogo = parseo.leer_catalogo(
            {
                "code": 0,
                "products": {
                    "ALGO QUE LINNTAE INVENTE MANANA": [
                        {
                            "idOperator": 9,
                            "name": "Nuevo",
                            "offers": [{"idOffer": 5, "amount": 50}],
                        }
                    ]
                },
            }
        )
        self.assertEqual(len(catalogo.companias), 1)
        self.assertEqual(catalogo.secciones_desconocidas, ())

    def test_una_seccion_irreconocible_se_reporta_no_se_ignora(self) -> None:
        catalogo = parseo.leer_catalogo(
            {
                "code": 0,
                "products": {
                    "TIEMPO AIRE": [
                        {"idOperator": 1, "name": "Telcel", "offers": [{"idOffer": 1, "amount": 20}]}
                    ],
                    "RARO": [{"cosa": 1}],
                },
            }
        )
        self.assertEqual(catalogo.secciones_desconocidas, ("RARO",))

    def test_una_oferta_sin_idoffer_no_es_una_oferta(self) -> None:
        """Sin idOffer no hay nada que mandarle a Linntae: es inejecutable."""
        catalogo = parseo.leer_catalogo(
            {
                "code": 0,
                "products": {
                    "TIEMPO AIRE": [
                        {
                            "idOperator": 1,
                            "name": "Telcel",
                            "offers": [{"amount": 20, "description": "sin id"}],
                        }
                    ]
                },
            }
        )
        self.assertEqual(catalogo.total_ofertas, 0)

    def test_una_respuesta_que_no_se_entiende_levanta(self) -> None:
        """No devuelve lista vacia. Una lista vacia se lee como "no vende nada"."""
        with self.assertRaises(parseo.RespuestaIlegible):
            parseo.leer_catalogo({"code": 0})
        with self.assertRaises(parseo.RespuestaIlegible):
            parseo.leer_catalogo({"code": 0, "products": "texto"})
        with self.assertRaises(parseo.RespuestaIlegible):
            parseo.leer_catalogo({"code": 0, "products": {"X": [{"nada": 1}]}})


class ComisionesEnSusDosFormas(SimpleTestCase):
    def test_forma_plana_del_ejemplo(self) -> None:
        comisiones = parseo.leer_comisiones(
            {
                "code": 0,
                "message": "Consulta exitosa",
                "data": [
                    {"comision": "5.5%", "id": 1, "nombre": "TELCEL"},
                    {"comision": "5.5%", "id": 24, "nombre": "TELCEL SIN LIMITES"},
                ],
            }
        )
        self.assertEqual(len(comisiones), 2)
        self.assertEqual(comisiones[0].alcance, parseo.AlcanceComision.OPERADOR)
        self.assertEqual(comisiones[0].clave, "1")
        assert comisiones[0].tasa is not None
        self.assertEqual(comisiones[0].tasa.bps, 550)

    def test_forma_agrupada_del_esquema(self) -> None:
        comisiones = parseo.leer_comisiones(
            {
                "code": 0,
                "data": [
                    {
                        "key": "tae",
                        "title": "Tiempo aire",
                        "list": [
                            {"comision": "6%", "sku": "96", "name": "TELCEL $10"},
                            {"comision": "5%", "sku": "628", "name": "BAIT $30"},
                        ],
                    }
                ],
            }
        )
        self.assertEqual(len(comisiones), 2)
        self.assertEqual(comisiones[0].alcance, parseo.AlcanceComision.SKU)
        self.assertEqual(comisiones[0].categoria, "Tiempo aire")
        assert comisiones[0].tasa is not None
        self.assertEqual(comisiones[0].tasa.bps, 600)

    def test_una_tasa_ilegible_se_guarda_como_desconocida(self) -> None:
        comisiones = parseo.leer_comisiones(
            {"code": 0, "data": [{"comision": "consultar", "id": 1, "nombre": "TELCEL"}]}
        )
        self.assertIsNone(comisiones[0].tasa)

    def test_una_respuesta_irreconocible_levanta(self) -> None:
        with self.assertRaises(parseo.RespuestaIlegible):
            parseo.leer_comisiones({"code": 0})
        with self.assertRaises(parseo.RespuestaIlegible):
            parseo.leer_comisiones({"code": 0, "data": [{"cosa": 1}]})


class VentasDelHistorico(SimpleTestCase):
    def test_se_lee_el_ejemplo_de_la_especificacion(self) -> None:
        ventas = parseo.leer_ventas(
            {
                "code": 0,
                "list": [
                    {
                        "amount": 200,
                        "totalAmount": 200,
                        "productName": "TELCEL $200",
                        "reference": "7772565412",
                        "carrierName": "TELCEL",
                        "folio": "12311057912",
                        "successTransaction": True,
                        "id": 6785,
                        "registerDate": "11/07/2025 13:09:47",
                        "message": "Pago realizado correctamente",
                    }
                ],
            }
        )
        self.assertEqual(len(ventas), 1)
        venta = ventas[0]
        self.assertEqual(venta.id_venta, 6785)
        assert venta.monto is not None
        self.assertEqual(venta.monto.cents, 20_000)
        self.assertIs(venta.exitosa, True)

    def test_sin_successtransaction_queda_en_none_no_en_false(self) -> None:
        """"No lo dijo" y "dijo que no" se tratan distinto en conciliacion.

        Leerlo como False cerraria como FALLIDA una recarga sobre la que
        Linntae no afirmo nada, y eso dispara un reembolso.
        """
        ventas = parseo.leer_ventas({"code": 0, "list": [{"id": 1, "reference": "5512345678"}]})
        self.assertIsNone(ventas[0].exitosa)

    def test_sin_lista_levanta(self) -> None:
        with self.assertRaises(parseo.RespuestaIlegible):
            parseo.leer_ventas({"code": 0})


class CodigosYConsecuencias(SimpleTestCase):
    """Un codigo desconocido en una COMPRA no autoriza a decir que no paso nada."""

    def test_cero_es_exito(self) -> None:
        self.assertIs(codigos.consecuencia_de_compra(0), Consecuencia.EJECUTADA)

    def test_rechazos_definitivos(self) -> None:
        for code in (1, 2, 3):
            with self.subTest(code=code):
                self.assertIs(
                    codigos.consecuencia_de_compra(code), Consecuencia.NO_EJECUTADA
                )

    def test_compania_con_intermitencia_es_indeterminada(self) -> None:
        """code 22 nombra una compania y solo aparece en endpoints de compra.

        Es el desenlace de un intento, no una puerta previa, y la
        especificacion no dice en que momento fallo. Darlo por fallido
        arriesga reembolsar una recarga aplicada.
        """
        self.assertIs(codigos.consecuencia_de_compra(22), Consecuencia.INDETERMINADA)

    def test_mantenimiento_es_no_ejecutada(self) -> None:
        """code 23 aparece tambien en las lecturas: es una puerta global."""
        self.assertIs(
            codigos.consecuencia_de_compra(23), Consecuencia.NO_EJECUTADA_TRANSITORIA
        )

    def test_duplicada_no_es_un_error(self) -> None:
        self.assertIs(codigos.consecuencia_de_compra(24), Consecuencia.DUPLICADA)

    def test_un_codigo_desconocido_en_compra_es_indeterminado(self) -> None:
        for code in (7, 99, None, 12345):
            with self.subTest(code=code):
                self.assertIs(
                    codigos.consecuencia_de_compra(code), Consecuencia.INDETERMINADA
                )

    def test_quinientos_y_quinientos_tres_dependen_de_si_es_compra(self) -> None:
        """El mismo HTTP significa cosas distintas segun haya dinero en juego."""
        for status in (500, 503):
            with self.subTest(status=status):
                self.assertIs(
                    codigos.consecuencia_http(status, es_compra=True),
                    Consecuencia.INDETERMINADA,
                )
                self.assertIs(
                    codigos.consecuencia_http(status, es_compra=False),
                    Consecuencia.NO_EJECUTADA,
                )

    def test_el_403_geografico_se_distingue_del_403_de_permisos(self) -> None:
        """Linntae usa el MISMO 403 para las dos cosas.

        Confundirlos produce o un bucle de renovacion de token o una
        integracion caida hasta que alguien reinicie.
        """
        self.assertIs(
            codigos.consecuencia_http(
                403, mensaje="Error country-US-403", es_compra=False
            ),
            Consecuencia.GEOBLOQUEO,
        )
        self.assertIs(
            codigos.consecuencia_http(
                403, mensaje="Tu cuenta ha sido bloqueada", es_compra=False
            ),
            Consecuencia.PERMISOS,
        )
        self.assertEqual(codigos.es_geobloqueo("Error country-MX-403"), "MX")
        self.assertIsNone(codigos.es_geobloqueo("Tu cuenta esta inactiva"))

    def test_el_code_4_no_tiene_significado_global(self) -> None:
        """Significa una cosa en /getToken y otra en /sale/unlock."""
        self.assertIn(
            "contrasena", codigos.significado_code_4(Endpoint.TOKEN).lower()
        )
        # Para un endpoint sin traduccion propia se devuelve el mensaje del
        # proveedor, no una invencion.
        self.assertEqual(
            codigos.significado_code_4(Endpoint.VENTAS, "no existe una venta"),
            "no existe una venta",
        )

    def test_las_rutas_conservan_la_errata_de_linntae(self) -> None:
        """``checkTransacctionTae`` con doble c es la ruta REAL.

        Corregirla produce un 404, y un 404 en la consulta de conciliacion
        significa quedarse sin poder averiguar que paso con una recarga.
        """
        self.assertEqual(str(Endpoint.CONSULTA_TAE), "sale/checkTransacctionTae")
