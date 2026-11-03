' Lanzador invisible de FinderJR (sin ventana de consola)
Option Explicit
Dim fso, scriptDir, WshShell
Set fso = CreateObject("Scripting.FileSystemObject")
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
Set WshShell = CreateObject("WScript.Shell")
WshShell.CurrentDirectory = scriptDir
WshShell.Run """" & fso.BuildPath(scriptDir, "iniciar_finderjr.bat") & """ --silent", 0, False
