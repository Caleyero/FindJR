@echo off
cd /d "%~dp0"

if "%~1" NEQ "--silent" (
    if exist "%~dp0iniciar_finderjr.vbs" (
        start "" wscript.exe //B //Nologo "%~dp0iniciar_finderjr.vbs"
        exit /b 0
    )
)

where python >nul 2>&1
if %ERRORLEVEL% EQU 0 (
    python -c "import fitz" >nul 2>&1
    if not %ERRORLEVEL% EQU 0 (
        python -m pip install -r requirements.txt >nul 2>&1
    )
    where pythonw >nul 2>&1
    if %ERRORLEVEL% EQU 0 (
        start "" /b pythonw app_finder.py
    ) else (
        start "" /b python app_finder.py
    )
    exit /b 0
)

msg * "FinderJR: no se ha detectado Python 3. Instala Python 3 desde https://www.python.org/downloads/ marcando 'Add Python to PATH'."
exit /b 1
