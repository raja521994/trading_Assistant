<#
.SYNOPSIS
  Start the AI Trading Dashboard (FastAPI backend + Vite frontend) with one command.

.DESCRIPTION
  - Installs dependencies on first run (uv sync / npm install).
  - Starts the backend on http://127.0.0.1:8000 and the dashboard on http://127.0.0.1:5173.
  - Waits until both respond, opens the dashboard in your browser, and keeps running.
  - Press Ctrl+C (or close the window) to stop both servers.
  Logs: backend\var\logs\backend.log and frontend.log

.EXAMPLE
  .\start.ps1              # start everything
  .\start.ps1 -NoBrowser   # don't open the browser
  .\start.ps1 -Stop        # stop servers started earlier (anything listening on ports 8000 / 5173)
#>
param(
    [switch]$NoBrowser,
    [switch]$Stop
)

$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot
$Backend = Join-Path $Root "backend"
$Frontend = Join-Path $Root "frontend"
$LogDir = Join-Path $Backend "var\logs"
$ApiUrl = "http://127.0.0.1:8000/api/health"
$WebUrl = "http://127.0.0.1:5173"

function Get-ListenerPid([int]$Port) {
    $c = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($c) { return [int]$c.OwningProcess } else { return $null }
}

function Stop-Tree([int]$ProcessId) {
    # Stops the process and its children (uv -> python, npm -> node). Ignores already-exited processes.
    & cmd.exe /c "taskkill /T /F /PID $ProcessId >nul 2>&1"
}

function Stop-Servers {
    foreach ($port in 8000, 5173) {
        $procId = Get-ListenerPid $port
        if ($procId) { Stop-Tree $procId; Write-Host "Stopped server on port $port (pid $procId)" }
        else { Write-Host "Nothing running on port $port" }
    }
}

function Wait-Url([string]$Url, [int]$TimeoutSec, [System.Diagnostics.Process]$Proc, [string]$Log) {
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        try {
            $r = Invoke-WebRequest -UseBasicParsing -Uri $Url -TimeoutSec 3
            if ($r.StatusCode -eq 200) { return $true }
        } catch { }
        if ($Proc -and $Proc.HasExited) {
            Write-Host "Process exited early. Last log lines ($Log):" -ForegroundColor Red
            if (Test-Path $Log) { Get-Content $Log -Tail 20 }
            return $false
        }
        Start-Sleep -Milliseconds 700
    }
    return $false
}

if ($Stop) { Stop-Servers; exit 0 }

foreach ($tool in "uv", "npm") {
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) {
        Write-Host "'$tool' was not found on PATH. Install it first (see README.md)." -ForegroundColor Red
        exit 1
    }
}

New-Item -ItemType Directory -Force $LogDir | Out-Null

if (-not (Test-Path (Join-Path $Backend ".venv"))) {
    Write-Host "Installing backend dependencies (first run)..." -ForegroundColor Cyan
    Push-Location $Backend; try { uv sync } finally { Pop-Location }
}
if (-not (Test-Path (Join-Path $Frontend "node_modules"))) {
    Write-Host "Installing frontend dependencies (first run)..." -ForegroundColor Cyan
    Push-Location $Frontend; try { npm install --no-audit --no-fund } finally { Pop-Location }
}

$started = @()
$backendProc = $null
$frontendProc = $null

if (Get-ListenerPid 8000) {
    Write-Host "Backend already running on port 8000 - reusing it." -ForegroundColor Yellow
} else {
    Write-Host "Starting backend  -> http://127.0.0.1:8000" -ForegroundColor Cyan
    $backendProc = Start-Process -FilePath "uv" -WorkingDirectory $Backend -WindowStyle Hidden -PassThru `
        -ArgumentList "run", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8000" `
        -RedirectStandardOutput (Join-Path $LogDir "backend.log") -RedirectStandardError (Join-Path $LogDir "backend.err.log")
    $started += $backendProc
}

if (Get-ListenerPid 5173) {
    Write-Host "Frontend already running on port 5173 - reusing it." -ForegroundColor Yellow
} else {
    Write-Host "Starting dashboard -> $WebUrl" -ForegroundColor Cyan
    $frontendProc = Start-Process -FilePath "npm.cmd" -WorkingDirectory $Frontend -WindowStyle Hidden -PassThru `
        -ArgumentList "run", "dev" `
        -RedirectStandardOutput (Join-Path $LogDir "frontend.log") -RedirectStandardError (Join-Path $LogDir "frontend.err.log")
    $started += $frontendProc
}

try {
    if (-not (Wait-Url $ApiUrl 90 $backendProc (Join-Path $LogDir "backend.err.log"))) {
        throw "Backend did not start. See $LogDir\backend.err.log"
    }
    Write-Host "Backend ready." -ForegroundColor Green
    if (-not (Wait-Url $WebUrl 60 $frontendProc (Join-Path $LogDir "frontend.log"))) {
        throw "Dashboard did not start. See $LogDir\frontend.log"
    }
    Write-Host "Dashboard ready: $WebUrl" -ForegroundColor Green
    Write-Host "Market data loads in the background (first run ~1-2 min). Log in with Zerodha in 'Data & Integrations' once per trading day."
    if (-not $NoBrowser) { Start-Process $WebUrl }

    if ($started.Count -eq 0) { Write-Host "Both servers were already running; nothing else to do."; exit 0 }
    Write-Host "Press Ctrl+C to stop the servers. Logs: $LogDir" -ForegroundColor Cyan
    while ($true) {
        foreach ($p in $started) {
            if ($p.HasExited) { throw "A server process exited unexpectedly (pid $($p.Id)). Check logs in $LogDir" }
        }
        Start-Sleep -Seconds 2
    }
}
catch {
    Write-Host $_.Exception.Message -ForegroundColor Red
    $script:failed = $true
}
finally {
    foreach ($p in $started) {
        if (-not $p.HasExited) { Stop-Tree $p.Id }
    }
    if ($started.Count -gt 0) { Write-Host "Servers stopped." }
    if ($script:failed) { exit 1 }
}
