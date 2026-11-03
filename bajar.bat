@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"

echo.
echo ============================================
echo   BAJAR CAMBIOS DE GITHUB
echo ============================================
echo.

git rev-parse --is-inside-work-tree >nul 2>&1
if errorlevel 1 (
    echo ERROR: esta carpeta no es un repositorio git.
    pause
    exit /b 1
)

git remote get-url origin >nul 2>&1
if errorlevel 1 (
    echo AVISO: el repo aun no esta vinculado con GitHub ^(menu, opcion 4^). Nada que bajar.
    pause
    exit /b 0
)

echo Comprobando cambios remotos...
git fetch
if errorlevel 1 (
    echo ERROR al hacer fetch. Revisa tu conexion o el acceso al repo.
    pause
    exit /b 1
)

REM 1) ¿Hay algo nuevo en el remoto?
set BEHIND=0
for /f %%a in ('git rev-list --count "HEAD..@{u}" 2^>nul') do set BEHIND=%%a
if "!BEHIND!"=="0" (
    echo Ya estas al dia con el remoto. Nada que bajar.
    pause
    exit /b 0
)
echo Hay !BEHIND! commit^(s^) nuevo^(s^) en GitHub:
echo --------------------------------------------
git --no-pager log --oneline HEAD..@{u}
echo --------------------------------------------
echo.

REM 2) Detectar archivos untracked locales que chocarian con el pull.
set "COLISIONES=%TEMP%\finderjr_colisiones.tmp"
if exist "!COLISIONES!" del "!COLISIONES!"
set COLISION=0

for /f "delims=" %%F in ('git diff --name-only --diff-filter=A HEAD..@{u} 2^>nul') do (
    if exist "%%F" (
        git ls-files --error-unmatch "%%F" >nul 2>&1
        if errorlevel 1 (
            >>"!COLISIONES!" echo %%F
            set COLISION=1
        )
    )
)

if !COLISION!==1 (
    echo.
    echo AVISO: archivos locales sin rastrear que el pull sobrescribiria:
    echo --------------------------------------------
    type "!COLISIONES!"
    echo --------------------------------------------
    echo.
    echo Suele pasar cuando creaste el mismo archivo aqui y en otra maquina.
    echo Si confirmas, se borraran los locales y se traeran los del remoto.
    set /p BORRAR=Eliminarlos antes del pull? [s/N]:
    if /i not "!BORRAR!"=="s" (
        del "!COLISIONES!"
        echo Cancelado. Mueve o renombra esos archivos manualmente.
        pause
        exit /b 1
    )
    for /f "usebackq delims=" %%F in ("!COLISIONES!") do (
        if exist "%%F" del "%%F"
    )
    del "!COLISIONES!"
)

REM 3) Cambios locales sin commit -> auto-stash.
git diff --quiet --cached
set STAGED_EMPTY=%errorlevel%
git diff --quiet
set UNSTAGED_EMPTY=%errorlevel%

set STASHED=0
if not %STAGED_EMPTY%==0 set STASHED=1
if not %UNSTAGED_EMPTY%==0 set STASHED=1

if !STASHED!==1 (
    echo Tienes cambios locales sin commit. Guardando en stash temporal...
    git stash push -u -m "auto-stash bajar.bat"
    if errorlevel 1 (
        echo ERROR al hacer stash.
        pause
        exit /b 1
    )
)

REM 4) Pull con rebase.
echo.
echo Descargando cambios ^(pull --rebase^)...
git pull --rebase
if errorlevel 1 (
    echo.
    echo ERROR durante el rebase ^(probables conflictos^).
    if !STASHED!==1 (
        echo NOTA: tus cambios locales siguen guardados en "git stash list".
        echo Resuelve los conflictos del rebase, luego: git rebase --continue
        echo Por ultimo: git stash pop
    )
    pause
    exit /b 1
)

REM 5) Restaurar el stash si aplicaba.
if !STASHED!==1 (
    echo.
    echo Restaurando tus cambios locales del stash...
    git stash pop
    if errorlevel 1 (
        echo.
        echo CONFLICTO al restaurar el stash. Tus cambios siguen en "git stash list".
        echo Resuelve los conflictos manualmente y luego: git stash drop
        pause
        exit /b 1
    )
)

echo.
echo ============================================
echo OK - Repositorio al dia con GitHub.
echo ============================================
pause
