# =============================================================================
# Imagen comun para los cuatro servicios Django de SAMY Cloud.
# =============================================================================
#
# Una sola definicion parametrizada por SERVICE_PATH en vez de cuatro
# Dockerfiles casi identicos: cualquier mejora de seguridad o de tamano se
# aplica a todos los servicios a la vez, y no hay riesgo de que uno se quede
# atras con una version vieja de Python o sin el usuario sin privilegios.
#
# Build multi-etapa:
#   builder -> compila dependencias (necesita compilador de C para psycopg y
#              argon2-cffi)
#   runtime -> solo lo necesario para ejecutar. Sin compilador, sin cabeceras
#              de desarrollo. Menos superficie de ataque y ~400 MB menos.
# =============================================================================

ARG PYTHON_VERSION=3.12

# -----------------------------------------------------------------------------
# Etapa 1: construccion de dependencias
# -----------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim-bookworm AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update && apt-get install --no-install-recommends -y \
        build-essential \
        libpq-dev \
    && rm -rf /var/lib/apt/lists/*

ARG SERVICE_PATH
WORKDIR /build

# La libreria compartida se instala primero y en modo editable: asi un cambio
# en samy_common no obliga a reconstruir la capa de dependencias.
COPY libs/samy_common /libs/samy_common
COPY ${SERVICE_PATH}/requirements.txt /build/requirements.txt

RUN python -m venv /venv
ENV PATH="/venv/bin:$PATH"

RUN pip install --upgrade pip setuptools wheel \
    && pip install -r /build/requirements.txt \
    && pip install -e /libs/samy_common

# -----------------------------------------------------------------------------
# Etapa 2: imagen de ejecucion
# -----------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONFAULTHANDLER=1 \
    PATH="/venv/bin:$PATH"

# libpq5 es la biblioteca de cliente en tiempo de ejecucion; libpq-dev (las
# cabeceras) se quedan en la etapa de construccion.
RUN apt-get update && apt-get install --no-install-recommends -y \
        libpq5 \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Usuario sin privilegios. Un proceso Django comprometido corriendo como root
# dentro del contenedor es un problema mucho mayor que uno corriendo como
# usuario normal.
RUN groupadd --gid 1000 samy \
    && useradd --uid 1000 --gid samy --create-home --shell /bin/bash samy

COPY --from=builder /venv /venv
COPY --from=builder /libs/samy_common /libs/samy_common

ARG SERVICE_PATH
WORKDIR /app
COPY --chown=samy:samy ${SERVICE_PATH}/ /app/

RUN mkdir -p /app/staticfiles /app/media && chown -R samy:samy /app /venv

USER samy

EXPOSE 8000

HEALTHCHECK --interval=20s --timeout=5s --start-period=40s --retries=3 \
    CMD curl -fsS http://localhost:8000/health/ || exit 1

# Gunicorn por defecto. docker-compose lo sobreescribe con runserver en
# desarrollo, pero la imagen es apta para produccion tal cual.
#
# --workers 3: regla practica (2 x nucleos) + 1, ajustable por entorno.
# --timeout 30: un worker bloqueado mas de 30 s se reinicia; sin esto, un
#   proveedor lento agota el pool de workers y tumba el servicio.
# --access-logfile -: los logs van a stdout, que es donde el orquestador los
#   recoge. Escribir a archivo dentro de un contenedor es un antipatron.
CMD ["gunicorn", "config.wsgi:application", \
     "--bind", "0.0.0.0:8000", \
     "--workers", "3", \
     "--worker-class", "gthread", \
     "--threads", "4", \
     "--timeout", "30", \
     "--graceful-timeout", "20", \
     "--keep-alive", "5", \
     "--max-requests", "1000", \
     "--max-requests-jitter", "100", \
     "--access-logfile", "-", \
     "--error-logfile", "-"]
