' Khởi động HoSoPDF: thêm poppler + tesseract vào PATH, chạy Streamlit ẩn console, mở trình duyệt.
' File này được đặt ở thư mục cài đặt (cùng cấp với python\, poppler\, tesseract\, app\).
Option Explicit

Dim fso, sh, base, py, appDir, appPy, popplerBin, tessDir, env, cmd, url
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh  = CreateObject("WScript.Shell")

base       = fso.GetParentFolderName(WScript.ScriptFullName)
py         = base & "\python\python.exe"
appDir     = base & "\app"
appPy      = appDir & "\hoso_tool\app.py"
popplerBin = base & "\poppler\Library\bin"
tessDir    = base & "\tesseract"

' poppler và tesseract được gọi bằng tên trần (pdfinfo, pdftoppm, ..., tesseract)
' -> phải nằm trên PATH của tiến trình Python. Đặt TRƯỚC PATH sẵn có để bản đi kèm
' thắng mọi bản khác đã cài trên máy.
Set env = sh.Environment("PROCESS")
env("PATH") = popplerBin & ";" & tessDir & ";" & env("PATH")
' Chỉ rõ dữ liệu nhận dạng của bản đi kèm: nếu máy có sẵn TESSDATA_PREFIX của một
' Tesseract khác (thiếu osd/eng) thì sửa xoay sẽ hỏng âm thầm.
env("TESSDATA_PREFIX") = tessDir & "\tessdata"

' Streamlit đọc .streamlit\config.toml theo thư mục làm việc -> đặt CWD = app\.
sh.CurrentDirectory = appDir

Dim logDir, logFile, testFile
logDir = sh.ExpandEnvironmentStrings("%APPDATA%\HoSoPDF")
logFile = logDir & "\app_log.txt"

url = "http://localhost:8501"

' Đã chạy sẵn (người dùng bấm icon lần nữa) -> chỉ mở trình duyệt, không dựng server
' thứ hai (sẽ lỗi trùng cổng) và không ghi đè nhật ký của server đang chạy.
If ServerUp() Then
    sh.Run url, 1, False
    WScript.Quit
End If

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

' Chờ server THỰC SỰ lên rồi mới mở trình duyệt. Lần đầu sau khi cài có thể mất
' 20-30s (Windows Defender quét thư viện) — chờ cứng vài giây thì người dùng gặp trang
' "không kết nối được" và tưởng phần mềm hỏng.
Dim i
For i = 1 To 90
    WScript.Sleep 1000
    If ServerUp() Then Exit For
Next
sh.Run url, 1, False

Function ServerUp()
    ' Hỏi endpoint sức khỏe của Streamlit; lỗi kết nối = server chưa lên.
    Dim http
    ServerUp = False
    On Error Resume Next
    Set http = CreateObject("MSXML2.ServerXMLHTTP.6.0")
    http.setTimeouts 1000, 1000, 1000, 1000
    http.Open "GET", "http://127.0.0.1:8501/_stcore/health", False
    http.Send
    If Err.Number = 0 Then ServerUp = (http.Status = 200)
    Err.Clear
    On Error GoTo 0
End Function
