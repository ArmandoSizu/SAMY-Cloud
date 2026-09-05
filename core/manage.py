#!/usr/bin/env python
"""Utilidad de linea de comandos de Django para el Core Platform."""

import os
import sys


def main() -> None:
    # Por defecto, desarrollo. En contenedor se fija DJANGO_SETTINGS_MODULE
    # explicitamente, de modo que produccion nunca dependa de este default.
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")
    try:
        from django.core.management import execute_from_command_line
    except ImportError as exc:
        raise ImportError(
            "No se pudo importar Django. Verifica que el entorno virtual este "
            "activado y que las dependencias esten instaladas."
        ) from exc
    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
