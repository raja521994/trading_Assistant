@echo off
rem Double-click to start the AI Trading Dashboard in the background. You can close this window afterwards;
rem the app keeps running. Stop it with stop.bat.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" %*
echo.
pause
