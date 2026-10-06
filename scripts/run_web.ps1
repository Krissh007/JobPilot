# Starts the JobPilot dashboard (Vite dev server) on http://127.0.0.1:5173. Start the API first (run_api.ps1).
# Usage:  powershell -ExecutionPolicy Bypass -File E:\JobPilot\scripts\run_web.ps1
$Root = Split-Path -Parent $PSScriptRoot
$env:npm_config_cache = "$Root\.cache\npm"   # keep npm's cache off C:
npm --prefix "$Root\web" run dev
