#!/usr/bin/env python3
"""Genera los secretos criptograficos que SAMY Cloud necesita.

Uso:
    python scripts/generate-secrets.py

Pega la salida en tu archivo .env. NUNCA los subas al repositorio.
"""

import secrets


def main() -> None:
    print("# Pega esto en tu .env (y no lo compartas)")
    print()
    print(f"DJANGO_SECRET_KEY={secrets.token_urlsafe(64)}")
    print(f"SERVICE_S2S_SECRET={secrets.token_urlsafe(64)}")
    print(f"POSTGRES_SUPERUSER_PASSWORD={secrets.token_urlsafe(24)}")
    print(f"CORE_DB_PASSWORD={secrets.token_urlsafe(20)}")
    print(f"PAYMENTS_DB_PASSWORD={secrets.token_urlsafe(20)}")
    print(f"TOPUPS_DB_PASSWORD={secrets.token_urlsafe(20)}")
    print(f"BILLPAY_DB_PASSWORD={secrets.token_urlsafe(20)}")
    print()
    print("# Recuerda actualizar tambien las cadenas *_DATABASE_URL con estas claves.")


if __name__ == "__main__":
    main()
