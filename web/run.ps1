param(
  [int]$Port = 8000
)
$ErrorActionPreference = 'Stop'

# Activate venv if exists
if (Test-Path "..\.venv\Scripts\Activate.ps1") {
  . "..\.venv\Scripts\Activate.ps1"
}

# Ensure requirements installed
$req = Join-Path (Split-Path $PSCommandPath) "..\requirements-web.txt"
if (Test-Path $req) {
  Write-Host "Instalando dependencias web..."
  pip install -r $req | Out-Null
}

# Create downloads dir
$downloads = Join-Path (Split-Path $PSCommandPath) "..\downloads"
New-Item -ItemType Directory -Path $downloads -Force | Out-Null

# Launch server
Write-Host "Iniciando servidor en http://127.0.0.1:$Port ..."
$env:PYTHONUNBUFFERED = "1"
python -m uvicorn main:app --host 127.0.0.1 --port $Port --reload
