# ============================================================
# start.ps1 — Mindgate Policy Bot launcher
# Starts Postgres (via Docker) + FastAPI server in one go.
# ============================================================

$ErrorActionPreference = "Continue"

Write-Host ""
Write-Host "  ┌─────────────────────────────────────────┐" -ForegroundColor Cyan
Write-Host "  │        Mindgate Policy Bot v2.0          │" -ForegroundColor Cyan
Write-Host "  │  PostgreSQL + Auth + Chat Memory         │" -ForegroundColor Cyan
Write-Host "  └─────────────────────────────────────────┘" -ForegroundColor Cyan
Write-Host ""

# ── Step 1: Start Docker Desktop if not running ───────────────────────────
Write-Host "[1/3] Checking Docker..." -ForegroundColor Yellow

$dockerRunning = docker ps 2>&1 | Select-String -Pattern "CONTAINER"
if (-not $dockerRunning) {
    Write-Host "      Docker daemon not running. Launching Docker Desktop..." -ForegroundColor DarkYellow
    Start-Process "C:\Program Files\Docker\Docker\Docker Desktop.exe" -ErrorAction SilentlyContinue

    Write-Host "      Waiting for Docker to become ready (up to 60s)..." -ForegroundColor DarkYellow
    $maxWait = 60
    $waited  = 0
    while ($waited -lt $maxWait) {
        Start-Sleep -Seconds 3
        $waited += 3
        $status = docker ps 2>&1
        if ($status -notmatch "error|failed|cannot|refused") {
            Write-Host "      Docker is ready!" -ForegroundColor Green
            break
        }
        Write-Host "      Still waiting... ($waited/$maxWait s)" -ForegroundColor DarkGray
    }
} else {
    Write-Host "      Docker is already running." -ForegroundColor Green
}

# ── Step 2: Start PostgreSQL container ────────────────────────────────────
Write-Host ""
Write-Host "[2/3] Starting PostgreSQL (pgvector)..." -ForegroundColor Yellow

$dockerOut = & docker compose up -d 2>&1
$dockerExit = $LASTEXITCODE
$dockerOut | ForEach-Object { Write-Host $_ }
if ($dockerExit -ne 0) {
    Write-Host "      ERROR: Failed to start Postgres container." -ForegroundColor Red
    Write-Host "      Make sure Docker Desktop is running and try again." -ForegroundColor Red
    exit 1
}
Write-Host "      PostgreSQL container started." -ForegroundColor Green


# Wait for Postgres to accept connections
Write-Host "      Waiting for Postgres to be ready..." -ForegroundColor DarkYellow
$maxWait = 30
$waited  = 0
while ($waited -lt $maxWait) {
    Start-Sleep -Seconds 2
    $waited += 2
    $ready = docker exec mindgate_postgres pg_isready -U postgres 2>&1
    if ($ready -match "accepting connections") {
        Write-Host "      PostgreSQL is ready on port 5432!" -ForegroundColor Green
        break
    }
    Write-Host "      Still waiting... ($waited/$maxWait s)" -ForegroundColor DarkGray
}

# ── Step 3: Start FastAPI server ──────────────────────────────────────────
Write-Host ""
Write-Host "[3/3] Starting Mindgate Policy Bot API..." -ForegroundColor Yellow
Write-Host ""
Write-Host "  ✓ App will be at: http://localhost:8000" -ForegroundColor Cyan
Write-Host "  ✓ API docs at:    http://localhost:8000/docs" -ForegroundColor Cyan
Write-Host ""
Write-Host "  Press Ctrl+C to stop." -ForegroundColor DarkGray
Write-Host ""

if (Test-Path ".\.venv\Scripts\python.exe") {
    & ".\.venv\Scripts\python.exe" -m uvicorn main:app --host 0.0.0.0 --port 8000
} else {
    python -m uvicorn main:app --host 0.0.0.0 --port 8000
}
