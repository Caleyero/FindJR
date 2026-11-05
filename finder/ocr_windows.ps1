# OCR con el motor integrado de Windows (Windows.Media.Ocr), sin instalar nada.
# Lo llama finder/ocr.py: recibe un archivo con una ruta de imagen por linea (UTF-8) y escribe en
# -Salida un JSON con el texto reconocido de cada imagen, en el mismo orden.
#   -Comprobar: solo comprueba que hay OCR para el idioma (codigo de salida 0 = disponible).
# Codigos de salida: 0 bien, 2 no hay OCR para el idioma, 1 otro error.
param(
    [string]$Lista = "",
    [string]$Salida = "",
    [string]$Idioma = "es-ES",
    [switch]$Comprobar
)
$ErrorActionPreference = "Stop"
try {
    Add-Type -AssemblyName System.Runtime.WindowsRuntime
    $null = [Windows.Storage.StorageFile, Windows.Storage, ContentType = WindowsRuntime]
    $null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime]
    $null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics, ContentType = WindowsRuntime]
    $null = [Windows.Globalization.Language, Windows.Globalization, ContentType = WindowsRuntime]

    $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage([Windows.Globalization.Language]::new($Idioma))
    if ($null -eq $engine) {
        [Console]::Error.WriteLine("Windows no tiene OCR para el idioma $Idioma")
        exit 2
    }
    if ($Comprobar) { exit 0 }

    # Las operaciones de WinRT son asincronas: se esperan convirtiendolas en Task de .NET
    $asTask = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
            $_.Name -eq "AsTask" -and $_.GetParameters().Count -eq 1 -and
            $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
    function Esperar($op, [Type]$tipo) {
        $t = $asTask.MakeGenericMethod($tipo).Invoke($null, @($op))
        $t.Wait(-1) | Out-Null
        $t.Result
    }

    $textos = New-Object System.Collections.Generic.List[string]
    foreach ($ruta in [IO.File]::ReadAllLines($Lista, [Text.Encoding]::UTF8)) {
        if (-not $ruta) { continue }
        $f = Esperar ([Windows.Storage.StorageFile]::GetFileFromPathAsync($ruta)) ([Windows.Storage.StorageFile])
        $s = Esperar ($f.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
        try {
            $d = Esperar ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($s)) ([Windows.Graphics.Imaging.BitmapDecoder])
            $b = Esperar ($d.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
            $r = Esperar ($engine.RecognizeAsync($b)) ([Windows.Media.Ocr.OcrResult])
            $textos.Add((($r.Lines | ForEach-Object { $_.Text }) -join "`n"))
        }
        finally {
            $s.Dispose()
        }
    }
    $json = ConvertTo-Json -InputObject @($textos.ToArray()) -Compress
    [IO.File]::WriteAllText($Salida, $json, (New-Object System.Text.UTF8Encoding($false)))
    exit 0
}
catch {
    [Console]::Error.WriteLine($_.Exception.Message)
    exit 1
}
