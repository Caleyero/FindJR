# =============================================================================
# FinderJR en segundo plano permanente (este PC).
#
# Con el arranque automatico instalado, FinderJR se inicia al entrar en Windows
# sin abrir el navegador y sin apagarse al cerrar la pestana, de modo que basta
# un favorito a http://localhost:8766/ para usarlo en cualquier momento.
#
# Uso:
#   .\arranque_automatico.ps1 -Accion Instalar   acceso directo en Inicio + arranca ya
#   .\arranque_automatico.ps1 -Accion Quitar     quita el acceso directo y detiene FinderJR
#   .\arranque_automatico.ps1 -Accion Iniciar    arranca en segundo plano (si no lo esta)
#   .\arranque_automatico.ps1 -Accion Detener    detiene el proceso en segundo plano
#   .\arranque_automatico.ps1 -Accion Estado     indica si esta instalado y respondiendo
# =============================================================================

param(
    [ValidateSet("Instalar", "Quitar", "Iniciar", "Detener", "Estado")]
    [string]$Accion = "Estado"
)

$ErrorActionPreference = "Stop"

$url       = "http://localhost:8766/"
$vbs       = Join-Path $PSScriptRoot "finderjr_fondo.vbs"
$startup   = [Environment]::GetFolderPath("Startup")
$lnk       = Join-Path $startup "FinderJR.lnk"

function Test-FinderJR {
    try {
        $r = Invoke-WebRequest -Uri "$($url)api/status" -UseBasicParsing -TimeoutSec 5
        return ($r.StatusCode -eq 200)
    } catch {
        return $false
    }
}

function Get-FinderJRProcess {
    Get-CimInstance Win32_Process -Filter "Name = 'python.exe' OR Name = 'pythonw.exe'" |
        Where-Object { $_.CommandLine -match 'app_finder\.py' }
}

function Start-FinderJR {
    if (Test-FinderJR) {
        Write-Host "  FinderJR ya esta en marcha en $url" -ForegroundColor Green
        return
    }
    Start-Process -FilePath "wscript.exe" -ArgumentList @("//B", "//Nologo", "`"$vbs`"")
    for ($i = 0; $i -lt 45; $i++) {
        Start-Sleep -Seconds 1
        if (Test-FinderJR) {
            Write-Host "  FinderJR en marcha en $url" -ForegroundColor Green
            return
        }
    }
    Write-Host "  AVISO: FinderJR no responde tras 45 s. Prueba a arrancarlo con iniciar_finderjr.bat para ver si falta Python o alguna dependencia." -ForegroundColor Yellow
}

function Stop-FinderJR {
    $procs = @(Get-FinderJRProcess)
    if ($procs.Count -eq 0) {
        Write-Host "  FinderJR no estaba en marcha." -ForegroundColor DarkGray
        return
    }
    foreach ($p in $procs) { Stop-Process -Id $p.ProcessId -Force }
    Write-Host "  FinderJR detenido." -ForegroundColor Green
}

switch ($Accion) {
    "Instalar" {
        $shell = New-Object -ComObject WScript.Shell
        $sc = $shell.CreateShortcut($lnk)
        $sc.TargetPath       = "$env:WINDIR\System32\wscript.exe"
        $sc.Arguments        = "//B //Nologo `"$vbs`""
        $sc.WorkingDirectory = $PSScriptRoot
        $sc.Description      = "FinderJR en segundo plano ($url)"
        $sc.Save()
        Write-Host "  Arranque automatico instalado: $lnk" -ForegroundColor Green
        Start-FinderJR
        Write-Host ""
        Write-Host "  Anade este favorito en el navegador: $url" -ForegroundColor White
    }
    "Quitar" {
        if (Test-Path $lnk) {
            Remove-Item $lnk -Force
            Write-Host "  Arranque automatico quitado." -ForegroundColor Green
        } else {
            Write-Host "  El arranque automatico no estaba instalado." -ForegroundColor DarkGray
        }
        Stop-FinderJR
    }
    "Iniciar" { Start-FinderJR }
    "Detener" { Stop-FinderJR }
    "Estado" {
        $inst = if (Test-Path $lnk) { "instalado" } else { "no instalado" }
        $run  = if (Test-FinderJR) { "respondiendo en $url" } else { "parado" }
        Write-Host "  Arranque automatico: $inst" -ForegroundColor White
        Write-Host "  FinderJR:            $run" -ForegroundColor White
    }
}
