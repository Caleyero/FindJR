@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"

echo.
echo ============================================
echo   SUBIR CAMBIOS A GITHUB
echo ============================================
echo.

git rev-parse --is-inside-work-tree >nul 2>&1
if errorlevel 1 (
    echo ERROR: esta carpeta no es un repositorio git.
    pause
    exit /b 1
)

REM 0) ¿Hay remoto configurado? Si no, solo se hace el commit local.
set HAS_REMOTE=1
git remote get-url origin >nul 2>&1
if errorlevel 1 set HAS_REMOTE=0

REM 1) Fetch para conocer el estado real del remoto.
if !HAS_REMOTE!==1 (
    echo Comprobando estado del remoto...
    git fetch
    if errorlevel 1 (
        echo ERROR al hacer fetch. Revisa tu conexion o el acceso al repo.
        pause
        exit /b 1
    )
)

echo.
echo Estado actual:
echo --------------------------------------------
git status --short --branch
echo --------------------------------------------
echo.

REM 2) Detectar cambios locales sin commit (incluidos archivos nuevos).
set CAMBIOS=0
for /f %%a in ('git status --porcelain ^| find /c /v ""') do set CAMBIOS=%%a
if "!CAMBIOS!"=="0" goto :check_remote

REM 3) Hay cambios -> pedir mensaje y commitear.
set /p MENSAJE=Mensaje de commit:
if "!MENSAJE!"=="" (
    echo Cancelado: no se escribio mensaje.
    pause
    exit /b 1
)

git add -A
git commit -m "!MENSAJE!"
if errorlevel 1 (
    echo.
    echo ERROR al crear el commit.
    pause
    exit /b 1
)

:check_remote
if !HAS_REMOTE!==0 (
    echo.
    echo Commit guardado en local. El repo aun no esta vinculado con GitHub:
    echo usa la opcion 4 del menu para crear el vinculo y subirlo.
    pause
    exit /b 0
)

REM 4) Rama sin upstream (primera subida) -> push -u.
git rev-parse --abbrev-ref "@{u}" >nul 2>&1
if errorlevel 1 (
    echo.
    echo La rama aun no existe en GitHub. Subiendola por primera vez...
    git push -u origin HEAD
    if errorlevel 1 (
        echo.
        echo ERROR al hacer push.
        pause
        exit /b 1
    )
    echo.
    echo OK - Rama subida y vinculada con GitHub.
    pause
    exit /b 0
)

REM 5) ¿Hay commits pendientes de subir?
set AHEAD=0
for /f %%a in ('git rev-list --count "@{u}..HEAD" 2^>nul') do set AHEAD=%%a
if "!AHEAD!"=="0" (
    echo No hay commits pendientes de subir. Repositorio al dia.
    pause
    exit /b 0
)

REM 6) ¿El remoto avanzo? -> rebase automatico antes del push.
set BEHIND=0
for /f %%a in ('git rev-list --count "HEAD..@{u}" 2^>nul') do set BEHIND=%%a
if not "!BEHIND!"=="0" (
    echo.
    echo El remoto tiene !BEHIND! commit^(s^) nuevo^(s^). Rebasando local...
    git pull --rebase
    if errorlevel 1 (
        echo.
        echo ERROR durante el rebase ^(probablemente conflictos^).
        echo Resuelvelos manualmente y luego ejecuta:
        echo     git rebase --continue
        echo     git push
        pause
        exit /b 1
    )
)

REM 7) Capturar el hash actual del remoto antes de subir, para mostrar
REM    al final exactamente que commits se han subido.
set OLD_REMOTE=
for /f %%h in ('git rev-parse "@{u}" 2^>nul') do set OLD_REMOTE=%%h

REM 8) Push.
echo.
echo Subiendo a GitHub...
git push
if errorlevel 1 (
    echo.
    echo ERROR al hacer push.
    pause
    exit /b 1
)

REM 9) Verificacion: tras el push, no debe quedar nada por subir.
git fetch
set AHEAD2=0
for /f %%a in ('git rev-list --count "@{u}..HEAD" 2^>nul') do set AHEAD2=%%a
if not "!AHEAD2!"=="0" (
    echo.
    echo ************************************************************
    echo *** AVISO: tras el push siguen quedando !AHEAD2! commit^(s^)  ***
    echo *** sin subir. Revisa el repositorio antes de cerrar.    ***
    echo ************************************************************
    pause
    exit /b 1
)

echo.
echo ============================================
echo OK - Commits subidos correctamente:
echo ============================================
if defined OLD_REMOTE (
    git --no-pager log --oneline !OLD_REMOTE!..HEAD
) else (
    git --no-pager log --oneline -5
)
echo.
pause
