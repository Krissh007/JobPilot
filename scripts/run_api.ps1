# Starts the JobPilot API on this computer only (http://127.0.0.1:8000, docs at /docs).
# Usage:  powershell -ExecutionPolicy Bypass -File E:\JobPilot\scripts\run_api.ps1
$Root = Split-Path -Parent $PSScriptRoot
& "$Root\.venv\Scripts\uvicorn.exe" --app-dir $Root jobpilot.api.main:app --host 127.0.0.1 --port 8000
