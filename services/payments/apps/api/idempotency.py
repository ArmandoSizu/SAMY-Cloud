"""Decorador de idempotencia para endpoints que mutan estado.

Como funciona, y por que asi:

1. Se exige la cabecera ``Idempotency-Key``. Sin ella la peticion se rechaza.
   Preferimos un 400 explicito a permitir que una operacion de dinero llegue
   sin proteccion contra duplicados.

2. Se intenta **INSERT** del registro con estado IN_PROGRESS. La proteccion es
   la restriccion ``UNIQUE(scope, key)`` de PostgreSQL, no una consulta previa.
   Un "consulta si existe y si no crea" tiene una ventana entre ambas
   operaciones por la que caben dos peticiones concurrentes.

3. Si el INSERT choca con el UNIQUE, ya hubo una peticion identica:
   - Si termino, se devuelve su respuesta guardada. El cliente recibe
     exactamente el mismo resultado que la primera vez.
   - Si sigue en curso, se responde 409 para que reintente en un momento.

4. Se compara el hash del cuerpo. Misma clave con cuerpo distinto es un error
   del cliente y se responde 422: devolver la respuesta de otra operacion
   seria peor que fallar.

Los fallos tambien se registran: reintentar con la misma clave tras un error
de negocio devuelve el mismo error, no ejecuta la operacion de nuevo.
"""

from __future__ import annotations

import json
from functools import wraps
from typing import Callable

import structlog
from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework import status
from rest_framework.request import Request
from rest_framework.response import Response

from apps.api.models import IdempotencyRecord, IdempotencyStatus
from samy_common.idempotency.models import hash_payload

log = structlog.get_logger("idempotency")

#: Cuanto se conserva un registro de idempotencia. 24 h cubre de sobra
#: cualquier reintento razonable de un cliente o de un proveedor.
RETENTION_HOURS = 24


def idempotent(scope: str) -> Callable:
    """Hace idempotente un endpoint de DRF."""

    def decorator(view_func: Callable) -> Callable:
        @wraps(view_func)
        def _wrapped(request: Request, *args, **kwargs) -> Response:
            key = request.headers.get("Idempotency-Key", "").strip()

            if not key:
                return Response(
                    {
                        "error": {
                            "code": "idempotency_key_required",
                            "message": (
                                "Esta operacion requiere la cabecera "
                                "Idempotency-Key."
                            ),
                        }
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )

            body = request.body or b""
            request_hash = hash_payload(body)
            # El ambito incluye la tienda: dos tiendas distintas pueden usar
            # la misma clave sin colisionar.
            store_id = _extract_store_id(request)
            full_scope = f"{scope}:{store_id}" if store_id else scope

            try:
                with transaction.atomic():
                    record = IdempotencyRecord.objects.create(
                        key=key,
                        scope=full_scope,
                        request_hash=request_hash,
                        status=IdempotencyStatus.IN_PROGRESS,
                        expires_at=timezone.now() + timezone.timedelta(hours=RETENTION_HOURS),
                    )
            except IntegrityError:
                return _handle_duplicate(full_scope, key, request_hash)

            try:
                response = view_func(request, *args, **kwargs)
            except Exception:
                # Una excepcion no controlada libera la clave: la operacion no
                # se completo y el cliente debe poder reintentarla.
                IdempotencyRecord.objects.filter(pk=record.pk).delete()
                raise

            payload = _serializable(response)
            if 200 <= response.status_code < 300:
                record.mark_completed(response.status_code, payload)
            else:
                record.mark_failed(response.status_code, payload)

            return response

        return _wrapped

    return decorator


def _handle_duplicate(scope: str, key: str, request_hash: str) -> Response:
    record = IdempotencyRecord.objects.filter(scope=scope, key=key).first()

    if record is None:  # pragma: no cover - carrera con la purga
        return Response(
            {
                "error": {
                    "code": "idempotency_conflict",
                    "message": "Reintenta en unos segundos.",
                }
            },
            status=status.HTTP_409_CONFLICT,
        )

    if record.request_hash != request_hash:
        log.warning(
            "idempotency_key_reused_with_different_body", scope=scope, key=key
        )
        return Response(
            {
                "error": {
                    "code": "idempotency_key_reused",
                    "message": (
                        "Esta clave de idempotencia ya se uso con datos "
                        "distintos. Usa una clave nueva."
                    ),
                }
            },
            status=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )

    if record.status == IdempotencyStatus.IN_PROGRESS:
        return Response(
            {
                "error": {
                    "code": "idempotency_in_progress",
                    "message": (
                        "Una peticion identica se esta procesando. "
                        "Reintenta en unos segundos."
                    ),
                }
            },
            status=status.HTTP_409_CONFLICT,
            headers={"Retry-After": "2"},
        )

    log.info("idempotency_replay", scope=scope, key=key, status=record.status)
    return Response(
        record.response_body,
        status=record.response_status_code or status.HTTP_200_OK,
        headers={"Idempotency-Replayed": "true"},
    )


def _extract_store_id(request: Request) -> str:
    try:
        if request.method in {"POST", "PUT", "PATCH"} and request.body:
            data = json.loads(request.body)
            if isinstance(data, dict):
                return str(data.get("store_id", ""))
    except (json.JSONDecodeError, UnicodeDecodeError):
        pass
    return str(request.query_params.get("store_id", ""))


def _serializable(response: Response) -> dict | list | None:
    data = getattr(response, "data", None)
    if data is None:
        return None
    # Fuerza la serializacion para que lo guardado sea JSON puro y no objetos
    # de DRF que no sobreviven a un round-trip por la base de datos.
    try:
        return json.loads(json.dumps(data, default=str))
    except (TypeError, ValueError):  # pragma: no cover
        return None
