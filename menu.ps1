# =============================================================================
# Menu interactivo de tareas habituales en FinderJR.
# Centraliza los scripts del repo agrupados por contexto, con descripcion
# breve. Las opciones delicadas piden confirmacion antes de ejecutarse.
#
# Uso:
#   .\menu.ps1            (desde PowerShell en la raiz)
#   menu.bat              (doble-click en el Explorador)
# =============================================================================

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

function Confirm-Action {
    param([string]$Message)
    Write-Host ""
    Write-Host "  AVISO: $Message" -ForegroundColor Yellow
    $r = Read-Host "  Continuar? [s/N]"
    return ($r -match '^(s|si|y|yes)$')
}

function Get-RemoteUrl {
    # 'git config --get' no escribe en stderr si la clave no existe. Con
    # ErrorActionPreference=Stop, cualquier salida de git por stderr redirigida
    # (p. ej. "No such remote 'origin'") abortaria el menu.
    $url = git config --get remote.origin.url
    if (-not $url) { return $null }
    return $url.Trim()
}

function Show-Header {
    Clear-Host
    Write-Host ""
    Write-Host "  FinderJR - Menu de tareas" -ForegroundColor White
    Write-Host "  $((Get-Date).ToString('yyyy-MM-dd HH:mm'))   $env:COMPUTERNAME" -ForegroundColor DarkGray
    $remote = Get-RemoteUrl
    if ($remote) {
        Write-Host "  Remoto: $remote" -ForegroundColor DarkGray
    } else {
        Write-Host "  Remoto: (sin vincular con GitHub - opcion 4)" -ForegroundColor DarkYellow
    }
    Write-Host "  -------------------------------------------------------------" -ForegroundColor DarkGray
}

function Show-Item {
    param([string]$Num, [string]$Title, [string]$Hint)
    Write-Host ("  {0,3})  " -f $Num) -NoNewline -ForegroundColor White
    Write-Host $Title.PadRight(36) -NoNewline -ForegroundColor White
    Write-Host $Hint -ForegroundColor DarkGray
}

function Show-Menu {
    Show-Header

    Write-Host ""
    Write-Host "  GIT" -ForegroundColor Cyan
    Show-Item "1" "Estado del repo"          "estado.bat - cambios locales y diferencias con GitHub"
    Show-Item "2" "Bajar cambios"            "bajar.bat  - git pull con auto-stash"
    Show-Item "3" "Subir cambios"            "subir.bat  - git add + commit + push"
    Show-Item "4" "Vincular con GitHub"      "anade el remoto 'origin' y sube la rama por primera vez"

    Write-Host ""
    Write-Host "  APLICACION (este PC)" -ForegroundColor Cyan
    Show-Item "5" "Arrancar FinderJR"        "iniciar_finderjr.bat - servidor + navegador (puerto 8766)"
    Show-Item "6" "Instalar dependencias"    "pip install -r requirements.txt"
    Show-Item "7" "Sincronizar indice"       "python -m finder.indexador - solo archivos nuevos/modificados"
    Show-Item "8" "Reindexar todo"           "python -m finder.indexador --full - relee todos los PDF"
    Show-Item "9" "Prueba de la API"         "python tools\probar_api.py - busquedas de ejemplo"

    Write-Host ""
    Write-Host "  SEGUNDO PLANO (favorito http://localhost:8766/)" -ForegroundColor Cyan
    Show-Item "10" "Activar arranque automatico" "arranca FinderJR al entrar en Windows, sin apagarse"
    Show-Item "11" "Quitar arranque automatico"  "lo quita del Inicio y detiene FinderJR"
    Show-Item "12" "Estado segundo plano"        "instalado / respondiendo"
    Show-Item "13" "Reiniciar FinderJR"          "detiene y vuelve a arrancar (tras actualizar codigo)"

    Write-Host ""
    Write-Host "   Q)  Salir" -ForegroundColor DarkGray
    Write-Host ""
}

function Connect-GitHub {
    $actual = Get-RemoteUrl
    if ($actual) {
        Write-Host "  El repo ya esta vinculado con: $actual" -ForegroundColor Yellow
        $r = Read-Host "  Cambiar la URL del remoto? [s/N]"
        if ($r -notmatch '^(s|si|y|yes)$') { return }
    } else {
        Write-Host "  1. Crea un repositorio VACIO en https://github.com/new" -ForegroundColor White
        Write-Host "     (nombre sugerido: FinderJR, privado, sin README ni .gitignore)" -ForegroundColor DarkGray
        Write-Host "  2. Copia su URL y pegala aqui." -ForegroundColor White
        Write-Host ""
    }
    $url = (Read-Host "  URL del repositorio (ej. https://github.com/Caleyero/FinderJR.git)").Trim()
    if (-not $url) {
        Write-Host "  Cancelado." -ForegroundColor DarkGray
        return
    }
    if ($actual) {
        git remote set-url origin $url
    } else {
        git remote add origin $url
    }
    if ($LASTEXITCODE -ne 0) { throw "No se pudo configurar el remoto." }
    $rama = (git rev-parse --abbrev-ref HEAD).Trim()
    Write-Host ""
    Write-Host "  Subiendo la rama '$rama' a GitHub..." -ForegroundColor Cyan
    git push -u origin $rama
    if ($LASTEXITCODE -ne 0) { throw "Fallo el push inicial. Revisa la URL y tus credenciales de GitHub." }
    Write-Host ""
    Write-Host "  OK - Repositorio vinculado y subido." -ForegroundColor Green
}

while ($true) {
    Show-Menu
    $choice = (Read-Host "  Opcion").Trim().ToLower()
    Write-Host ""

    $cmd = $null
    $needsConfirm = $null

    switch -Regex ($choice) {
        '^(1)$'  { $cmd = { & "$PSScriptRoot\estado.bat" } }
        '^(2)$'  { $cmd = { & "$PSScriptRoot\bajar.bat" } }
        '^(3)$'  { $cmd = { & "$PSScriptRoot\subir.bat" } }
        '^(4)$'  { $cmd = { Connect-GitHub } }
        '^(5)$'  {
            & "$PSScriptRoot\iniciar_finderjr.bat"
            Write-Host "  FinderJR se esta abriendo en el navegador (http://localhost:8766)." -ForegroundColor Green
            Start-Sleep -Seconds 2
            continue
        }
        '^(6)$'  { $cmd = { python -m pip install -r "$PSScriptRoot\requirements.txt" } }
        '^(7)$'  { $cmd = { python -m finder.indexador } }
        '^(8)$'  {
            $needsConfirm = "Esto vacia el indice y vuelve a leer TODOS los documentos de la Biblioteca (unos minutos). Cierra FinderJR antes si esta abierto (en segundo plano: opcion 13 al terminar, o detenlo antes)."
            $cmd = { python -m finder.indexador --full }
        }
        '^(9)$'  { $cmd = { python "$PSScriptRoot\tools\probar_api.py" } }
        '^(10)$' { $cmd = { & "$PSScriptRoot\arranque_automatico.ps1" -Accion Instalar } }
        '^(11)$' { $cmd = { & "$PSScriptRoot\arranque_automatico.ps1" -Accion Quitar } }
        '^(12)$' { $cmd = { & "$PSScriptRoot\arranque_automatico.ps1" -Accion Estado } }
        '^(13)$' { $cmd = { & "$PSScriptRoot\arranque_automatico.ps1" -Accion Detener; & "$PSScriptRoot\arranque_automatico.ps1" -Accion Iniciar } }
        '^(q|0|exit|salir)$' { exit 0 }
        default {
            Write-Host "  Opcion no reconocida." -ForegroundColor Yellow
            Start-Sleep -Seconds 1
            continue
        }
    }

    if ($needsConfirm) {
        if (-not (Confirm-Action $needsConfirm)) {
            Write-Host "  Cancelado." -ForegroundColor DarkGray
            Start-Sleep -Seconds 1
            continue
        }
    }

    if ($cmd) {
        try {
            & $cmd
        } catch {
            Write-Host ""
            Write-Host "  ERROR: $($_.Exception.Message)" -ForegroundColor Red
        }
        Write-Host ""
        Read-Host "  --- Pulsa Enter para volver al menu ---" | Out-Null
    }
}
