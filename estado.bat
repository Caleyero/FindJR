@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"

echo.
echo ============================================
echo   ESTADO DEL REPOSITORIO
echo ============================================
echo.

git rev-parse --is-inside-work-tree >nul 2>&1
if errorlevel 1 (
    echo ERROR: esta carpeta no es un repositorio git.
    pause
    exit /b 1
)

REM Sin remoto configurado todavia -> solo informacion local.
git remote get-url origin >nul 2>&1
if errorlevel 1 (
    echo AVISO: el repo aun no esta vinculado con GitHub ^(menu, opcion 4^).
    echo.
    echo --- Working tree ---
    git status --short --branch
    echo.
    echo --- Ultimos commits ---
    git --no-pager log --oneline -10
    echo.
    pause
    exit /b 0
)

echo Consultando GitHub...
git fetch >nul 2>&1
if errorlevel 1 (
    echo AVISO: no se pudo hacer fetch ^(revisa conexion^). Datos locales.
)
echo.

echo --- Working tree ---
git status --short --branch
echo.

set AHEAD=0
set BEHIND=0
for /f %%a in ('git rev-list --count "@{u}..HEAD" 2^>nul') do set AHEAD=%%a
for /f %%a in ('git rev-list --count "HEAD..@{u}" 2^>nul') do set BEHIND=%%a

if not "!AHEAD!"=="0" (
    echo --- !AHEAD! commit^(s^) sin SUBIR a GitHub ^(haz "subir.bat"^) ---
    git --no-pager log --oneline @{u}..HEAD
    echo.
)

if not "!BEHIND!"=="0" (
    echo --- !BEHIND! commit^(s^) sin BAJAR de GitHub ^(haz "bajar.bat"^) ---
    git --no-pager log --oneline HEAD..@{u}
    echo.
)

REM ¿Working tree limpio?
git diff --quiet --cached
set STAGED_EMPTY=%errorlevel%
git diff --quiet
set UNSTAGED_EMPTY=%errorlevel%
set DIRTY=0
if not %STAGED_EMPTY%==0 set DIRTY=1
if not %UNSTAGED_EMPTY%==0 set DIRTY=1

if "!AHEAD!"=="0" if "!BEHIND!"=="0" if !DIRTY!==0 (
    echo Repositorio totalmente sincronizado con GitHub.
    echo.
)

REM Stashes guardados.
for /f %%a in ('git stash list 2^>nul ^| find /c /v ""') do set STASHCOUNT=%%a
if not "!STASHCOUNT!"=="0" (
    echo --- !STASHCOUNT! stash^(es^) guardado^(s^) ^(git stash list^) ---
    git stash list
    echo.
)

pause
