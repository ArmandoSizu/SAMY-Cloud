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
        sin_bloques = re.sub(r"/\*.*?\*/", "", self.crudo, flags=re.DOTALL)
        self.js = re.sub(r"//[^\n]*", "", sin_bloques)

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
