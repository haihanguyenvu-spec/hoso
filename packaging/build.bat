@echo off
REM ============================================================
REM  build.bat — đóng gói HoSoPDF thành bundle + installer (.exe)
REM  CHẠY TRÊN WINDOWS. Mở "Command Prompt", cd vào thư mục packaging, gõ: build.bat
REM  Cần: kết nối Internet (tải Python embeddable + poppler), và Inno Setup 6 (để ra .exe).
REM ============================================================
setlocal enabledelayedexpansion

REM ---- Phiên bản (chỉnh nếu cần) ----
set PYVER=3.12.7
set PYZIP=python-%PYVER%-embed-amd64.zip
set PYURL=https://www.python.org/ftp/python/%PYVER%/%PYZIP%
set POPVER=24.08.0-0
set POPZIP=Release-%POPVER%.zip
set POPURL=https://github.com/oschwartz10612/poppler-windows/releases/download/v%POPVER%/%POPZIP%
set GETPIP=https://bootstrap.pypa.io/get-pip.py

REM ---- Thư mục ----
set HERE=%~dp0
set ROOT=%HERE%..
set BUILD=%HERE%build
set DL=%BUILD%\_dl
set OUT=%BUILD%\HoSoPDF
set PY=%OUT%\python\python.exe

echo === Don build cu ===
if exist "%BUILD%" rmdir /s /q "%BUILD%"
mkdir "%DL%"  || goto :err
mkdir "%OUT%" || goto :err

echo === Tai Python embeddable %PYVER% ===
powershell -NoProfile -Command "Invoke-WebRequest -Uri '%PYURL%' -OutFile '%DL%\%PYZIP%'" || goto :err
powershell -NoProfile -Command "Expand-Archive -Force '%DL%\%PYZIP%' '%OUT%\python'" || goto :err

echo === Bat site-packages trong python._pth ===
powershell -NoProfile -Command "$f=Get-ChildItem '%OUT%\python\python*._pth' | Select-Object -First 1; (Get-Content $f.FullName) -replace '^#\s*import site','import site' | Set-Content $f.FullName" || goto :err

echo === Cai pip ===
powershell -NoProfile -Command "Invoke-WebRequest -Uri '%GETPIP%' -OutFile '%DL%\get-pip.py'" || goto :err
"%PY%" "%DL%\get-pip.py" --no-warn-script-location || goto :err

echo === Cai thu vien Python (wheel Windows) ===
"%PY%" -m pip install --no-warn-script-location -r "%ROOT%\hoso_tool\requirements.txt" || goto :err

echo === Tai poppler %POPVER% ===
powershell -NoProfile -Command "Invoke-WebRequest -Uri '%POPURL%' -OutFile '%DL%\%POPZIP%'" || goto :err
powershell -NoProfile -Command "Expand-Archive -Force '%DL%\%POPZIP%' '%DL%\poppler'" || goto :err
set POPSRC=
for /d %%D in ("%DL%\poppler\poppler-*") do set POPSRC=%%D
if not defined POPSRC ( echo Khong tim thay thu muc poppler sau giai nen & goto :err )
robocopy "!POPSRC!\Library" "%OUT%\poppler\Library" /e /njh /njs /ndl /nc /ns /np >nul
if errorlevel 8 goto :err

echo === Sao chep code app ===
robocopy "%ROOT%\hoso_tool" "%OUT%\app\hoso_tool" /e /xd __pycache__ output /xf .gemini_key .gemini_key_2 key.py /njh /njs /ndl /nc /ns /np >nul
if errorlevel 8 goto :err
robocopy "%ROOT%\.streamlit" "%OUT%\app\.streamlit" /e /xf secrets.toml /njh /njs /ndl /nc /ns /np >nul
if errorlevel 8 goto :err

echo === Xoa input_root mac dinh (path may khac) trong config ban dong goi ===
powershell -NoProfile -Command "$p='%OUT%\app\hoso_tool\config.yaml'; (Get-Content -Raw $p) -replace 'input_root:\s*\".*?\"','input_root: \"\"' | Set-Content -NoNewline $p" || goto :err

echo === Copy launcher ===
copy /y "%HERE%launch.vbs" "%OUT%\launch.vbs" >nul || goto :err
copy /y "%HERE%stop.vbs"   "%OUT%\stop.vbs"   >nul || goto :err

echo.
echo === Bundle xong: %OUT% ===

REM ---- Build installer .exe (tu cai Inno Setup neu may chua co) ----
call :find_iscc
if not defined ISCC (
  echo === Chua co Inno Setup -^> tu tai va cai im lang ^(co the hien 1 cua so UAC^) ===
  powershell -NoProfile -Command "Invoke-WebRequest -Uri 'https://jrsoftware.org/download.php/is.exe' -OutFile '%DL%\innosetup.exe'" || goto :err
  "%DL%\innosetup.exe" /VERYSILENT /SUPPRESSMSGBOXES /NORESTART /SP-
  call :find_iscc
)
if not defined ISCC (
  echo.
  echo [!] Cai Inno Setup khong thanh cong. Cai tay tu https://jrsoftware.org/isdl.php roi chay lai.
  goto :err
)

echo === Build installer bang Inno Setup ===
"%ISCC%" "%HERE%installer.iss" || goto :err
echo.
echo *** XONG! File cai dat: %HERE%dist\HoSoPDF_Setup.exe ***
goto :eof

:find_iscc
set ISCC=
for %%P in ("%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" "%ProgramFiles%\Inno Setup 6\ISCC.exe") do if exist "%%~P" set ISCC=%%~P
exit /b

:err
echo.
echo *** BUILD LOI — xem thong bao ben tren. ***
exit /b 1
