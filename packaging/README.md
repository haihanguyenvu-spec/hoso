# Đóng gói HoSoPDF thành file cài đặt Windows (.exe)

Tạo `HoSoPDF_Setup.exe` để người dùng cài kiểu **Next → Next → Finish**, có icon Desktop,
double-click là tự mở trình duyệt vào app. Không cần cài Python, poppler hay Tesseract trên máy đích.

## Cần chuẩn bị (chỉ trên MÁY BUILD)
- Windows 64-bit, có kết nối Internet.

Không cần cài sẵn gì thêm: `build.bat` tự tải Python embeddable, poppler, Tesseract, **và cả Inno Setup**
(nếu máy chưa có) rồi cài im lặng.

## Cách build — chỉ 1 lệnh
```bat
REM Mở Command Prompt, vào thư mục packaging của project:
cd <đường-dẫn-project>\packaging
build.bat
```
Lần đầu, nếu máy chưa có Inno Setup, script sẽ tự tải và cài — có thể hiện **1 cửa sổ UAC**
xin quyền admin, bấm **Yes**. Các lần sau không hỏi nữa.
Script tự động:
1. Tải Python 3.12 embeddable → `build\HoSoPDF\python\`, bật site-packages, cài pip.
2. `pip install -r hoso_tool\requirements.txt` (lấy wheel Windows: streamlit, google-genai, pandas...).
3. Tải poppler-windows → `build\HoSoPDF\poppler\Library\bin\` (pdfinfo, pdftoppm, pdfseparate, pdfunite, pdftotext).
4. Tải Tesseract (bản Windows chính thức) → `build\HoSoPDF\tesseract\` — dùng cho **tự động sửa trang
   xoay/lộn ngược**. Bộ cài NSIS được **giải nén** bằng 7-Zip (lấy từ `7zr.exe`, không cài gì vào
   máy build). Bộ cài này KHÔNG kèm dữ liệu nhận dạng nên script tải thêm `eng` + `osd`
   (`tessdata_fast`), rồi chạy thử `tesseract --list-langs` — thiếu `osd` là dừng build.
5. Tải **Microsoft Visual C++ Runtime** (`vc_redist.x64.exe`) → bộ cài tự cài im lặng kèm theo.
   poppler cần `MSVCP140.dll`/`VCRUNTIME140.dll`; máy chưa có bộ này thì MỌI thao tác PDF hỏng
   (máy build thường đã có sẵn nên không lộ lỗi khi tự test).
6. Copy code app → `build\HoSoPDF\app\` (bỏ `__pycache__`, `output`, các file key) và xoá `input_root` mặc định.
7. Copy `launcher.py` + `stop.py`.
8. Nếu có Inno Setup → gọi `ISCC` build `dist\HoSoPDF_Setup.exe`.

Kết quả:
- `packaging\build\HoSoPDF\` — bundle chạy được (test nhanh: `python\pythonw.exe launcher.py` trong thư mục đó).
- `packaging\dist\HoSoPDF_Setup.exe` — file gửi cho người dùng.

## Người dùng cuối trải nghiệm
1. Tải `HoSoPDF_Setup.exe` → chạy wizard cài đặt. File chưa ký số nên Windows hiện
   *"Windows protected your PC"* → bấm **More info** → **Run anyway**.
2. Double-click icon **HoSoPDF** trên Desktop → trình duyệt tự mở `http://127.0.0.1:8517`
   khi app sẵn sàng (lần đầu có thể chờ ~30 giây). Bấm icon lần nữa khi app đang chạy = chỉ mở lại trình duyệt.
3. Lần đầu: **⚙️ Cài đặt → API Keys** → dán key → **💾 Lưu API keys** (lưu ở `%APPDATA%\HoSoPDF`, lần sau tự nhận).
4. Dán đường dẫn thư mục gốc chứa các folder căn hộ (lấy từ thanh địa chỉ File Explorer) → Enter.
   App nhớ thư mục này cho lần mở sau.
5. Muốn tắt app nền: chạy **Dừng HoSoPDF** trong Start Menu (hoặc tắt máy).

Tờ hướng dẫn dành cho người dùng (PDF, 5 trang, có ảnh từng bước) được cài kèm và mở được từ
Start Menu → HoSoPDF → **Hướng dẫn sử dụng**. Có thể gửi riêng file `huongdan\HuongDan_HoSoPDF.pdf`
cùng với `HoSoPDF_Setup.exe`.

## Cấu trúc bundle
```
HoSoPDF/
├── python/              Python 3.12 embeddable + thư viện (Lib\site-packages)
├── poppler/Library/bin/ pdfinfo, pdftoppm, pdfseparate, pdfunite, pdftotext
├── tesseract/           tesseract.exe + DLL, tessdata\ (eng, osd) — sửa trang xoay
├── app/
│   ├── hoso_tool/       code app (config input_root để trống)
│   └── .streamlit/      config.toml (ẩn chrome Streamlit)
├── HuongDan.pdf         tờ hướng dẫn cho người dùng (chép từ huongdan\HuongDan_HoSoPDF.pdf)
├── launcher.py          (chạy bằng python\pythonw.exe) đặt PATH + TESSDATA_PREFIX, chạy Streamlit ẩn,
│                       chờ server lên rồi mở browser; lỗi thì hiện hộp thoại kèm nhật ký
└── stop.py              dừng đúng tiến trình python của bản cài này
```

## Ghi chú
- **API key** lưu ở `%APPDATA%\HoSoPDF\.gemini_key` (và `.gemini_key_2`) — không nằm trong thư mục
  cài, nên app cài ở `Program Files` vẫn lưu được mà không cần quyền admin lúc chạy.
- **Output** (6 file PDF, `_index.csv`, `.done`) ghi vào chính các folder căn hộ của người dùng,
  không đụng vào thư mục cài.
- Đổi phiên bản Python/poppler/Tesseract: sửa `PYVER` / `POPVER` / `TESSVER` + `TESSEXE` đầu file `build.bat`.
- **Sửa tờ hướng dẫn:** sửa `huongdan\huongdan.html` (ảnh trong `huongdan\img\`), rồi mở file đó bằng Chrome →
  `Ctrl+P` → *Save as PDF* (khổ A4) và lưu đè `huongdan\HuongDan_HoSoPDF.pdf`. Ảnh chụp từ giao diện
  ở phiên bản 1.1.x (dữ liệu mẫu) — nếu giao diện đổi nhiều thì chụp lại ảnh cho khớp.
- **Launcher bằng Python, không dùng VBScript**: Microsoft đang gỡ dần VBScript khỏi Windows.
  Server chỉ nghe ở `127.0.0.1:8517` (không bật hộp thoại Firewall, máy khác trong LAN không mở
  được app chứa API key); cổng 8517 thay cho 8501 để không đụng app Streamlit khác. Nhật ký
  khởi động: `%APPDATA%\HoSoPDF\app_log.txt`.
- Mục "Khởi động HoSoPDF ngay" cuối bộ cài chạy bằng **tài khoản người dùng** (`runasoriginaluser`):
  nếu chạy bằng quyền admin của bộ cài, app không thấy ổ mạng / ổ Google Drive (G:) và lưu API
  key vào hồ sơ admin.
- `build.bat` phải giữ xuống dòng **CRLF** (đã ép bằng `.gitattributes`): `cmd.exe` đọc
  file `.bat` chỉ có LF có thể nhảy sai nhãn `goto`/`call`.
- Nâng phiên bản app hiển thị trong installer: sửa `AppVersion` trong `installer.iss`.
