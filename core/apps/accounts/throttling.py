"""Limitacion de intentos para vistas que no pasan por django-axes.

``django-axes`` protege el login porque se engancha al backend de
autenticacion. El registro no autentica a nadie, asi que no lo cubre, y sin
limite un script puede crear cuentas en masa o averiguar que correos ya estan
dados de alta probandolos uno por uno.

Implementacion: contador en cache con ventana fija. La cache es Redis, donde
``add`` e ``incr`` son atomicos, asi que dos peticiones simultaneas no pueden
colarse por una condicion de carrera como pasaria con un
``get``-comprueba-``set``.

Ventana fija y no deslizante a proposito: en el peor caso permite el doble del
limite en el cambio de ventana, y a cambio cuesta una sola clave por
identificador en vez de una lista de marcas de tiempo. Para frenar abuso es
mas que suficiente; esto no es un contador de facturacion.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.core.cache import cache
from django.http import HttpRequest


@dataclass(frozen=True, slots=True)
class RateLimit:
    """Cuantos intentos se permiten y en cuanto tiempo."""

    limit: int
    window_seconds: int

    @property
    def window_minutes(self) -> int:
        return max(1, self.window_seconds // 60)


#: Alta de cuentas por IP. Un negocio real se registra una vez; cinco intentos
#: en una hora cubre de sobra los errores de captura.
SIGNUP_RATE = RateLimit(limit=5, window_seconds=3600)

#: Comprobaciones del paso 1 (donde se revela si un correo ya existe). Mas
#: holgado porque el usuario legitimo corrige el formulario varias veces, pero
#: acotado para que enumerar correos salga caro.
SIGNUP_STEP_RATE = RateLimit(limit=20, window_seconds=3600)

#: Alta de empleados por usuario. Un dueno no da de alta 30 cajeros por hora.
EMPLOYEE_CREATE_RATE = RateLimit(limit=30, window_seconds=3600)


def client_ip(request: HttpRequest) -> str:
    """IP real del cliente detras del proxy inverso.

    Se toma el primer valor de X-Forwarded-For porque nuestro Caddy la
    reescribe. Sin un proxy de confianza delante, esta cabecera se puede
    falsificar y el limite dejaria de servir.
    """
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR", "desconocida")


def check(scope: str, identity: str, rate: RateLimit) -> bool:
    """Consume un intento. Devuelve ``True`` si todavia queda cupo.

    ``cache.add`` solo escribe si la clave no existia, y devuelve si escribio.
    Esa combinacion es la que hace atomica la creacion de la ventana: quien
    gana la carrera fija el vencimiento y el resto incrementa.
    """
    key = f"ratelimit:{scope}:{identity}"

    if cache.add(key, 1, timeout=rate.window_seconds):
        return True

    try:
        used = cache.incr(key)
    except ValueError:
        # La clave vencio entre el add y el incr. Se vuelve a abrir la ventana.
        cache.set(key, 1, timeout=rate.window_seconds)
        return True

    return used <= rate.limit


def reset(scope: str, identity: str) -> None:
    """Borra el contador. Se usa tras un alta correcta, para no castigar a
    quien acaba de completar el flujo con exito."""
    cache.delete(f"ratelimit:{scope}:{identity}")
