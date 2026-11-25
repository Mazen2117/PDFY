$ErrorActionPreference = "Stop"

Set-Location $PSScriptRoot

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    Write-Host "Creating virtual environment..."
    python -m venv .venv
}

Write-Host "Starting PDFY server on http://localhost:8000 ..."
.\.venv\Scripts\python.exe -m uvicorn main:app --host 0.0.0.0 --port 8000
