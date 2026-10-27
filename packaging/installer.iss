; Inno Setup script cho HoSoPDF — tạo HoSoPDF_Setup.exe
; Nguồn: thư mục build\HoSoPDF do build.bat sinh ra (chạy build.bat TRƯỚC).
; Build: mở file này bằng Inno Setup rồi Compile, hoặc build.bat tự gọi ISCC.

#define AppName "HoSoPDF"
#define AppVersion "1.1.0"

[Setup]
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=PMH
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
OutputDir=dist
OutputBaseFilename={#AppName}_Setup
Compression=lzma2/max
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; Cài vào Program Files -> cần quyền admin. Key/cấu hình người dùng lưu ở %APPDATA% nên runtime không cần admin.
PrivilegesRequired=admin
WizardStyle=modern

[Languages]
Name: "en"; MessagesFile: "compiler:Default.isl"

[Files]
Source: "build\{#AppName}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs
; Microsoft Visual C++ Runtime: poppler (đọc/cắt/ghép PDF) cần MSVCP140/VCRUNTIME140 —
; máy chưa cài Visual C++ Redistributable sẽ hỏng mọi thao tác PDF.
Source: "build\_dl\vc_redist.x64.exe"; DestDir: "{tmp}"; Flags: deleteafterinstall

[InstallDelete]
; Bản cũ khởi động bằng VBScript (Microsoft đang gỡ dần) -> xóa launcher cũ khi nâng cấp.
Type: files; Name: "{app}\launch.vbs"
Type: files; Name: "{app}\stop.vbs"

[Tasks]
Name: "desktopicon"; Description: "Tạo icon trên Desktop"; GroupDescription: "Tùy chọn:"

[Icons]
Name: "{group}\{#AppName}";        Filename: "{app}\python\pythonw.exe"; Parameters: """{app}\launcher.py"""; WorkingDir: "{app}"; IconFilename: "{app}\app.ico"
Name: "{group}\Dừng {#AppName}";   Filename: "{app}\python\pythonw.exe"; Parameters: """{app}\stop.py""";     WorkingDir: "{app}"
Name: "{group}\Hướng dẫn sử dụng"; Filename: "{app}\HuongDan.pdf"
Name: "{group}\Gỡ cài đặt {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}";  Filename: "{app}\python\pythonw.exe"; Parameters: """{app}\launcher.py"""; WorkingDir: "{app}"; IconFilename: "{app}\app.ico"; Tasks: desktopicon

[Run]
Filename: "{tmp}\vc_redist.x64.exe"; Parameters: "/install /quiet /norestart"; StatusMsg: "Đang cài Microsoft Visual C++ Runtime..."; Flags: waituntilterminated
; runasoriginaluser: chạy bằng tài khoản người dùng, KHÔNG bằng quyền admin của bộ cài —
; app chạy quyền admin không thấy ổ mạng / ổ Google Drive (G:) của người dùng và lưu
; API key vào hồ sơ admin; server đó còn sống tới khi khởi động lại máy.
Filename: "{app}\python\pythonw.exe"; Parameters: """{app}\launcher.py"""; WorkingDir: "{app}"; Description: "Khởi động {#AppName} ngay"; Flags: postinstall nowait skipifsilent runasoriginaluser

[UninstallRun]
; Dừng app trước khi gỡ để không khoá file.
Filename: "{app}\python\pythonw.exe"; Parameters: """{app}\stop.py"""; Flags: runhidden waituntilterminated; RunOnceId: "stopapp"
