# mock_npci_agent/start.ps1
# Starts the Mock NPCI Agent on PORT_B (default: 8001).
# Run this from the project root:  .\mock_npci_agent\start.ps1

param(
    [int]$Port = 8001,
    [switch]$NoReload
)

$ProjectRoot = Split-Path -Parent $PSScriptRoot

# --- Ensure we're running from project root ---
Set-Location $ProjectRoot

# --- Display startup banner ---
Write-Host ""
Write-Host "  +------------------------------------------------------+" -ForegroundColor Cyan
Write-Host "  |       Mock NPCI Agent  (Stub Mode)                  |" -ForegroundColor Cyan
Write-Host "  |       Architecture v9 Section 2 - Dummy NPCI counterpart |" -ForegroundColor Cyan
Write-Host "  +------------------------------------------------------+" -ForegroundColor Cyan
Write-Host ""

# --- Check prerequisites ---
$UvicornCmd = "uvicorn"
$VenvUvicorn = Join-Path $ProjectRoot ".venv\Scripts\uvicorn.exe"
if (Test-Path $VenvUvicorn) {
    $UvicornCmd = $VenvUvicorn
    Write-Host "  Using venv uvicorn: $UvicornCmd" -ForegroundColor Green
} else {
    if (-not (Get-Command uvicorn -ErrorAction SilentlyContinue)) {
        Write-Error "uvicorn not found. Install with:  pip install uvicorn[standard]"
        exit 1
    }
}

# --- Check .env ---
$EnvFile = Join-Path $ProjectRoot ".env"
if (-not (Test-Path $EnvFile)) {
    Write-Warning ".env not found at $EnvFile - mock will start with no secrets configured."
} else {
    Write-Host "  .env       : $EnvFile" -ForegroundColor Green
}

# --- Detect A2A_MTLS_REQUIRED warning ---
$MtlsRequired = Select-String -Path $EnvFile -Pattern "A2A_MTLS_REQUIRED=true" -Quiet
if ($MtlsRequired) {
    Write-Host ""
    Write-Warning "A2A_MTLS_REQUIRED=true is set in .env!"
    Write-Warning "The bank will reject non-mTLS calls from this mock."
    Write-Warning "Set A2A_MTLS_REQUIRED=false in .env for local dev."
    Write-Host ""
}

# --- Launch ---
Write-Host "  PORT       : $Port" -ForegroundColor Cyan
Write-Host "  UI         : http://localhost:$Port" -ForegroundColor Cyan
Write-Host "  Inbound    : http://localhost:$Port/a2a" -ForegroundColor Cyan
Write-Host "  Status     : http://localhost:$Port/status" -ForegroundColor Cyan
Write-Host ""
Write-Host "  Set in bank's .env:" -ForegroundColor Yellow
Write-Host "    NPCI_AGENT_URL=http://localhost:$Port/a2a" -ForegroundColor Yellow
Write-Host "    A2A_MTLS_REQUIRED=false" -ForegroundColor Yellow
Write-Host ""
Write-Host "  Press Ctrl+C to stop." -ForegroundColor DarkGray
Write-Host ""

$ReloadFlag = if ($NoReload) { "" } else { "--reload" }

$env:MOCK_NPCI_PORT = $Port

& $UvicornCmd mock_npci_agent.main:app --host 0.0.0.0 --port $Port $ReloadFlag
