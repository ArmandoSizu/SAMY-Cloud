"""Permisos de la API del microservicio.

La autenticacion la resolvio ``ServiceAuthMiddleware``. Aqui solo se confirma
que efectivamente hubo verificacion: si por un error de configuracion el
middleware se quitara del stack, esta comprobacion hace que la API deje de
responder en vez de quedar abierta.

Es defensa en profundidad: dos controles independientes tienen que fallar a la
vez para que se cuele una peticion sin firmar.
"""

from __future__ import annotations

from rest_framework import permissions


class IsSignedService(permissions.BasePermission):
    message = "Se requiere una peticion firmada por un servicio autorizado."

    def has_permission(self, request, view) -> bool:
        return bool(getattr(request, "calling_service", None))
