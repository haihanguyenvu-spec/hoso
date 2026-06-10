' Khởi động HoSoPDF: thêm poppler vào PATH, chạy Streamlit ẩn console, mở trình duyệt.
' File này được đặt ở thư mục cài đặt (cùng cấp với python\, poppler\, app\).
Option Explicit

Dim fso, sh, base, py, appDir, appPy, popplerBin, env, cmd, url
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh  = CreateObject("WScript.Shell")

base       = fso.GetParentFolderName(WScript.ScriptFullName)
py         = base & "\python\python.exe"
appDir     = base & "\app"
appPy      = appDir & "\hoso_tool\app.py"
popplerBin = base & "\poppler\Library\bin"

' poppler được gọi bằng tên trần (pdfinfo, pdftoppm, pdfseparate, pdfunite, pdftotext)
' -> phải nằm trên PATH của tiến trình Python.
Set env = sh.Environment("PROCESS")
env("PATH") = popplerBin & ";" & env("PATH")

' Streamlit đọc .streamlit\config.toml theo thư mục làm việc -> đặt CWD = app\.
sh.CurrentDirectory = appDir

Dim logDir, logFile, testFile
logDir = sh.ExpandEnvironmentStrings("%APPDATA%\HoSoPDF")
logFile = logDir & "\app_log.txt"

url = "http://localhost:8501"

On Error Resume Next
If Not fso.FolderExists(logDir) Then
    fso.CreateFolder(logDir)
End If

Set testFile = fso.CreateTextFile(logFile, True)
If Err.Number = 0 Then
    testFile.Close()
    cmd = "cmd.exe /c """"" & py & """ -m streamlit run """ & appPy & """ --server.headless=true --server.port=8501 --browser.gatherUsageStats=false > """ & logFile & """ 2>&1"""
Else
    Err.Clear()
    cmd = """" & py & """ -m streamlit run """ & appPy & """" & _
          " --server.headless=true --server.port=8501 --browser.gatherUsageStats=false"
End If
On Error GoTo 0

' 0 = ẩn cửa sổ, False = không chờ (server chạy nền).
sh.Run cmd, 0, False

' Chờ server lên rồi mở trình duyệt mặc định.
WScript.Sleep 5000
sh.Run url, 1, False
