#!/bin/bash
# =============================================================================
# Crea una base de datos y un usuario POR MICROSERVICIO.
# =============================================================================
#
# Esta es la garantia tecnica de que los microservicios estan realmente
# desacoplados a nivel de datos: el usuario 'samy_topups' no tiene ningun
# privilegio sobre la base 'samy_payments'. Si alguien intentara hacer un JOIN
# entre servicios, la base de datos lo impide.
#
# Se ejecuta UNA sola vez, en el primer arranque del contenedor (cuando el
# volumen de datos esta vacio). Para volver a ejecutarlo hay que eliminar el
# volumen: docker compose down -v
# =============================================================================
set -euo pipefail

create_service_db() {
  local db="$1"
  local user="$2"
  local password="$3"

  echo "  -> creando base '${db}' y usuario '${user}'"

  psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres <<-SQL
    CREATE USER ${user} WITH PASSWORD '${password}';
    CREATE DATABASE ${db} OWNER ${user};
    -- Nadie mas que el dueno puede conectarse.
    REVOKE ALL ON DATABASE ${db} FROM PUBLIC;
    GRANT ALL PRIVILEGES ON DATABASE ${db} TO ${user};
SQL

  psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "${db}" <<-SQL
    -- El esquema public por defecto es escribible por todos en Postgres < 15;
    -- lo cerramos explicitamente para no depender de la version.
    REVOKE ALL ON SCHEMA public FROM PUBLIC;
    GRANT ALL ON SCHEMA public TO ${user};
    -- Extensiones que usan los servicios.
    CREATE EXTENSION IF NOT EXISTS "pgcrypto";
    CREATE EXTENSION IF NOT EXISTS "pg_trgm";
SQL
}

echo "== SAMY Cloud: inicializando bases de datos por servicio =="

require_password() {
  local name="$1"
  local value="$2"
  if [ -z "$value" ]; then
    echo "ERROR: falta la variable ${name}." >&2
    echo "  El servicio 'postgres' debe recibirla en docker-compose.yml." >&2
    echo "  Sin ella se crearia un usuario con una clave que no coincide con" >&2
    echo "  la cadena *_DATABASE_URL, y las migraciones fallarian con un" >&2
    echo "  'password authentication failed' dificil de diagnosticar." >&2
    exit 1
  fi
}

require_password "CORE_DB_PASSWORD"     "${CORE_DB_PASSWORD:-}"
require_password "PAYMENTS_DB_PASSWORD" "${PAYMENTS_DB_PASSWORD:-}"
require_password "TOPUPS_DB_PASSWORD"   "${TOPUPS_DB_PASSWORD:-}"
require_password "BILLPAY_DB_PASSWORD"  "${BILLPAY_DB_PASSWORD:-}"

create_service_db "samy_core"     "samy_core"     "${CORE_DB_PASSWORD}"
create_service_db "samy_payments" "samy_payments" "${PAYMENTS_DB_PASSWORD}"
create_service_db "samy_topups"   "samy_topups"   "${TOPUPS_DB_PASSWORD}"
create_service_db "samy_billpay"  "samy_billpay"  "${BILLPAY_DB_PASSWORD}"

echo "== Bases de datos listas =="
