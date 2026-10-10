# Starts the JobPilot API + dashboard on this computer only: http://127.0.0.1:8000 (API docs at /docs).
# Usage:  powershell -ExecutionPolicy Bypass -File E:\JobPilot\scripts\run_api.ps1            # start (or say it's already running)
#         powershell -ExecutionPolicy Bypass -File E:\JobPilot\scripts\run_api.ps1 -Restart   # replace a running copy (e.g. after an update)
#         ... -Verbose   also log every request (the open dashboard polls every few seconds, so this is noisy)
# By default only warnings and errors are printed, so a real problem stands out.
param([switch]$Restart, [switch]$Verbose)
$Root = Split-Path -Parent $PSScriptRoot
$Port = 8000
$Url = "http://127.0.0.1:$Port"

$listener = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue | Select-Object -First 1
if ($listener) {
    $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$($listener.OwningProcess)"
    $isJobPilot = $proc.CommandLine -match "jobpilot\.api\.main"
    if (-not $isJobPilot) {
        Write-Host "Port $Port is used by another program (PID $($proc.ProcessId)): $($proc.CommandLine)" -ForegroundColor Red
        Write-Host "Close that program, then run this script again."
        exit 1
    }
    if (-not $Restart) {
        Write-Host "JobPilot is already running (PID $($proc.ProcessId), started $($proc.CreationDate))." -ForegroundColor Green
        Write-Host "Open $Url   - or add -Restart to replace it with a fresh copy in this window."
        exit 0
    }
    Write-Host "Stopping the running JobPilot API (PID $($proc.ProcessId))..."
    Stop-Process -Id $proc.ProcessId -Force
    Start-Sleep -Milliseconds 800
}

$logging = if ($Verbose) { @("--log-level", "info") } else { @("--log-level", "warning", "--no-access-log") }
Write-Host "Starting JobPilot at $Url  (Ctrl+C to stop)" -ForegroundColor Green
if ($Verbose) { Write-Host "Logging every request." -ForegroundColor DarkGray }
else { Write-Host "Quiet mode: only warnings and errors are shown (add -Verbose to see every request)." -ForegroundColor DarkGray }
& "$Root\.venv\Scripts\uvicorn.exe" --app-dir $Root jobpilot.api.main:app --host 127.0.0.1 --port $Port @logging
