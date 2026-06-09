' Dừng HoSoPDF: kill đúng tiến trình python.exe của bản cài này (không đụng python khác).
Option Explicit

Dim fso, sh, base, target, ps
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh  = CreateObject("WScript.Shell")

base   = fso.GetParentFolderName(WScript.ScriptFullName)
target = base & "\python\python.exe"

ps = "Get-CimInstance Win32_Process -Filter ""Name='python.exe'"" | " & _
     "Where-Object { $_.ExecutablePath -eq '" & target & "' } | " & _
     "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"

sh.Run "powershell -NoProfile -WindowStyle Hidden -Command """ & ps & """", 0, True
