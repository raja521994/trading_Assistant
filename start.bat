@echo off
rem Double-click to start the AI Trading Dashboard (backend + frontend). Close this window or press Ctrl+C to stop.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" %*
