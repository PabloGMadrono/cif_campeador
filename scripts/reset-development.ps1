[CmdletBinding()]
param(
    [switch]$Force
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$repositoryRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$pythonExecutable = Join-Path $repositoryRoot ".venv\Scripts\python.exe"
$databaseDirectory = Join-Path $repositoryRoot "data"
$databasePath = Join-Path $databaseDirectory "cif_campeador.db"
$databaseFiles = @(
    $databasePath,
    "$databasePath-wal",
    "$databasePath-shm"
)
$redisKeys = @(
    "whatsapp:downloads",
    "whatsapp:ocr",
    "whatsapp:downloads:dead",
    "whatsapp:ocr:dead"
)

if (-not (Test-Path -LiteralPath $pythonExecutable -PathType Leaf)) {
    throw "Virtual-environment Python was not found at: $pythonExecutable"
}

if (-not $Force) {
    Write-Warning "This permanently deletes the development database and all invoice Redis jobs."
    Write-Warning "Stop FastAPI and both workers before continuing. Downloaded media files are preserved."
    $confirmation = Read-Host "Type RESET to continue"
    if ($confirmation -cne "RESET") {
        Write-Host "Reset cancelled."
        exit 0
    }
}

Push-Location $repositoryRoot
try {
    Write-Host "Starting Redis..."
    & docker compose up -d redis
    if ($LASTEXITCODE -ne 0) {
        throw "Redis could not be started."
    }

    Write-Host "Clearing application Redis streams..."
    & docker compose exec -T redis redis-cli DEL @redisKeys
    if ($LASTEXITCODE -ne 0) {
        throw "Redis streams could not be cleared."
    }

    Write-Host "Deleting the SQLite database..."
    foreach ($file in $databaseFiles) {
        Remove-Item -LiteralPath $file -Force -ErrorAction SilentlyContinue
    }

    New-Item -ItemType Directory -Path $databaseDirectory -Force | Out-Null

    Write-Host "Recreating the database schema..."
    & $pythonExecutable -m alembic upgrade head
    if ($LASTEXITCODE -ne 0) {
        throw "Alembic failed to recreate the database schema."
    }

    Write-Host "Development state reset successfully."
    Write-Host "Database: $databasePath"
}
finally {
    Pop-Location
}
