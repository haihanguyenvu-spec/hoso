; Inno Setup script cho HoSoPDF — tạo HoSoPDF_Setup.exe
; Nguồn: thư mục build\HoSoPDF do build.bat sinh ra (chạy build.bat TRƯỚC).
; Build: mở file này bằng Inno Setup rồi Compile, hoặc build.bat tự gọi ISCC.

#define AppName "HoSoPDF"
#define AppVersion "1.0.0"

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

[Tasks]
Name: "desktopicon"; Description: "Tạo icon trên Desktop"; GroupDescription: "Tùy chọn:"

[Icons]
Name: "{group}\{#AppName}";        Filename: "{app}\launch.vbs"; WorkingDir: "{app}"; IconFilename: "{app}\app.ico"
Name: "{group}\Dừng {#AppName}";   Filename: "{app}\stop.vbs";   WorkingDir: "{app}"
Name: "{group}\Gỡ cài đặt {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}";  Filename: "{app}\launch.vbs"; WorkingDir: "{app}"; IconFilename: "{app}\app.ico"; Tasks: desktopicon

[Run]
Filename: "{app}\launch.vbs"; Description: "Khởi động {#AppName} ngay"; Flags: postinstall shellexec nowait skipifsilent

[UninstallRun]
; Dừng app trước khi gỡ để không khoá file.
Filename: "{app}\stop.vbs"; Flags: shellexec runhidden; RunOnceId: "stopapp"
