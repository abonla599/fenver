' Start the AI stack at logon without flashing a console window.
'
' shell:startup used to hold a .lnk pointing straight at tools\start-ai-stack.bat.
' A .bat is a console program, so cmd.exe always got a visible window for the
' second it took to run netstat/tasklist and hand off to Start-Process.
'
' Run through wscript with window style 0 and the window is created hidden.
'
' Keep this file ASCII-only (see the note in deploy\watchdog-hidden.vbs).
'
' Uninstall: point the Startup .lnk back at the .bat and delete this file.
Option Explicit

Dim sh, fso, root, bat
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

' Locate the .bat next to this file instead of a machine-specific absolute path:
' this script ships in the public snapshot and must work wherever the repo lives.
root = fso.GetParentFolderName(fso.GetParentFolderName(WScript.ScriptFullName))
bat = root & "\tools\start-ai-stack.bat"

' waitOnReturn = False: this runs during logon, and blocking the shell on a
' netstat + two Start-Process calls is not worth it.
sh.Run "cmd.exe /c """ & bat & """", 0, False
