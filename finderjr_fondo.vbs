' Arranca FinderJR en segundo plano permanente: sin ventana, sin abrir el
' navegador y sin apagarse al cerrar la pestana (favorito http://localhost:8766/).
Option Explicit
Dim fso, scriptDir, WshShell
Set fso = CreateObject("Scripting.FileSystemObject")
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
Set WshShell = CreateObject("WScript.Shell")
WshShell.CurrentDirectory = scriptDir
WshShell.Run """" & fso.BuildPath(scriptDir, "iniciar_finderjr.bat") & """ --silent --fondo", 0, False
