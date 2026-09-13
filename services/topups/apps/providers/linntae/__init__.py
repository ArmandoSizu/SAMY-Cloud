"""Integracion con Linntae.

    ESTADO: DEMO. Ninguna operacion productiva esta habilitada.

Las piezas estan separadas por responsabilidad, y la separacion es la que
permite probar lo que importa sin red:

``codigos.py``
    Traduce los codigos de Linntae a consecuencias de dinero: ejecutada, no
    ejecutada, o no se sabe.

``parseo.py``
    Convierte sus respuestas a tipos de SAMY. Aqui vive el parser de
    ``"$3,314.00"`` y la tolerancia a las dos formas que su especificacion
    declara para el catalogo y las comisiones.

``auth.py``
    Politica del token: cache compartida, renovacion por evidencia y tope de
    reintentos. Sin transporte HTTP.

``client.py``
    Transporte. Tiempos de espera, sesion reutilizable, guarda de host por
    ambiente, y dos metodos distintos -``leer()`` y ``comprar()``- porque las
    lecturas se reintentan y las compras no.

``conciliacion.py``
    Averiguar que paso cuando la compra no fue concluyente, sin reintentarla.

``provider.py``
    El adaptador que implementa ``TopupProvider``.

Importar este paquete registra ``LinntaeProvider`` en el registro de
proveedores. Registrarlo no lo habilita: sigue necesitando credenciales,
ambiente coherente, ``LINNTAE_TYPE_BALANCE`` y
``ALLOW_REAL_PROVIDER_TRANSACTIONS``.
"""

from __future__ import annotations

from apps.providers.linntae.client import LinntaeClient, LinntaeConfig
from apps.providers.linntae.provider import LinntaeProvider

__all__ = ["LinntaeProvider", "LinntaeClient", "LinntaeConfig"]
