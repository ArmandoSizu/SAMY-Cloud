"""Content Security Policy, en un solo sitio para produccion y desarrollo.

Por que esta separado
---------------------
Una CSP que solo existe en produccion es una CSP que se descubre rota EN
produccion. Ya paso una vez en este proyecto: la pantalla de cobro con tarjeta
llevaba su JavaScript en un ``<script>`` en linea; en desarrollo (sin CSP)
funcionaba, y con la CSP de produccion el navegador lo habria bloqueado sin
decir nada visible al cajero, dejando el formulario de la tarjeta en blanco.

Compartiendo las directivas, desarrollo puede aplicarlas en modo **solo
reporte**: no bloquea nada, pero el navegador escribe en la consola cada
recurso que produccion rechazaria. El fallo aparece el dia que se escribe el
codigo, no el dia del despliegue.
"""

from __future__ import annotations

from typing import Any, Final

#: Directivas de la politica. Se aplican tal cual en produccion y en modo solo
#: reporte en desarrollo.
DIRECTIVAS: Final[dict[str, Any]] = {
    "default-src": ["'self'"],
    # El tokenizador de Conekta es la UNICA excepcion a "todo desde nuestro
    # origen", y es una excepcion que MEJORA la seguridad, no que la relaja:
    # el numero de tarjeta se captura dentro de un iframe de Conekta y nunca
    # toca nuestro dominio. Autoalojar ese script es imposible (Conekta no lo
    # distribuye) y, aunque se pudiera, nos meteria el PAN en casa y con el
    # todo PCI DSS en lugar de SAQ A.
    #
    # El alcance es minimo y deliberado: solo pay.conekta.com. No se abre
    # 'unsafe-inline', ni 'unsafe-eval', ni ningun comodin.
    "script-src": ["'self'", "https://pay.conekta.com"],
    # 'self' no es relleno: zoid (el motor de componentes que usa Conekta)
    # crea primero un iframe sin src, que hereda nuestro origen, y despues lo
    # apunta a pay.conekta.com. Sin 'self' ese primer paso se bloquea y el
    # formulario no llega a montarse.
    "frame-src": ["'self'", "https://pay.conekta.com"],
    # child-src es el nombre que entienden los navegadores anteriores a CSP3,
    # donde frame-src no existe. Se declara igual para que la pantalla de
    # cobro no dependa de la version del navegador del mostrador.
    "child-src": ["'self'", "https://pay.conekta.com"],
    "style-src": ["'self'"],
    "img-src": ["'self'", "data:", "blob:"],
    "font-src": ["'self'"],
    # El iframe del tokenizador habla con la API de Conekta desde el navegador
    # para crear el token; zoid ademas intercambia mensajes de arranque contra
    # pay.conekta.com.
    "connect-src": ["'self'", "https://api.conekta.io", "https://pay.conekta.com"],
    # blob: es necesario para el flujo de camara del lector de codigos.
    "media-src": ["'self'", "blob:"],
    "worker-src": ["'self'", "blob:"],
    "frame-ancestors": ["'none'"],
    "form-action": ["'self'"],
    "base-uri": ["'self'"],
    "object-src": ["'none'"],
}

#: En produccion, ademas, se fuerza HTTPS. En desarrollo no: el navegador
#: entra por http://localhost y esta directiva romperia todos los recursos.
DIRECTIVAS_PRODUCCION: Final[dict[str, Any]] = {
    **DIRECTIVAS,
    "upgrade-insecure-requests": True,
}
