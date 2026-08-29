# Nepal Flood Watch - start the console.
#   .\run.ps1            start the server on http://127.0.0.1:8000
#   .\run.ps1 -Check     run the deployment preflight and exit
#   .\run.ps1 -Tiles     warm the offline map cache for Nepal, then exit
param([switch]$Check, [switch]$Tiles)

$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$py = Join-Path $root '.venv\Scripts\python.exe'

if (-not (Test-Path $py)) {
    Write-Host 'Creating virtual environment...'
    python -m venv (Join-Path $root '.venv')
    & $py -m pip install --upgrade pip
    & $py -m pip install -r (Join-Path $root 'backend\requirements.txt')
}

Set-Location (Join-Path $root 'backend')

if ($Check) { & $py -m app.preflight; exit $LASTEXITCODE }
if ($Tiles) { & $py -c "import asyncio;from app import tiles;print(asyncio.run(tiles.prefetch('dark')))"; exit }

Write-Host 'Nepal Flood Watch -> http://127.0.0.1:8000'
& $py -m uvicorn app.main:app --host 127.0.0.1 --port 8000
