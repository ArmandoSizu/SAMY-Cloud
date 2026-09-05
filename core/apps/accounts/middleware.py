"""Obliga a cambiar una contrasena temporal antes de operar.

Cuando el dueno da de alta a un cajero, el sistema genera una clave temporal
que el dueno le dicta o le apunta. Esa clave la conoce al menos una persona
mas que su titular, asi que no puede quedarse puesta: mientras siga vigente,
lo que haga ese cajero no se le puede atribuir con certeza, y en un sistema
que mueve dinero eso vacia de valor la auditoria.

El campo ``User.must_change_password`` existia desde el principio, pero nada
lo hacia cumplir. Un campo que nadie comprueba es una promesa incumplida;
este middleware es lo que lo convierte en una regla de verdad.
"""

from __future__ import annotations

from typing import Callable

from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect
from django.urls import reverse

#: Rutas que siguen accesibles con la contrasena temporal puesta. Sin esta
#: lista, la propia pantalla de cambio de contrasena se redirigiria a si misma
#: y la sesion quedaria atrapada en un bucle.
_RUTAS_PERMITIDAS = (
    "accounts:password_change",
    "accounts:logout",
    "accounts:password_reset",
    "accounts:password_reset_done",
    "accounts:password_reset_complete",
)

#: Prefijos que nunca se interceptan: sondas de salud, estaticos, y el enlace
#: de recuperacion, que lleva parametros en la URL y no se puede invertir.
_PREFIJOS_LIBRES = ("/health/", "/static/", "/media/", "/recuperar/")


class ForcePasswordChangeMiddleware:
    """Redirige a cambiar la contrasena mientras ``must_change_password`` este activo."""

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response
        self._permitidas: set[str] | None = None

    def __call__(self, request: HttpRequest) -> HttpResponse:
        user = getattr(request, "user", None)

        if (
            user is not None
            and user.is_authenticated
            and getattr(user, "must_change_password", False)
            and not self._esta_permitida(request.path)
        ):
            return redirect("accounts:password_change")

        return self.get_response(request)

    def _esta_permitida(self, path: str) -> bool:
        if path.startswith(_PREFIJOS_LIBRES):
            return True
        # Se resuelven una sola vez y se guardan: invertir seis rutas en cada
        # peticion de cada usuario no aporta nada.
        if self._permitidas is None:
            self._permitidas = {reverse(nombre) for nombre in _RUTAS_PERMITIDAS}
        return path in self._permitidas
