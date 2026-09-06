# Cierra el tunel publico y borra la URL guardada.
# La ruta /webhooks/conekta/ del proxy sigue existiendo, pero deja de ser
# alcanzable desde internet: sin tunel no hay puerta de entrada.
$raiz = Split-Path -Parent $PSScriptRoot
docker rm -f samy-tunel 2>&1 | Out-Null
Remove-Item (Join-Path $raiz ".tunel-url") -Force -ErrorAction SilentlyContinue
Write-Host "Tunel cerrado. Recuerda borrar o actualizar el webhook en Conekta:"
Write-Host "  la URL registrada dejara de responder."
