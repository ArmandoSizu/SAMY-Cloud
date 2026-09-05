"""Middleware que resuelve la tienda activa de cada peticion.

La tienda activa se guarda en sesion, no en la URL ni en una cabecera que el
cliente controle. En cada peticion se **revalida** que la membresia sigue
existiendo y activa: si a un cajero se le retira el acceso, deja de operar en
la siguiente peticion, sin esperar a que caduque su sesion.

Este es el punto unico donde se establece ``request.store`` y
``request.membership``; todo el sistema de permisos lee de ahi.
"""

from __future__ import annotations

from typing import Callable

import structlog
from django.http import HttpRequest, HttpResponse

SESSION_KEY_STORE = "samy_active_store_id"

log = structlog.get_logger("tenancy")


class CurrentStoreMiddleware:
    """Publica ``request.store`` y ``request.membership``."""

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        request.store = None  # type: ignore[attr-defined]
        request.membership = None  # type: ignore[attr-defined]

        user = getattr(request, "user", None)
        if user is not None and user.is_authenticated:
            membership = self._resolve_membership(request)
            if membership is not None:
                request.membership = membership  # type: ignore[attr-defined]
                request.store = membership.store  # type: ignore[attr-defined]
                structlog.contextvars.bind_contextvars(
                    store_id=str(membership.store_id), role=membership.role
                )

        response = self.get_response(request)
        structlog.contextvars.unbind_contextvars("store_id", "role")
        return response

    def _resolve_membership(self, request: HttpRequest):
        """Devuelve la membresia activa, revalidada contra la base de datos."""
        user = request.user
        store_id = request.session.get(SESSION_KEY_STORE)

        if store_id:
            # Revalidacion en cada peticion: el id en sesion no basta como
            # prueba de acceso, solo indica cual tienda se quiere usar.
            membership = user.membership_for(store_id)
            if membership is not None:
                return membership
            # La membresia ya no es valida: se limpia y se cae al default.
            log.warning(
                "membership_revoked_or_invalid",
                user_id=str(user.pk),
                attempted_store_id=str(store_id),
            )
            request.session.pop(SESSION_KEY_STORE, None)

        membership = user.default_membership()
        if membership is not None:
            request.session[SESSION_KEY_STORE] = str(membership.store_id)
        return membership


def set_active_store(request: HttpRequest, store_id: str) -> bool:
    """Cambia la tienda activa. Devuelve False si el usuario no pertenece a ella.

    Se llama desde la vista de cambio de tienda. Nunca confia en el valor
    recibido: comprueba la membresia antes de escribir en sesion.
    """
    membership = request.user.membership_for(store_id)
    if membership is None:
        log.warning(
            "store_switch_denied",
            user_id=str(request.user.pk),
            attempted_store_id=str(store_id),
        )
        return False
    request.session[SESSION_KEY_STORE] = str(membership.store_id)
    request.store = membership.store  # type: ignore[attr-defined]
    request.membership = membership  # type: ignore[attr-defined]
    return True
