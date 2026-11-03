@echo off
REM Wrapper para menu.ps1 - permite doble-click desde el Explorador.
powershell.exe -ExecutionPolicy Bypass -NoProfile -File "%~dp0menu.ps1"
