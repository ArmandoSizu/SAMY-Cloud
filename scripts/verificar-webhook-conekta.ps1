# =============================================================================
# Paso 4: probar que la llave que quedo en .env sirve para verificar firmas.
# =============================================================================
#
# Que la variable exista no prueba nada. Que su contenido "se parezca" al PEM
# tampoco: si los saltos de linea se perdieron al pasar por .env, la cadena
# puede seguir conteniendo los mismos caracteres y aun asi no cargar como
# llave RSA. Aqui se carga de verdad y se verifica una firma real.
# =============================================================================

$ErrorActionPreference = "Continue"
$raiz = Split-Path -Parent $PSScriptRoot
Set-Location $raiz

$py = @'
import os, django
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")
django.setup()

from django.conf import settings
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
import base64, hashlib

pem = settings.CONEKTA_WEBHOOK_PUBLIC_KEY

print("1. Forma del valor tras pasar por .env y el parser de settings")
print(f"   longitud            = {len(pem)}")
print(f"   lineas reales       = {len(pem.splitlines())}   (un PEM RSA-2048 tiene 9)")
print(f"   empieza con BEGIN   = {pem.startswith('-----BEGIN PUBLIC KEY-----')}")
print(f"   termina con END     = {pem.strip().endswith('-----END PUBLIC KEY-----')}")
print(f"   quedan \\n literales = {'\\\\n' in pem}   (debe ser False)")
print()

print("2. Carga como llave RSA de verdad")
try:
    llave = serialization.load_pem_public_key(pem.encode())
except Exception as exc:
    print(f"   FALLA: {type(exc).__name__}: {exc}")
    raise SystemExit(1)
print(f"   OK. RSA de {llave.key_size} bits")

# Huella publica: identifica la llave sin revelar nada sensible (es publica).
der = llave.public_bytes(
    encoding=serialization.Encoding.DER,
    format=serialization.PublicFormat.SubjectPublicKeyInfo,
)
print(f"   huella SHA-256 = {hashlib.sha256(der).hexdigest()[:32]}...")
print()

print("3. El adaptador la usa para rechazar una firma invalida")
from apps.providers.registry import get_provider
from samy_common.providers.exceptions import ProviderPermanentError

proveedor = get_provider("conekta")
cuerpo = b'{"id":"evt_falso","type":"order.paid"}'
firma_basura = base64.b64encode(b"x" * 256).decode()

try:
    proveedor.verify_webhook(payload=cuerpo, headers={"digest": firma_basura})
except ProviderPermanentError as exc:
    print(f"   OK, rechazado: {exc.message}")
else:
    print("   FALLA GRAVE: acepto un webhook con firma invalida.")
    raise SystemExit(1)

print()
print("4. Un webhook sin cabecera de firma tampoco pasa")
try:
    proveedor.verify_webhook(payload=cuerpo, headers={})
except ProviderPermanentError as exc:
    print(f"   OK, rechazado: {exc.message}")
else:
    print("   FALLA GRAVE: acepto un webhook sin firma.")
    raise SystemExit(1)

print()
print("TODO CORRECTO: la llave viajo intacta por .env y el adaptador la usa.")
'@

$py | docker compose exec -T payments python -
