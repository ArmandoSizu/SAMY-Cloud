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

create_service_db "samy_core"     "samy_core"     "${CORE_DB_PASSWORD:-samy_core_dev}"
create_service_db "samy_payments" "samy_payments" "${PAYMENTS_DB_PASSWORD:-samy_payments_dev}"
create_service_db "samy_topups"   "samy_topups"   "${TOPUPS_DB_PASSWORD:-samy_topups_dev}"
create_service_db "samy_billpay"  "samy_billpay"  "${BILLPAY_DB_PASSWORD:-samy_billpay_dev}"

echo "== Bases de datos listas =="
