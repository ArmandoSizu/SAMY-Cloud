$raiz = Split-Path -Parent $PSScriptRoot
Set-Location $raiz

$gi = Get-Content .gitignore -Raw
if ($gi -notmatch '\.tunel-url') {
    Add-Content .gitignore ''
    Add-Content .gitignore '# URL efimera del tunel de webhooks: cambia en cada arranque.'
    Add-Content .gitignore '.tunel-url'
}

git add -A
git commit -F .git/COMMIT_MSG.txt --quiet
git log --oneline -1
Write-Host "--- ARCHIVOS ---"
git show --stat --oneline HEAD | Select-Object -Last 12
Write-Host "--- ESTADO (vacio = limpio) ---"
git status --short
Write-Host "--- .env rastreado? (vacio = correcto) ---"
git ls-files .env
