# Đóng gói HoSoPDF thành file cài đặt Windows (.exe)

Tạo `HoSoPDF_Setup.exe` để người dùng cài kiểu **Next → Next → Finish**, có icon Desktop,
double-click là tự mở trình duyệt vào app. Không cần cài Python hay poppler trên máy đích.

## Cần chuẩn bị (chỉ trên MÁY BUILD)
- Windows 64-bit, có kết nối Internet.

Không cần cài sẵn gì thêm: `build.bat` tự tải Python embeddable, poppler, **và cả Inno Setup**
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
4. Copy code app → `build\HoSoPDF\app\` (bỏ `__pycache__`, `output`, các file key) và xoá `input_root` mặc định.
5. Copy `launch.vbs` + `stop.vbs`.
6. Nếu có Inno Setup → gọi `ISCC` build `dist\HoSoPDF_Setup.exe`.

Kết quả:
- `packaging\build\HoSoPDF\` — bundle chạy được (test nhanh: double-click `launch.vbs`).
- `packaging\dist\HoSoPDF_Setup.exe` — file gửi cho người dùng.

## Người dùng cuối trải nghiệm
1. Tải `HoSoPDF_Setup.exe` → chạy wizard cài đặt.
2. Double-click icon **HoSoPDF** trên Desktop → trình duyệt tự mở `http://localhost:8501`.
3. Lần đầu: dán **Gemini API key** vào ô sidebar → bấm **💾 Lưu** (key lưu ở `%APPDATA%\HoSoPDF`, lần sau tự nhận).
4. Nhập đường dẫn thư mục gốc chứa các folder căn hộ → dùng bình thường.
5. Muốn tắt app nền: chạy **Dừng HoSoPDF** trong Start Menu (hoặc tắt máy).

## Cấu trúc bundle
```
HoSoPDF/
├── python/              Python 3.12 embeddable + thư viện (Lib\site-packages)
├── poppler/Library/bin/ pdfinfo, pdftoppm, pdfseparate, pdfunite, pdftotext
├── app/
│   ├── hoso_tool/       code app (config input_root để trống)
│   └── .streamlit/      config.toml (ẩn chrome Streamlit)
├── launch.vbs           chạy Streamlit ẩn console + mở browser
└── stop.vbs             dừng đúng tiến trình python của bản cài này
```

## Ghi chú
- **API key** lưu ở `%APPDATA%\HoSoPDF\.gemini_key` (và `.gemini_key_2`) — không nằm trong thư mục
  cài, nên app cài ở `Program Files` vẫn lưu được mà không cần quyền admin lúc chạy.
- **Output** (6 file PDF, `_index.csv`, `.done`) ghi vào chính các folder căn hộ của người dùng,
  không đụng vào thư mục cài.
- Đổi phiên bản Python/poppler: sửa `PYVER` / `POPVER` đầu file `build.bat`.
- Nâng phiên bản app hiển thị trong installer: sửa `AppVersion` trong `installer.iss`.
