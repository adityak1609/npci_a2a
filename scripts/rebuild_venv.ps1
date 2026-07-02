Write-Host "Rebuilding .venv using Python 3.11..."
if (Test-Path ".venv") {
    Remove-Item -Recurse -Force .venv
}
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
Write-Host ".venv rebuilt successfully!" -ForegroundColor Green
