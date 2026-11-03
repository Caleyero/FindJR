@echo off
REM Wrapper para menu.ps1 - permite doble-click desde el Explorador.
powershell.exe -ExecutionPolicy Bypass -NoProfile -File "%~dp0menu.ps1"
REM Si el menu termina con error, mantener la ventana abierta para poder leerlo.
if errorlevel 1 (
    echo.
    echo El menu ha terminado con un error. Revisa el mensaje anterior.
    pause
)
