# =============================================================================
# Comprueba si las integraciones estan REALMENTE conectadas.
# =============================================================================
#
# No mira si las variables de entorno estan llenas: hace una llamada de verdad
# a cada proveedor. Una variable con una llave caducada esta "llena" y no
# sirve; lo unico que cuenta es que el proveedor conteste.
#
# Uso:   .\scripts\verificar-proveedores.ps1
# =============================================================================

$ErrorActionPreference = "Continue"
Set-Location (Join-Path ([Environment]::GetFolderPath("MyDocuments")) "SAMY Cloud")

function Mostrar-Proveedores($servicio, $etiqueta) {
  Write-Output ""
  Write-Output "=============================================================="
  Write-Output "  $etiqueta"
  Write-Output "=============================================================="

  $py = @'
import json, os, django
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")
django.setup()
from apps.providers.registry import describe_all
for p in describe_all():
    estado = p.get("status", "?")
    marca = {"READY": "[OK]      ", "NOT_CONFIGURED": "[FALTA]   ",
             "PENDING_CONTRACT": "[CONTRATO]", "DEGRADED": "[DEGRADADO]",
             "ERROR": "[ERROR]   "}.get(estado, "[" + estado + "]")
    print(f"{marca} {p.get('display_name','?'):<22} modo={p.get('mode','')}")
    detalle = (p.get("detail") or "").strip()
    if detalle:
        print(f"           {detalle[:150]}")
    for falta in p.get("missing_requirements") or []:
        print(f"           falta: {falta}")
    print()
'@
  $py | docker compose exec -T $servicio python -
}

Mostrar-Proveedores "payments" "PAGOS"
Mostrar-Proveedores "topups"   "RECARGAS"
Mostrar-Proveedores "billpay"  "PAGO DE SERVICIOS"

Write-Output ""
Write-Output "=============================================================="
Write-Output "  CATALOGO DE RECARGAS"
Write-Output "=============================================================="
$cat = @'
import os, django
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")
django.setup()
from apps.catalog.models import CatalogSyncRun, Operator, TopupProduct
print(f"  Operadores activos: {Operator.objects.filter(is_active=True).count()}")
print(f"  Productos activos:  {TopupProduct.objects.filter(is_active=True).count()}")
ultima = CatalogSyncRun.objects.order_by("-started_at").first()
if ultima is None:
    print("  Nunca se ha sincronizado el catalogo.")
    print("  Cuando Reloadly tenga credenciales, ejecuta:")
    print("    docker compose exec topups python manage.py sync_catalog")
else:
    estado = "OK" if ultima.succeeded else "FALLIDA"
    print(f"  Ultima sincronizacion: {ultima.started_at:%Y-%m-%d %H:%M} [{estado}]")
    if ultima.error_message:
        print(f"  Motivo del fallo: {ultima.error_message[:200]}")
'@
$cat | docker compose exec -T topups python -
Write-Output ""
