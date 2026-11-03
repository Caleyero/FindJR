@echo off
cd /d "%~dp0"

if "%~1" NEQ "--silent" (
    if exist "%~dp0iniciar_finderjr.vbs" (
        start "" wscript.exe //B //Nologo "%~dp0iniciar_finderjr.vbs"
        exit /b 0
    )
)

REM --fondo: segundo plano permanente (arranque automatico, favorito en el navegador)
set "FJR_ARGS="
if /i "%~2"=="--fondo" set "FJR_ARGS=--no-browser --no-auto-shutdown"

where python >nul 2>&1
if errorlevel 1 goto sin_python

python -c "import fitz" >nul 2>&1
if errorlevel 1 python -m pip install -r requirements.txt >nul 2>&1

where pythonw >nul 2>&1
if errorlevel 1 (
    start "" /b python app_finder.py %FJR_ARGS%
) else (
    start "" /b pythonw app_finder.py %FJR_ARGS%
)
exit /b 0

:sin_python
msg * "FinderJR: no se ha detectado Python 3. Instala Python 3 desde https://www.python.org/downloads/ marcando 'Add Python to PATH'."
exit /b 1
