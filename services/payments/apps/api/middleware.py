"""Autenticacion servicio-a-servicio por firma HMAC.

Este middleware es la puerta del microservicio. Sin una firma valida no se
entra, punto. No hay usuarios ni sesiones aqui: la unica identidad que existe
es "que servicio esta llamando".

Protecciones:

* **Firma HMAC-SHA256** sobre metodo, ruta, timestamp, nonce y hash del cuerpo.
* **Ventana temporal** de 5 minutos: una peticion capturada no sirve manana.
* **Nonce de un solo uso**, guardado en Redis con el mismo TTL que la ventana.
  Sin esto, una peticion interceptada podria repetirse dentro de la ventana y
  duplicar una operacion.
* **Lista blanca de servicios**: aunque alguien obtenga el secreto, solo los
  servicios declarados pueden llamar.

Se exceptuan las rutas de salud (el orquestador las consulta sin firmar) y los
webhooks de proveedores, que traen su propia firma y se verifican en la vista
con la llave publica del proveedor correspondiente.
"""

from __future__ import annotations

from typing import Callable

import structlog
from django.conf import settings
from django.core.cache import cache
from django.http import HttpRequest, HttpResponse, JsonResponse

from samy_common.security.signing import HEADER_NONCE, SignatureError, verify_request

log = structlog.get_logger("s2s.auth")

#: Prefijo de las claves de nonce en Redis.
NONCE_CACHE_PREFIX = "samy:s2s:nonce:"


class ServiceAuthMiddleware:
    """Exige firma S2S valida en toda peticion a la API."""

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        if self._is_exempt(request.path):
            return self.get_response(request)

        try:
            caller = self._verify(request)
        except SignatureError as exc:
            log.warning(
                "s2s_auth_rejected",
                path=request.path,
                method=request.method,
                reason=str(exc),
                remote_addr=request.META.get("REMOTE_ADDR"),
            )
            # 401 sin detalle: explicar por que fallo la firma le da a un
            # atacante informacion para afinar el siguiente intento.
            return JsonResponse(
                {
                    "error": {
                        "code": "unauthorized",
                        "message": "Firma de servicio invalida o ausente.",
                    }
                },
                status=401,
            )

        request.calling_service = caller  # type: ignore[attr-defined]
        structlog.contextvars.bind_contextvars(calling_service=caller)

        response = self.get_response(request)
        structlog.contextvars.unbind_contextvars("calling_service")
        return response

    @staticmethod
    def _is_exempt(path: str) -> bool:
        return path.startswith(tuple(settings.SERVICE_AUTH_EXEMPT_PREFIXES))

    def _verify(self, request: HttpRequest) -> str:
        nonce = request.headers.get(HEADER_NONCE, "")
        nonce_key = f"{NONCE_CACHE_PREFIX}{nonce}" if nonce else ""

        # ``cache.add`` es atomico: devuelve False si la clave ya existia. Es
        # justo la primitiva que se necesita para consumir un nonce una sola
        # vez sin condicion de carrera entre workers.
        already_seen = False
        if nonce_key:
            already_seen = not cache.add(
                nonce_key, "1", timeout=settings.SERVICE_S2S_TOLERANCE_SECONDS
            )

        caller = verify_request(
            secret=settings.SERVICE_S2S_SECRET,
            method=request.method or "GET",
            path=request.path,
            headers=dict(request.headers),
            body=request.body,
            tolerance_seconds=settings.SERVICE_S2S_TOLERANCE_SECONDS,
            seen_nonce=already_seen,
        )

        if caller not in settings.SERVICE_ALLOWED_CALLERS:
            raise SignatureError(f"El servicio '{caller}' no esta autorizado.")

        return caller
