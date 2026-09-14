#!/usr/bin/env bash
# =============================================================================
# SAMY Cloud - Arranque del contenedor unico de Cloud Run
# =============================================================================
#
# Levanta, en este orden: Redis, migraciones, estaticos, los cuatro gunicorn,
# los workers de Celery y Caddy.
#
# POR QUE UN SOLO CONTENEDOR
# --------------------------
# Es temporal y es una decision de despliegue, no de arquitectura. Cloud Run
# corre un contenedor por servicio y no ofrece red interna entre servicios;
# repartir los cuatro Django en cuatro servicios de Cloud Run exige tambien
# Memorystore para Redis y resolver la autenticacion entre ellos. Para tener la
# URL publica hoy, todo va junto y se hablan por 127.0.0.1.
#
# Lo que NO se movio: el codigo de los cuatro servicios, sus bases de datos
# separadas, ni la firma S2S entre ellos. Separarlos despues toca este archivo
# y el Caddyfile, no la aplicacion.
#
# SOBRE LOS DATOS
# ---------------
# Nada importante vive en el contenedor. PostgreSQL es Cloud SQL, externo. El
# Redis de aqui es cache y broker de Celery: si el contenedor se reinicia se
# pierde, y no pasa nada, porque el estado que impide una doble recarga vive en
# PostgreSQL (estado del cumplimiento + select_for_update), no en Redis.
#
# SI UN PROCESO MUERE, MUERE EL CONTENEDOR
# ----------------------------------------
# El ``wait -n`` del final es deliberado. Un contenedor al que se le murio
# gunicorn pero sigue contestando la salud de Caddy es lo peor de los dos
# mundos: Cloud Run lo cree sano y le manda trafico que falla. Mejor caer y
# que Cloud Run arranque otro.
# =============================================================================

set -euo pipefail

export PORT="${PORT:-8080}"
export PYTHONUNBUFFERED=1

log() { echo "[entrypoint] $*"; }

# -----------------------------------------------------------------------------
# 1. Redis local
# -----------------------------------------------------------------------------
# Solo en 127.0.0.1 y sin persistencia: es cache y cola, no un almacen. El
# --save '' evita que escriba dumps al disco efimero del contenedor.
log "iniciando redis"
redis-server \
	--bind 127.0.0.1 \
	--port 6379 \
	--save '' \
	--appendonly no \
	--daemonize yes \
	--maxmemory 256mb \
	--maxmemory-policy allkeys-lru

for _ in $(seq 1 30); do
	if redis-cli -h 127.0.0.1 ping >/dev/null 2>&1; then break; fi
	sleep 0.5
done
redis-cli -h 127.0.0.1 ping >/dev/null || { log "redis no respondio"; exit 1; }

# -----------------------------------------------------------------------------
# 2. Migraciones
# -----------------------------------------------------------------------------
# Se corren al arrancar. Con --min-instances=1 esto ocurre una vez por
# despliegue; si algun dia hay varias instancias, Django toma el bloqueo de
# migraciones de PostgreSQL y las demas esperan, no se duplican.
#
# Si una migracion falla, el contenedor no arranca. Es lo correcto: arrancar
# con el esquema a medias corrompe datos de forma silenciosa.
for servicio in core payments topups billpay; do
	log "migrando ${servicio}"
	(cd "/app/${servicio}" && python manage.py migrate --noinput)
done

# -----------------------------------------------------------------------------
# 3. Estaticos
# -----------------------------------------------------------------------------
# Solo el Core tiene interfaz. Los sirve WhiteNoise desde STATIC_ROOT, no
# Caddy: produccion usa CompressedManifestStaticFilesStorage, que escribe un
# manifiesto con los nombres hasheados. Si Caddy sirviera la carpeta por su
# cuenta habria dos fuentes de verdad para el mismo archivo.
#
# Sin --clear: borrar y recolectar en cada arranque no aporta nada aqui, porque
# la imagen es inmutable y el contenido no cambia entre instancias.
log "recolectando estaticos"
(cd /app/core && python manage.py collectstatic --noinput >/dev/null)

# -----------------------------------------------------------------------------
# 4. Los cuatro servicios Django
# -----------------------------------------------------------------------------
# Puertos en localhost. Solo el 8001 (Core) recibe trafico de Caddy; los otros
# tres son alcanzables unicamente desde dentro del contenedor.
#
#   8001 core      8002 payments      8003 topups      8004 billpay
#
# --workers 2 y no 3: Cloud Run da 1 vCPU por omision y aqui conviven cuatro
# gunicorn, dos workers de Celery, Redis y Caddy. Mas procesos con la misma
# CPU no sirven mas peticiones, solo suben la latencia y la memoria.
arrancar_django() {
	local servicio="$1" puerto="$2"
	log "iniciando ${servicio} en ${puerto}"
	(
		cd "/app/${servicio}" || exit 1
		exec gunicorn config.wsgi:application \
			--bind "127.0.0.1:${puerto}" \
			--workers 2 \
			--worker-class gthread \
			--threads 4 \
			--timeout 60 \
			--graceful-timeout 20 \
			--keep-alive 5 \
			--access-logfile - \
			--error-logfile - \
			--name "samy-${servicio}"
	) &
}

arrancar_django core 8001
arrancar_django payments 8002
arrancar_django topups 8003
arrancar_django billpay 8004

# -----------------------------------------------------------------------------
# 5. Workers de Celery
# -----------------------------------------------------------------------------
# Siguen existiendo porque el proyecto los usa para el outbox y la
# conciliacion. Pero la recarga confirmada por una persona ya NO depende de
# ellos: se ejecuta dentro de la peticion, por
# POST /api/v1/topups/<id>/ejecutar/. Si un worker desaparece en un reinicio de
# Cloud Run, no deja a nadie pagado y sin servicio.
#
# --concurrency 1: son tareas de coordinacion, no de calculo.
arrancar_worker() {
	local servicio="$1"
	log "iniciando worker de ${servicio}"
	(
		cd "/app/${servicio}" || exit 1
		exec celery -A config worker \
			--loglevel=INFO \
			--concurrency=1 \
			--without-gossip --without-mingle --without-heartbeat
	) &
}

arrancar_worker payments
arrancar_worker topups

# -----------------------------------------------------------------------------
# 6. Espera a que el Core conteste antes de abrir la puerta
# -----------------------------------------------------------------------------
# Caddy arranca al final. Abrir el puerto publico antes de que el Core
# responda haria que las primeras peticiones dieran 502.
log "esperando al core"
for _ in $(seq 1 60); do
	if curl -fsS http://127.0.0.1:8001/health/ >/dev/null 2>&1; then
		log "core listo"
		break
	fi
	sleep 1
done

# -----------------------------------------------------------------------------
# 7. Caddy en $PORT
# -----------------------------------------------------------------------------
log "iniciando caddy en ${PORT}"
caddy run --config /etc/caddy/Caddyfile --adapter caddyfile &

# El primero que muera tumba el contenedor. Ver la nota de arriba.
wait -n
log "un proceso termino; el contenedor se detiene para que Cloud Run lo reemplace"
exit 1
