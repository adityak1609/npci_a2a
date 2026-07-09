# ============================================================
# start_a2a.ps1 - Launch the platform on the GOOGLE A2A (ADK) transport
#
# Starts three processes wired together over Google A2A instead of the legacy
# custom /a2a/external JSON-RPC transport:
#
#   1. Bank portal          uvicorn main:app                  http://localhost:8000
#      (SPA + /a2a/internal; outbound Bank->NPCI routed over A2A to the mock)
#   2. Bank ADK A2A server   uvicorn a2a_adk_server:a2a_app    http://localhost:8002
#      (inbound NPCI->Bank; card at /.well-known/agent-card.json)
#   3. Mock NPCI ADK agent   uvicorn mock_npci_agent.adk_agent http://localhost:8001
#      (Google-A2A counterpart; card at /.well-known/agent-card.json)
#
# Push a change_communication from the mock once all three are up:
#   python -m mock_npci_agent.push_change_a2a
#
# Run from the project root:  .\start_a2a.ps1
# ============================================================

$ErrorActionPreference = "Continue"
$ProjectRoot = $PSScriptRoot
Set-Location $ProjectRoot

Write-Host ""
Write-Host "  +------------------------------------------------------+" -ForegroundColor Cyan
Write-Host "  |   NPCI A2A Partner Platform  -  GOOGLE A2A (ADK) mode |" -ForegroundColor Cyan
Write-Host "  +------------------------------------------------------+" -ForegroundColor Cyan
Write-Host ""

# -- Resolve Python (prefer venv) -------------------------------------------
$Python = "python"
$VenvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (Test-Path $VenvPython) {
    $Python = $VenvPython
    Write-Host "  Using venv python: $Python" -ForegroundColor Green
} else {
    Write-Warning "  .venv not found - using system 'python'. Activate/create the venv if imports fail."
}

# -- Preflight: ensure the ADK / a2a-sdk deps are installed ------------------
# The Google A2A path needs google-adk[a2a] + a2a-sdk (and the matching
# OpenTelemetry set) from requirements.txt. A venv created before the ADK work
# will not have them, so install on first run rather than crash three windows.
& $Python -c "import a2a, google.adk" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "  ADK / a2a-sdk not found - installing from requirements.txt..." -ForegroundColor Yellow
    & $Python -m pip install -r (Join-Path $ProjectRoot "requirements.txt")
    & $Python -c "import a2a, google.adk" 2>$null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "  ERROR: ADK deps still missing after install. Check pip output above." -ForegroundColor Red
        exit 1
    }
    Write-Host "  ADK deps installed." -ForegroundColor Green
} else {
    Write-Host "  ADK / a2a-sdk present." -ForegroundColor Green
}

# -- Google A2A wiring (process env -> inherited by the child windows) -------
# python-dotenv does not override existing env vars, and NPCI_TRANSPORT is not a
# DB-backed setting, so these process values win over .env for this run.
$env:NPCI_TRANSPORT            = "a2a"                                              # Bank->NPCI outbound over A2A
$env:NPCI_A2A_AGENT_CARD_URL   = "http://localhost:8001/.well-known/agent-card.json" # bank -> mock (8001)
$env:BANK_A2A_AGENT_CARD_URL   = "http://localhost:8002/.well-known/agent-card.json" # mock -> bank (8002)
$env:BANK_A2A_PORT             = "8002"
$env:BANK_A2A_HOST             = "localhost"
$env:MOCK_NPCI_PORT            = "8001"
# Local dev: transport auth (spec 4.1 L1-4) off so the bundled mock works
# without certs/HMAC. Set true (+ mTLS/HMAC/JWT config) for a production run.
$env:A2A_ADK_TRANSPORT_AUTH    = "false"

Write-Host "  NPCI_TRANSPORT           = $($env:NPCI_TRANSPORT)" -ForegroundColor DarkGray
Write-Host "  NPCI_A2A_AGENT_CARD_URL  = $($env:NPCI_A2A_AGENT_CARD_URL)" -ForegroundColor DarkGray
Write-Host "  BANK_A2A_AGENT_CARD_URL  = $($env:BANK_A2A_AGENT_CARD_URL)" -ForegroundColor DarkGray
Write-Host "  A2A_ADK_TRANSPORT_AUTH   = $($env:A2A_ADK_TRANSPORT_AUTH)" -ForegroundColor DarkGray
Write-Host ""

# -- Start PostgreSQL (needed by the bank portal + ADK server) --------------
Write-Host "[1/2] Starting PostgreSQL (pgvector)..." -ForegroundColor Yellow
$dockerOut = & docker compose up -d 2>&1
$dockerExit = $LASTEXITCODE
$dockerOut | ForEach-Object { Write-Host "      $_" }
if ($dockerExit -ne 0) {
    Write-Host "      ERROR: Failed to start Postgres. Is Docker Desktop running?" -ForegroundColor Red
    exit 1
}
Write-Host "      Waiting for Postgres to accept connections..." -ForegroundColor DarkYellow
$waited = 0
while ($waited -lt 30) {
    Start-Sleep -Seconds 2
    $waited += 2
    $ready = docker exec mindgate_postgres pg_isready -U postgres 2>&1
    if ($ready -match "accepting connections") {
        Write-Host "      PostgreSQL is ready on port 5432." -ForegroundColor Green
        break
    }
}

# -- Launch the three A2A processes, each in its own window -----------------
Write-Host ""
Write-Host "[2/2] Launching Google A2A processes..." -ForegroundColor Yellow

function Start-A2AProcess {
    param([string]$Title, [string]$UvicornTarget, [int]$Port)
    $inner = "`$host.UI.RawUI.WindowTitle = '$Title'; " +
             "Set-Location '$ProjectRoot'; " +
             "& '$Python' -m uvicorn $UvicornTarget --host 0.0.0.0 --port $Port"
    Start-Process powershell -ArgumentList "-NoExit", "-Command", $inner | Out-Null
    Write-Host "      $Title  ->  http://localhost:$Port" -ForegroundColor Cyan
}

Start-A2AProcess -Title "Bank ADK A2A server (8002)" -UvicornTarget "a2a_adk_server:a2a_app"        -Port 8002
Start-A2AProcess -Title "Mock NPCI ADK agent (8001)"  -UvicornTarget "mock_npci_agent.adk_agent:a2a_app" -Port 8001
Start-A2AProcess -Title "Bank portal (8000)"          -UvicornTarget "main:app"                      -Port 8000

Write-Host ""
Write-Host "  All three processes launched in separate windows." -ForegroundColor Green
Write-Host "  Portal:      http://localhost:8000" -ForegroundColor White
Write-Host "  Bank card:   http://localhost:8002/.well-known/agent-card.json" -ForegroundColor White
Write-Host "  Mock card:   http://localhost:8001/.well-known/agent-card.json" -ForegroundColor White
Write-Host ""
Write-Host "  Push a change_communication from the mock over A2A:" -ForegroundColor Yellow
Write-Host "    $Python -m mock_npci_agent.push_change_a2a" -ForegroundColor Yellow
Write-Host ""
Write-Host "  Close the three windows (or Ctrl+C in each) to stop." -ForegroundColor DarkGray
Write-Host ""
