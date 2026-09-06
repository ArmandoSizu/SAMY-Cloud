"""La pantalla de cobro con tarjeta tiene que poder MOSTRARSE.

Estas pruebas no comprueban que se cobre: comprueban que el formulario de
Conekta pueda aparecer. Es una distincion que costo un rato descubrir, porque
los dos fallos que la rompieron eran invisibles: la pagina cargaba entera, con
su total y sus avisos, y el area del formulario quedaba en blanco sin un solo
error en consola.

Fallo 1 - el iframe con cero pixeles de alto
    Conekta monta el formulario con zoid, que inyecta un envoltorio y dentro
    un iframe absoluto con alto 100%. Sin un ancestro posicionado y con altura
    real, ese 100% no resuelve y el iframe mide cero. Se defiende con la clase
    .conekta-tokenizer.

Fallo 2 - el <script> en linea, latente hasta el despliegue
    La CSP de produccion es "script-src 'self' https://pay.conekta.com", sin
    'unsafe-inline'. Un <script> escrito en la plantilla funciona en
    desarrollo y el navegador lo bloquea en produccion: la pantalla de cobro
    se veria completa y sin formulario de tarjeta. Se defiende exigiendo que
    el codigo viva en un archivo estatico.
"""

from __future__ import annotations

import re
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

RAIZ = Path(settings.BASE_DIR)
PLANTILLA = RAIZ / "templates" / "operations" / "card.html"
JS_TOKENIZADOR = RAIZ / "static" / "js" / "conekta-tokenizer.js"
CSS_FUENTE = RAIZ / "assets" / "app.css"
#: Artefacto de compilacion: no se versiona, puede no existir en un clon nuevo.
CSS_COMPILADO = RAIZ / "static" / "css" / "app.css"

#: Script oficial vigente del tokenizador.
SCRIPT_OFICIAL = "https://pay.conekta.com/v1.0/js/conekta-checkout.min.js"


def sin_comentarios(plantilla: str) -> str:
    """Quita los bloques {% comment %}.

    Las notas de esta plantilla explican por que NO debe haber un <script> en
    linea, y para explicarlo escriben la palabra. Buscar sobre el texto crudo
    encontraria esas menciones y la prueba fallaria acusando al comentario que
    documenta la regla, que seria una forma tonta de romperse.
    """
    return re.sub(
        r"\{%\s*comment\s*%\}.*?\{%\s*endcomment\s*%\}",
        "",
        plantilla,
        flags=re.DOTALL,
    )


def sin_comentarios_js(fuente: str) -> str:
    """Quita comentarios de bloque y de linea de un archivo JavaScript."""
    sin_bloques = re.sub(r"/\*.*?\*/", "", fuente, flags=re.DOTALL)
    return re.sub(r"//[^\n]*", "", sin_bloques)


class PlantillaTarjetaTests(SimpleTestCase):
    """Lo que la plantilla debe y no debe contener."""

    def setUp(self) -> None:
        self.crudo = PLANTILLA.read_text(encoding="utf-8")
        self.html = sin_comentarios(self.crudo)

    def test_carga_el_script_oficial_de_conekta(self) -> None:
        self.assertIn(SCRIPT_OFICIAL, self.html)

    def test_no_hay_javascript_en_linea(self) -> None:
        """Un <script> sin src lo bloquea la CSP de produccion."""
        etiquetas = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>", self.html)
        self.assertEqual(
            etiquetas,
            [],
            "Hay JavaScript en linea en card.html. La CSP de produccion lo "
            "bloquea y el formulario de tarjeta no aparecera al desplegar. "
            "Muevelo a static/js/.",
        )

    def test_no_hay_bloques_de_estilo_en_linea(self) -> None:
        """Un <style> tambien lo bloquea la CSP (style-src 'self')."""
        self.assertNotIn("<style", self.html)

    def test_referencia_el_archivo_del_tokenizador(self) -> None:
        self.assertIn("js/conekta-tokenizer.js", self.html)

    def test_el_contenedor_lleva_la_clase_que_le_da_altura(self) -> None:
        """Sin esta clase el iframe se monta con cero pixeles de alto."""
        self.assertRegex(
            self.html,
            r'id="conektaIframeContainer"[^>]*class="[^"]*conekta-tokenizer',
        )

    def test_la_llave_publica_viaja_como_atributo(self) -> None:
        self.assertIn('data-public-key="{{ public_key }}"', self.html)

    def test_no_hay_campos_propios_de_tarjeta(self) -> None:
        """El PAN y el CVV viven en el iframe de Conekta, nunca aqui.

        Un <input> propio para el numero de tarjeta sacaria a SAMY Cloud del
        alcance PCI DSS mas bajo (SAQ A) y lo metaria en PCI DSS completo.
        """
        for prohibido in ("card_number", "cardNumber", 'name="cvv"', "cvc"):
            self.assertNotIn(prohibido, self.html, f"Campo prohibido: {prohibido}")


class ArchivoTokenizadorTests(SimpleTestCase):
    """El JavaScript existe, usa la API vigente y no filtra datos."""

    def setUp(self) -> None:
        self.crudo = JS_TOKENIZADOR.read_text(encoding="utf-8")
        # Igual que en la plantilla: los comentarios explican por que no se
        # tocan el PAN ni el CVV, y para explicarlo los nombran.
        self.js = sin_comentarios_js(self.crudo)

    def test_el_archivo_existe(self) -> None:
        self.assertTrue(JS_TOKENIZADOR.is_file())

    def test_usa_el_componente_oficial_vigente(self) -> None:
        self.assertIn("ConektaCheckoutComponents.Card", self.js)

    def test_pasa_la_configuracion_que_exige_conekta(self) -> None:
        for clave in ("targetIFrame", "publicKey", "locale"):
            self.assertIn(clave, self.js, f"Falta config.{clave}")

    def test_implementa_los_tres_callbacks(self) -> None:
        for callback in (
            "onGetInfoSuccess",
            "onCreateTokenSucceeded",
            "onCreateTokenError",
        ):
            self.assertIn(callback, self.js, f"Falta el callback {callback}")

    def test_no_registra_el_token_completo(self) -> None:
        """Del token solo salen los ultimos caracteres, nunca entero."""
        self.assertIn("tokenParcial", self.js)
        self.assertNotRegex(
            self.js,
            r"console\.log\([^)]*token\.id",
            "No se debe registrar el identificador completo del token.",
        )

    def test_no_menciona_campos_de_tarjeta(self) -> None:
        for prohibido in ("cardnumber", "card_number", "cvv", "cvc"):
            self.assertNotIn(
                prohibido,
                self.js.lower(),
                f"El tokenizador no debe tocar {prohibido}.",
            )


class SinGiroInfinitoTests(SimpleTestCase):
    """El cajero nunca se queda mirando "Cobrando..." para siempre.

    El cobro dejo de depender de que el navegador siguiera la redireccion del
    POST. Ahora se envia con fetch, la navegacion la decide el JavaScript y
    hay un limite de tiempo. Si se agota, la pantalla lo dice y manda al
    comprobante en vez de girar indefinidamente.
    """

    def setUp(self) -> None:
        # Sin comentarios: el codigo explica por que ya NO usa requestSubmit,
        # y buscar sobre el texto crudo encontraria esa explicacion.
        self.js = sin_comentarios_js(JS_TOKENIZADOR.read_text(encoding="utf-8"))
        self.html = sin_comentarios(PLANTILLA.read_text(encoding="utf-8"))

    def test_el_cobro_se_envia_con_fetch(self) -> None:
        self.assertIn("fetch(", self.js)

    def test_ya_no_depende_de_la_navegacion_del_formulario(self) -> None:
        """requestSubmit deja el desenlace en manos del navegador."""
        self.assertNotIn(
            "requestSubmit",
            self.js,
            "Volver a requestSubmit reintroduce el giro infinito: si el "
            "navegador no sigue la redireccion, la pantalla se queda colgada.",
        )

    def test_hay_un_limite_de_tiempo(self) -> None:
        self.assertIn("AbortController", self.js)
        self.assertIn("TIEMPO_LIMITE_MS", self.js)

    def test_el_limite_supera_al_del_servidor(self) -> None:
        """Rendirse antes que el servidor haria contar una historia falsa."""
        encontrado = re.search(r"TIEMPO_LIMITE_MS\s*=\s*(\d+)", self.js)
        self.assertIsNotNone(encontrado)
        # core -> payments son 30 s; payments -> Conekta, 20 s.
        self.assertGreater(int(encontrado.group(1)), 30000)

    def test_no_se_reintenta_solo(self) -> None:
        """Reintentar tras un timeout es la receta del doble cargo."""
        self.assertNotIn("retry", self.js.lower())

    def test_existe_el_estado_indeterminado(self) -> None:
        self.assertIn("indeterminado", self.js)
        self.assertIn('x-show="indeterminado"', self.html)

    def test_el_estado_indeterminado_no_afirma_que_fallo(self) -> None:
        """Puede haberse cobrado: decir "fallo" seria mentir."""
        self.assertIn("pudo haberse realizado", self.html)

    def test_el_estado_indeterminado_lleva_al_comprobante(self) -> None:
        self.assertIn("data-receipt-url", self.html)
        self.assertIn("operations:receipt", self.html)

    def test_no_ofrece_reintentar_el_cobro(self) -> None:
        indeterminado = self.html[self.html.index('x-show="indeterminado"') :][:900]
        for tentacion in ("Reintentar", "Cobrar de nuevo", "Volver a cobrar"):
            self.assertNotIn(tentacion, indeterminado)


class EstilosTokenizadorTests(SimpleTestCase):
    """La regla que da altura al iframe, en la fuente y en lo compilado."""

    def test_la_regla_esta_en_la_fuente(self) -> None:
        """assets/app.css es lo que se versiona: aqui la regla debe existir."""
        css = CSS_FUENTE.read_text(encoding="utf-8").replace(" ", "")
        self.assertIn(".conekta-tokenizer", css)

        inicio = css.index(".conekta-tokenizer")
        bloque = css[inicio : inicio + 400]
        # Las dos mitades del arreglo. Sin position no hay contra que medir el
        # 100% del iframe; sin height el ancestro sigue en auto y el iframe
        # vuelve a quedar en cero.
        self.assertIn("position:relative", bloque)
        self.assertIn("height:", bloque)

    def test_la_regla_llego_al_css_compilado(self) -> None:
        """Editar la fuente no basta: hay que recompilar.

        app.css es un artefacto de compilacion y no se versiona, asi que en un
        clon recien hecho todavia no existe. En ese caso no hay nada que
        comprobar; lo que esta prueba atrapa es el caso real: tener un
        app.css compilado y desactualizado, que es lo que hace que el arreglo
        parezca no funcionar.
        """
        if not CSS_COMPILADO.is_file():
            self.skipTest("No hay CSS compilado todavia (npm run build:css).")

        css = CSS_COMPILADO.read_text(encoding="utf-8")
        self.assertIn(
            "conekta-tokenizer",
            css,
            "El CSS compilado esta desactualizado. Ejecuta: npm run build:css",
        )


#: Atributos de evento en linea. La CSP de produccion no lleva
#: 'unsafe-inline' en script-src, asi que el navegador los ignora en silencio.
_EVENTOS_EN_LINEA = re.compile(
    r"\son(click|submit|change|input|load|error|keyup|keydown|focus|blur)\s*=",
    re.IGNORECASE,
)


class SinJavaScriptEnLineaTests(SimpleTestCase):
    """Ninguna plantilla puede llevar manejadores de evento en linea.

    Es el mismo fallo del <script> en linea, en su version pequeña y por eso
    mas facil de colar: ``onclick="window.print()"`` funciona perfectamente en
    desarrollo, donde la CSP es solo de reporte, y en produccion el navegador
    lo bloquea sin escribir nada visible. El resultado es un boton que se ve
    bien, se puede pulsar y no hace absolutamente nada.

    Ya paso una vez, con el boton de imprimir del comprobante. La alternativa
    correcta es ``@click`` de Alpine, que no es JavaScript en linea para la
    CSP porque lo evalua la libreria, no el navegador.
    """

    def test_ninguna_plantilla_usa_manejadores_en_linea(self) -> None:
        culpables = []

        for plantilla in (RAIZ / "templates").rglob("*.html"):
            contenido = sin_comentarios(plantilla.read_text(encoding="utf-8"))
            for coincidencia in _EVENTOS_EN_LINEA.finditer(contenido):
                linea = contenido[: coincidencia.start()].count("\n") + 1
                culpables.append(
                    f"{plantilla.relative_to(RAIZ)}:{linea} -> "
                    f"{coincidencia.group().strip()}"
                )

        self.assertEqual(
            culpables,
            [],
            "Manejadores de evento en linea: la CSP de produccion los bloquea "
            "y el boton queda muerto sin avisar. Usa @click de Alpine.\n  "
            + "\n  ".join(culpables),
        )


class ComprobanteTests(SimpleTestCase):
    """El comprobante es lo unico que el cliente se lleva.

    Si le falta un dato, el cliente no tiene con que reclamar. Estas pruebas
    fijan los campos que no pueden desaparecer de la plantilla.
    """

    COMPROBANTE = RAIZ / "templates" / "operations" / "receipt.html"

    def setUp(self) -> None:
        self.contenido = self.COMPROBANTE.read_text(encoding="utf-8")

    def test_lleva_el_desglose_del_dinero(self) -> None:
        for campo in ("order.base_display", "order.commission_display",
                      "order.total_display"):
            with self.subTest(campo=campo):
                self.assertIn(campo, self.contenido)

    def test_lleva_folio_y_estado(self) -> None:
        self.assertIn("order.folio", self.contenido)
        self.assertIn("order.state", self.contenido)

    def test_lleva_el_telefono_enmascarado_y_nunca_el_completo(self) -> None:
        """El numero se muestra enmascarado: el comprobante se queda en el local."""
        self.assertIn("fulfillment.phone_masked", self.contenido)
        self.assertNotIn("fulfillment.phone_e164", self.contenido)
        self.assertNotIn("fulfillment.phone ", self.contenido)

    def test_lleva_la_referencia_con_la_que_se_reclama(self) -> None:
        """Sin folio del proveedor el cliente no puede reclamar la recarga."""
        self.assertIn("fulfillment.operator_reference", self.contenido)
        self.assertIn("fulfillment.provider_reference", self.contenido)

    def test_avisa_cuando_la_operacion_es_de_prueba(self) -> None:
        """Un comprobante de sandbox no puede parecer una operacion normal."""
        self.assertIn("OPERACION DE PRUEBA", self.contenido)
        self.assertIn('fulfillment.provider_mode == "SANDBOX"', self.contenido)

    def test_no_anuncia_exito_antes_de_tiempo(self) -> None:
        """Pagada y entregada son cosas distintas y el ticket las distingue."""
        self.assertIn("PAGADA - EN PROCESO", self.contenido)
        self.assertIn("COMPLETADA", self.contenido)
