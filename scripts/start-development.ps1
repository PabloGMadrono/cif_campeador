[CmdletBinding()]
param(
    [switch]$InstallDependencies,
    [switch]$SkipNgrok
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$repositoryRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$pythonExecutable = Join-Path $repositoryRoot ".venv\Scripts\python.exe"
$environmentFile = Join-Path $repositoryRoot ".env"
$powerShellExecutable = (Get-Process -Id $PID).Path

function Assert-LastCommandSucceeded {
    param([Parameter(Mandatory)][string]$Message)

    if ($LASTEXITCODE -ne 0) {
        throw $Message
    }
}

function Start-DevelopmentProcess {
    param(
        [Parameter(Mandatory)][string]$Title,
        [Parameter(Mandatory)][string]$Executable,
        [Parameter(Mandatory)][string[]]$Arguments
    )

    $escapedTitle = $Title.Replace("'", "''")
    $escapedRoot = $repositoryRoot.Replace("'", "''")
    $escapedExecutable = $Executable.Replace("'", "''")
    $escapedArguments = $Arguments | ForEach-Object {
        "'" + $_.Replace("'", "''") + "'"
    }
    $argumentText = $escapedArguments -join " "
    $command = (
        "`$host.UI.RawUI.WindowTitle = '$escapedTitle'; " +
        "Set-Location -LiteralPath '$escapedRoot'; " +
        "& '$escapedExecutable' $argumentText"
    )

    Start-Process `
        -FilePath $powerShellExecutable `
        -ArgumentList @("-NoExit", "-NoProfile", "-Command", $command) `
        -WorkingDirectory $repositoryRoot `
        -WindowStyle Normal | Out-Null
}

if (-not (Test-Path -LiteralPath $pythonExecutable -PathType Leaf)) {
    throw "Virtual-environment Python was not found at: $pythonExecutable"
}

if (-not (Test-Path -LiteralPath $environmentFile -PathType Leaf)) {
    throw "Create $environmentFile from .env.example and configure its secrets first."
}

$ngrokExecutable = $null
if (-not $SkipNgrok) {
    $ngrokCommand = Get-Command ngrok -ErrorAction SilentlyContinue
    if ($null -eq $ngrokCommand) {
        throw "ngrok is not installed or is not available on PATH. Use -SkipNgrok to start without it."
    }
    $ngrokExecutable = $ngrokCommand.Source
}

Push-Location $repositoryRoot
try {
    if ($InstallDependencies) {
        Write-Host "Installing Python dependencies..."
        & $pythonExecutable -m pip install -r requirements.txt
        Assert-LastCommandSucceeded "Python dependency installation failed."
    }

    Write-Host "Starting Redis..."
    & docker compose up -d redis
    Assert-LastCommandSucceeded "Redis could not be started."

    Write-Host "Waiting for Redis..."
    $redisReady = $false
    foreach ($attempt in 1..20) {
        $ping = & docker compose exec -T redis redis-cli ping 2>$null
        if ($LASTEXITCODE -eq 0 -and $ping -contains "PONG") {
            $redisReady = $true
            break
        }
        Start-Sleep -Milliseconds 500
    }
    if (-not $redisReady) {
        throw "Redis did not become ready within 10 seconds."
    }

    Write-Host "Applying database migrations..."
    & $pythonExecutable -m alembic upgrade head
    Assert-LastCommandSucceeded "Database migration failed."

    Write-Host "Opening FastAPI and worker terminals..."
    Start-DevelopmentProcess `
        -Title "CIF Campeador - FastAPI" `
        -Executable $pythonExecutable `
        -Arguments @("-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--reload")
    Start-DevelopmentProcess `
        -Title "CIF Campeador - Download worker" `
        -Executable $pythonExecutable `
        -Arguments @("-m", "src.workers.download")
    Start-DevelopmentProcess `
        -Title "CIF Campeador - OCR worker" `
        -Executable $pythonExecutable `
        -Arguments @("-m", "src.workers.ocr")

    if (-not $SkipNgrok) {
        Start-DevelopmentProcess `
            -Title "CIF Campeador - ngrok" `
            -Executable $ngrokExecutable `
            -Arguments @("http", "8000")
    }

    Write-Host "Development services started."
    Write-Host "FastAPI:     http://localhost:8000"
    Write-Host "Redis:       localhost:6379"
    Write-Host "RedisInsight: http://localhost:5540 (when its container is running)"
}
finally {
    Pop-Location
}
