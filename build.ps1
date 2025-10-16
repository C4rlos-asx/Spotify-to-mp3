# Build script for packaging spotify_to_mp3.py as a Windows executable using PyInstaller
# Usage:
#   powershell -ExecutionPolicy Bypass -File .\build.ps1

param(
    [switch]$OneFile = $true,
    [string]$IconPng = ".\assets\icon.png"
)

$ErrorActionPreference = 'Stop'

# 1) Ensure venv is active
if (Test-Path .\.venv\Scripts\Activate.ps1) {
    . .\.venv\Scripts\Activate.ps1
}

# 2) Verify pyinstaller is installed
python -m pip show pyinstaller > $null 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Host "Installing PyInstaller..."
    python -m pip install pyinstaller
}

# 3) Clean previous builds
if (Test-Path .\build) { Remove-Item .\build -Recurse -Force }
if (Test-Path .\dist) { Remove-Item .\dist -Recurse -Force }
if (Test-Path .\SpotifyToMP3.spec) { Remove-Item .\SpotifyToMP3.spec -Force }

# 4) Common PyInstaller options
$common = @(
    '--name', 'SpotifyToMP3',
    '--clean',
    '--log-level=WARN',
    '--collect-submodules', 'yt_dlp',
    '--collect-submodules', 'imagehash',
    '--collect-submodules', 'PIL',
    '--collect-submodules', 'mutagen',
    '--collect-data', 'yt_dlp',
    '--collect-data', 'PIL',
    '--collect-data', 'imagehash'
)

if ($OneFile) { $common += '--onefile' } else { $common += '--onedir' }

# Add .env if present (not required, but convenient to ship)
if (Test-Path .\.env) {
    $common += @('--add-data', ".\.env;.")
}

# 5) Prepare icon
$iconIco = $null
if (Test-Path $IconPng) {
    if (!(Test-Path .\assets)) { New-Item -ItemType Directory -Path .\assets | Out-Null }
    $iconIco = ".\assets\icon.ico"
    # Convert PNG to ICO using Python + Pillow (installed indirectly via imagehash/PIL)
    $py = @"
from PIL import Image
import sys
png, ico = sys.argv[1], sys.argv[2]
img = Image.open(png).convert("RGBA")
sizes = [(16,16),(32,32),(48,48),(64,64),(128,128),(256,256)]
img.save(ico, sizes=sizes)
"@
    $tmp = Join-Path $env:TEMP "png2ico.py"
    Set-Content -Path $tmp -Value $py -Encoding UTF8
    python $tmp $IconPng $iconIco
    if ($LASTEXITCODE -ne 0) { Write-Warning "No se pudo convertir PNG a ICO. Se compilará sin icono."; $iconIco = $null }
}

# 6) Choose entry script (CLI only)
$entry = 'spotify_to_mp3.py'
$specName = 'SpotifyToMP3'

# 7) Add icon if available
if ($iconIco) {
    $common = $common + @('--icon', $iconIco)
}

# 8) Run PyInstaller
Write-Host "Building $specName from $entry ..."
pyinstaller @common $entry

# 9) Post-build notes
Write-Host "\nBuild complete. Output in .\\dist\\$specName\n"
Write-Host "Notes:"
Write-Host " - ffmpeg no se empaqueta; debe estar en PATH en el equipo destino."
Write-Host " - Se ha deshabilitado la compilación GUI y C++."
