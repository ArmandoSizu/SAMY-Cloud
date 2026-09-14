# =============================================================================
# SAMY Cloud - Imagen unica para Cloud Run
# =============================================================================
#
# Los cuatro servicios Django, Caddy, Redis y los workers de Celery en una sola
# imagen. Es una imagen de DESPLIEGUE temporal, no la arquitectura: existe
# porque Cloud Run corre un contenedor por servicio y sin red interna, y hoy
# hace falta una URL publica, no cuatro servicios.
#
# infra/docker/django.Dockerfile sigue siendo la imagen correcta de cada
# servicio por separado y NO se toca. Cuando Cloud Run deje de ser el destino
# -o cuando toque separarlos en cuatro servicios- se borra este archivo y el
# otro sigue sirviendo.
#
# Se construye desde la RAIZ del repositorio:
#   docker build -f infra/docker/cloudrun.Dockerfile -t samy-cloud .
# =============================================================================

ARG PYTHON_VERSION=3.12

# -----------------------------------------------------------------------------
# Etapa 1: dependencias de Python
# -----------------------------------------------------------------------------
# Un solo venv con los requisitos de los cuatro servicios. Son cuatro servicios
# Django del mismo proyecto y sus dependencias coinciden casi por completo; pip
# resuelve el conjunto una vez y si hubiera un conflicto real, falla AQUI, al
# construir, y no a medianoche en produccion.
FROM python:${PYTHON_VERSION}-slim-bookworm AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update && apt-get install --no-install-recommends -y \
        build-essential \
        libpq-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

COPY libs/samy_common /libs/samy_common
COPY core/requirements.txt            /build/req-core.txt
COPY services/payments/requirements.txt /build/req-payments.txt
COPY services/topups/requirements.txt   /build/req-topups.txt
COPY services/billpay/requirements.txt  /build/req-billpay.txt

RUN python -m venv /venv
ENV PATH="/venv/bin:$PATH"

RUN pip install --upgrade pip setuptools wheel \
    && pip install \
        -r /build/req-core.txt \
        -r /build/req-payments.txt \
        -r /build/req-topups.txt \
        -r /build/req-billpay.txt \
    && pip install -e /libs/samy_common

# -----------------------------------------------------------------------------
# Etapa 2: CSS de Tailwind
# -----------------------------------------------------------------------------
# El CSS se compila al construir, no al arrancar: el contenedor de Cloud Run no
# debe necesitar Node ni npm para levantarse, y una instancia nueva no puede
# quedarse esperando a que Tailwind termine.
FROM node:22-slim AS css

WORKDIR /css
COPY core/package.json core/package-lock.json ./
RUN npm ci --no-audit --no-fund

# Hacen falta las tres carpetas: assets tiene la hoja de entrada, y templates y
# apps son donde Tailwind v4 BUSCA las clases usadas. Sin ellas compila un CSS
# valido y practicamente vacio, y la aplicacion sale sin estilos.
COPY core/assets    ./assets
COPY core/templates ./templates
COPY core/apps      ./apps
COPY core/tools     ./tools
COPY core/static    ./static

# `npm run build` = vendor + build:css, y hacen falta los dos pasos.
#
# El de vendor copia a static/vendor/ htmx, la fuente Inter y el lector de
# codigos. Compilar solo el CSS dejaba la interfaz sin htmx, que es lo que
# mueve el formulario de efectivo y el estado en vivo del comprobante: la
# pantalla se veia bien y no respondia.
RUN npm run build

# -----------------------------------------------------------------------------
# Etapa 3: imagen de ejecucion
# -----------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONFAULTHANDLER=1 \
    PATH="/venv/bin:$PATH"

# libpq5 para psycopg, redis-server para cache y cola, curl para la espera del
# arranque, ca-certificates para hablar HTTPS con Linntae y Conekta.
RUN apt-get update && apt-get install --no-install-recommends -y \
        libpq5 \
        redis-server \
        curl \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Caddy se copia de su imagen oficial en vez de instalarlo por apt: es un
# binario estatico y asi la version queda fijada en el Dockerfile.
COPY --from=caddy:2-alpine /usr/bin/caddy /usr/bin/caddy

COPY --from=builder /venv /venv
COPY --from=builder /libs/samy_common /libs/samy_common

# Los cuatro servicios, cada uno en su carpeta. El entrypoint hace `cd` a la
# que corresponde antes de cada manage.py, asi que los `config.settings` de
# cada servicio no se pisan.
WORKDIR /app
COPY core/              /app/core/
COPY services/payments/ /app/payments/
COPY services/topups/   /app/topups/
COPY services/billpay/  /app/billpay/

# El static del Core ya construido: reemplaza al que vino en COPY core/ e
# incluye el CSS compilado y static/vendor/. Va DESPUES del COPY del Core a
# proposito, para que gane esta version.
COPY --from=css /css/static/ /app/core/static/

COPY infra/cloudrun/Caddyfile     /etc/caddy/Caddyfile
COPY infra/cloudrun/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

# Usuario sin privilegios. staticfiles/ y /var/lib/redis tienen que ser suyos
# porque collectstatic y redis escriben ahi al arrancar.
RUN groupadd --gid 1000 samy \
    && useradd --uid 1000 --gid samy --create-home --shell /bin/bash samy \
    && mkdir -p /app/core/staticfiles /var/lib/redis /data \
    && chown -R samy:samy /app /venv /var/lib/redis /data

USER samy

# Informativo: Cloud Run inyecta $PORT y el Caddyfile lo lee de ahi. Fijarlo
# en el Dockerfile no sirve de nada, por eso no se fija.
EXPOSE 8080

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
