@echo off
rem Double-click to stop the AI Trading Dashboard background servers.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" -Stop
echo.
pause
