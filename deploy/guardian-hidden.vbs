' 2026-11-28: silent wrapper for watchdog-guardian.ps1.
'
' Why a wrapper and not -WindowStyle Hidden: Task Scheduler launches powershell.exe
' as a console application; -WindowStyle Hidden only takes effect AFTER PowerShell
' starts, so a console flashes. wscript.exe has no console at all. Same reason the
' crash guard has its own watchdog-hidden.vbs.
'
' waitOnReturn=True so Task Scheduler's ExecutionTimeLimit applies to the actual run,
' not to a wrapper that returns immediately.
Dim sh, fso, here, ps1
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
here = fso.GetParentFolderName(WScript.ScriptFullName)
ps1 = fso.GetParentFolderName(here) & "\deploy\watchdog-guardian.ps1"
If Not fso.FileExists(ps1) Then WScript.Quit 3
sh.Run "powershell.exe -NoProfile -ExecutionPolicy Bypass -File """ & ps1 & """", 0, True
