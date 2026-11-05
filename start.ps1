<#
.SYNOPSIS
  Start / stop the AI Trading Dashboard (FastAPI backend + Vite frontend) as background processes.

.DESCRIPTION
  The servers run detached from this window: you can close the window (or log out of the terminal) and the app keeps
  running at http://127.0.0.1:5173 until you stop it with `.\start.ps1 -Stop` (or `stop.bat`), or restart Windows.
  Processes are created through WMI (Win32_Process.Create), so they do not belong to this console or to the
  terminal's job object and are not killed when the window closes.

  - Installs dependencies on first run (uv sync / npm install).
  - Waits until both servers respond, opens the dashboard in the browser, then exits.
  Logs: backend\var\logs\backend.log and frontend.log (appended; rotated when larger than 20 MB).

.EXAMPLE
  .\start.ps1                    # start in the background (default) and open the browser
  .\start.ps1 -NoBrowser         # start without opening the browser
  .\start.ps1 -Status            # show whether the servers are running
  .\start.ps1 -Restart           # stop, then start again (e.g. after pulling new code)
  .\start.ps1 -Stop              # stop both servers
  .\start.ps1 -InstallAutoStart  # also start automatically when you log in to Windows
  .\start.ps1 -RemoveAutoStart   # remove the automatic start
#>
param(
    [switch]$NoBrowser,
    [switch]$Stop,
    [switch]$Status,
    [switch]$Restart,
    [switch]$InstallAutoStart,
    [switch]$RemoveAutoStart
)

$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot
$Backend = Join-Path $Root "backend"
$Frontend = Join-Path $Root "frontend"
$LogDir = Join-Path $Backend "var\logs"
$RunDir = Join-Path $Backend "var\run"
$ApiUrl = "http://127.0.0.1:8000/api/health"
$WebUrl = "http://127.0.0.1:5173"
$TaskName = "AI Trading Dashboard"

function Get-ListenerPid([int]$Port) {
    $c = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($c) { return [int]$c.OwningProcess } else { return $null }
}

function Stop-Tree([int]$ProcessId) {
    # Stops the process and its children (cmd -> uv -> python, cmd -> npm -> node). Ignores exited processes.
    & cmd.exe /c "taskkill /T /F /PID $ProcessId >nul 2>&1"
}

function Test-Url([string]$Url) {
    try { return (Invoke-WebRequest -UseBasicParsing -Uri $Url -TimeoutSec 3).StatusCode -eq 200 } catch { return $false }
}

function Stop-Servers {
    foreach ($name in "backend", "frontend") {
        $pidFile = Join-Path $RunDir "$name.pid"
        if (Test-Path $pidFile) {
            $procId = [int](Get-Content $pidFile -Raw)
            if (Get-Process -Id $procId -ErrorAction SilentlyContinue) { Stop-Tree $procId }
            Remove-Item $pidFile -Force
        }
    }
    foreach ($port in 8000, 5173) {  # anything still listening (e.g. started by an older version of this script)
        $procId = Get-ListenerPid $port
        if ($procId) { Stop-Tree $procId }
    }
    Start-Sleep -Milliseconds 800
    foreach ($port in 8000, 5173) {
        if (Get-ListenerPid $port) { Write-Host "Port $port is still in use." -ForegroundColor Yellow }
        else { Write-Host "Stopped server on port $port" }
    }
}

function Show-Status {
    $api = Test-Url $ApiUrl
    $web = Test-Url $WebUrl
    Write-Host ("Backend  (port 8000): " + $(if ($api) { "RUNNING" } elseif (Get-ListenerPid 8000) { "STARTING / NOT RESPONDING" } else { "STOPPED" }))
    Write-Host ("Dashboard (port 5173): " + $(if ($web) { "RUNNING  -> $WebUrl" } elseif (Get-ListenerPid 5173) { "STARTING / NOT RESPONDING" } else { "STOPPED" }))
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Write-Host ("Auto-start at logon: " + $(if ($task) { "ON" } else { "off" }))
    Write-Host "Logs: $LogDir"
}

function Rotate-Log([string]$Path) {
    if ((Test-Path $Path) -and (Get-Item $Path).Length -gt 20MB) { Move-Item $Path "$Path.1" -Force }
}

function Start-Detached([string]$Name, [string]$Exe, [string]$Arguments, [string]$WorkDir) {
    # cmd.exe wrapper only to append stdout/stderr to the log file; hidden window; created by WMI so it is fully
    # independent of this console (closing the window does not stop it).
    $log = Join-Path $LogDir "$Name.log"
    Rotate-Log $log
    $cmdLine = "cmd.exe /d /c `"`"$Exe`" $Arguments >> `"$log`" 2>&1`""
    $startup = New-CimInstance -ClassName Win32_ProcessStartup -ClientOnly -Property @{ ShowWindow = [uint16]0 }
    $r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{
        CommandLine = $cmdLine; CurrentDirectory = $WorkDir; ProcessStartupInformation = $startup }
    if ($r.ReturnValue -ne 0) { throw "Could not start $Name (Win32_Process.Create returned $($r.ReturnValue))" }
    Set-Content -Path (Join-Path $RunDir "$Name.pid") -Value $r.ProcessId -NoNewline
    return [int]$r.ProcessId
}

function Wait-Url([string]$Url, [int]$TimeoutSec, [int]$RootPid, [string]$Log) {
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        if (Test-Url $Url) { return $true }
        if ($RootPid -and -not (Get-Process -Id $RootPid -ErrorAction SilentlyContinue)) {
            Write-Host "The process exited early. Last log lines ($Log):" -ForegroundColor Red
            if (Test-Path $Log) { Get-Content $Log -Tail 20 }
            return $false
        }
        Start-Sleep -Milliseconds 700
    }
    return $false
}

# ------------------------------------------------------------------ commands
if ($Stop) { Stop-Servers; exit 0 }
if ($Status) { Show-Status; exit 0 }
if ($RemoveAutoStart) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "Auto-start at logon removed."
    } else { Write-Host "Auto-start at logon was not installed." }
    exit 0
}
if ($InstallAutoStart) {
    $action = New-ScheduledTaskAction -Execute "powershell.exe" `
        -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$PSCommandPath`" -NoBrowser"
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -ExecutionTimeLimit (New-TimeSpan -Minutes 10)
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
        -Description "Starts the AI Trading Dashboard servers in the background at logon" -Force | Out-Null
    Write-Host "Auto-start installed: the dashboard will start in the background each time you log in to Windows." -ForegroundColor Green
    Write-Host "(Remove with .\start.ps1 -RemoveAutoStart.) Starting it now..."
}
if ($Restart) { Stop-Servers }

$uv = (Get-Command uv -ErrorAction SilentlyContinue).Source
$npm = (Get-Command npm.cmd -ErrorAction SilentlyContinue).Source
if (-not $uv -or -not $npm) {
    Write-Host "'uv' and 'npm' must be on PATH. Install them first (see README.md)." -ForegroundColor Red
    exit 1
}

New-Item -ItemType Directory -Force $LogDir, $RunDir | Out-Null

function Invoke-Native([string]$Dir, [string]$Exe, [string[]]$ArgList) {
    # Native tools (uv, npm) print progress on stderr; Windows PowerShell 5.1 would treat that as a terminating
    # error under ErrorActionPreference=Stop, so judge success by the exit code instead.
    Push-Location $Dir
    $old = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try { & $Exe @ArgList 2>&1 | ForEach-Object { "$_" } | Write-Host; $code = $LASTEXITCODE }
    finally { $ErrorActionPreference = $old; Pop-Location }
    if ($code -ne 0) { throw "'$Exe $($ArgList -join ' ')' failed with exit code $code" }
}

function Test-Stale([string]$Lock, [string]$Stamp) {
    return (-not (Test-Path $Stamp)) -or ((Test-Path $Lock) -and (Get-Item $Lock).LastWriteTime -gt (Get-Item $Stamp).LastWriteTime)
}

$venvStamp = Join-Path $Backend ".venv\.synced"
if (-not (Test-Path (Join-Path $Backend ".venv")) -or (Test-Stale (Join-Path $Backend "uv.lock") $venvStamp)) {
    Write-Host "Installing / updating backend dependencies..." -ForegroundColor Cyan
    Invoke-Native $Backend $uv @("sync")
    Set-Content $venvStamp (Get-Date -Format o)
}
$nodeStamp = Join-Path $Frontend "node_modules\.installed"
if (-not (Test-Path (Join-Path $Frontend "node_modules")) -or (Test-Stale (Join-Path $Frontend "package-lock.json") $nodeStamp)) {
    Write-Host "Installing / updating frontend dependencies..." -ForegroundColor Cyan
    Invoke-Native $Frontend $npm @("install", "--no-audit", "--no-fund")
    Set-Content $nodeStamp (Get-Date -Format o)
}

$backendPid = $null
$frontendPid = $null
if (Get-ListenerPid 8000) {
    Write-Host "Backend already running on port 8000." -ForegroundColor Yellow
} else {
    Write-Host "Starting backend   -> http://127.0.0.1:8000 (background)" -ForegroundColor Cyan
    $backendPid = Start-Detached "backend" $uv "run uvicorn app.main:app --host 127.0.0.1 --port 8000" $Backend
}
if (Get-ListenerPid 5173) {
    Write-Host "Dashboard already running on port 5173." -ForegroundColor Yellow
} else {
    Write-Host "Starting dashboard -> $WebUrl (background)" -ForegroundColor Cyan
    $frontendPid = Start-Detached "frontend" $npm "run dev" $Frontend
}

if (-not (Wait-Url $ApiUrl 120 $backendPid (Join-Path $LogDir "backend.log"))) {
    Write-Host "Backend did not start. See $LogDir\backend.log" -ForegroundColor Red
    exit 1
}
Write-Host "Backend ready." -ForegroundColor Green
if (-not (Wait-Url $WebUrl 90 $frontendPid (Join-Path $LogDir "frontend.log"))) {
    Write-Host "Dashboard did not start. See $LogDir\frontend.log" -ForegroundColor Red
    exit 1
}
Write-Host "Dashboard ready: $WebUrl" -ForegroundColor Green
if (-not $NoBrowser) { Start-Process $WebUrl }
Write-Host ""
Write-Host "The app is running in the background - you can close this window." -ForegroundColor Green
Write-Host "Stop it with:  .\start.ps1 -Stop   (or double-click stop.bat)"
Write-Host "Status:        .\start.ps1 -Status"
Write-Host "Market data loads in the background (~1-2 min on first start). Log in with Zerodha in 'Data & Integrations' once per trading day."
exit 0
