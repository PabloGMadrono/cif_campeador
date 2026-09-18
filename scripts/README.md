# Development scripts

Run these commands from the repository root in PowerShell.

## Reset development data

Stop FastAPI and both workers first, then run:

```powershell
.\scripts\reset-development.ps1
```

The script requires typing `RESET`. For non-interactive use:

```powershell
.\scripts\reset-development.ps1 -Force
```

It deletes and recreates the SQLite database and clears the four application
Redis streams. Downloaded WhatsApp media is preserved.

## Start the application

```powershell
.\scripts\start-development.ps1
```

The script starts Redis, applies Alembic migrations, and opens separate visible
PowerShell terminals for FastAPI, the download worker, the OCR worker, and ngrok.

If ngrok is already running separately:

```powershell
.\scripts\start-development.ps1 -SkipNgrok
```

To install or update Python dependencies before startup:

```powershell
.\scripts\start-development.ps1 -InstallDependencies
```

Prerequisites:

- Docker Desktop must be running.
- `.venv` must already exist.
- `.env` must contain the WhatsApp and selected OCR credentials.
- ngrok must be available on `PATH` unless `-SkipNgrok` is used.
