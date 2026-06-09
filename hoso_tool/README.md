# hoso_tool — Phân loại & ghép hồ sơ PDF căn hộ

Tự động dò xuyên các file PDF (scan) trong mỗi folder căn hộ, phân loại từng trang bằng
vision model, rồi ghép thành **6 file chuẩn** đặt tên `<tên_folder>_<Tên loại>.pdf`.

## Kiến trúc
- `classify.py` — interface `Classifier` (tách-vendor). Mặc định `GeminiClassifier`
  (đọc PDF scan native qua Files API + structured JSON). `DeepSeekClassifier` là chỗ trống
  cho DeepSeek V4 Vision khi có model ID chính thức — chỉ cần hiện thực 1 class, không sửa pipeline.
- `assemble.py` — cắt/ghép bằng poppler (`pdfseparate` + `pdfunite`), giữ chất lượng gốc.
- `pipeline.py` — xử lý 1 folder: classify mọi PDF → gộp `(file,trang)→loại` → sắp thứ tự
  (văn bản chính trước, đính kèm sau) → ghép 6 file + QA.
- `run.py` — CLI batch: chạy song song nhiều folder, retry/backoff, resume, dry-run, báo cáo.
- `config.yaml` — định nghĩa 6 loại + sub-type + từ khóa tiếng Việt, model, ngưỡng QA.

## Cài đặt
```bash
python3 -m venv .venv
.venv/bin/pip install google-genai pyyaml
# Cần poppler: pdfinfo, pdfseparate, pdfunite (Ubuntu: apt install poppler-utils)
export GEMINI_API_KEY=...   # key đã bật billing
```

## Cấu trúc thư mục đầu vào
```
input_root/
  CR8-3_B4.01/                 # 1 căn hộ; TÊN folder = prefix
    *.pdf                      # các file hồ sơ scan
  CR8-3_B4.02/
    *.pdf
  ...
```
Output ghi vào `<folder>/output/`:
```
CR8-3_B4.01/output/
  CR8-3_B4.01_Đơn đăng ký biến động.pdf
  CR8-3_B4.01_Hợp đồng.pdf
  CR8-3_B4.01_Biên bản bàn giao.pdf
  CR8-3_B4.01_Thuế.pdf
  CR8-3_B4.01_Giấy ủy quyền.pdf
  CR8-3_B4.01_Thông tin Khách hàng.pdf
  _index.csv                   # nhãn từng trang để đối chiếu
  .done                        # marker đã xử lý (cho resume)
```

## Cách dùng
```bash
PY=.venv/bin/python

# 1) Calibrate 1 folder mẫu (xem _index.csv + 6 file output):
$PY hoso_tool/run.py --folder input_root/CR8-3_B4.01

# 2) Dry-run cả batch: chỉ classify + ước tính chi phí, KHÔNG ghép:
$PY hoso_tool/run.py --input-root input_root --dry-run

# 3) Chạy thật cả batch (song song 4 luồng, tự resume folder đã xong):
$PY hoso_tool/run.py --input-root input_root --workers 4

# Chạy lại tất cả (bỏ qua marker .done):
$PY hoso_tool/run.py --input-root input_root --force
```

## Giao diện web (UI) — khuyến nghị cho người không rành terminal
```bash
.venv/bin/pip install streamlit            # (đã cài nếu theo bước trên)
.venv/bin/streamlit run hoso_tool/app.py   # mở http://localhost:8501
```
3 tab:
- **① Phân loại**: chọn thư mục gốc → xem danh sách folder + trạng thái → chọn folder → bấm *Phân loại* (chạy vision model, có thanh tiến độ).
- **② Review & Sửa nhãn**: bảng nhãn từng trang, **dropdown sửa loại**, lọc "chỉ hiện trang cần kiểm", xem trang phóng to bên phải. Bấm *Lưu nhãn* rồi *Tạo 6 file PDF* (ghép theo nhãn ĐÃ SỬA) + nút tải file.
- **③ Tổng kết**: đọc `_review/summary.csv`, lọc nhanh folder cần chú ý.

API key: UI tự đọc từ biến môi trường `GEMINI_API_KEY`, hoặc file `key.py`/`.gemini_key`, hoặc ô nhập trong sidebar. Mọi xử lý chạy local; ảnh chỉ rời máy ở bước "Phân loại".

> Backend dùng chung với CLI: `pipeline.classify_folder()` (chạy model, ghi `_index.csv`) và
> `pipeline.assemble_from_index()` (ghép theo nhãn đã sửa). UI chỉ là lớp mỏng trên 2 hàm này.

## QA (kiểm tra của con người)
- `input_root/_review/summary.csv` — mỗi folder 1 dòng: status (ok/flagged/error),
  `sample_check=yes` (~5% folder ok được chọn ngẫu nhiên để soi tay), trang thiếu, lý do flag,
  chi phí ước tính.
- Folder **flagged** khi: thiếu loại bắt buộc, model bỏ sót trang, nghi trùng bản giữa các file,
  hoặc có trang `confidence < confidence_threshold`. **Không tạo file rỗng** cho loại thiếu.
- `<folder>/output/_index.csv` — nhãn từng trang (file, trang, loại, subtype, confidence, evidence).

## Đổi sang model khác
Sửa `provider` / `model` trong `config.yaml` (hoặc `--model`). Để dùng DeepSeek V4 Vision:
hiện thực `DeepSeekClassifier.classify()` trong `classify.py` rồi đặt `provider: deepseek`.

## Lưu ý quyền riêng tư
Hồ sơ chứa PII (CCCD, hộ chiếu, GCN kết hôn) được gửi lên API của nhà cung cấp model.
Gemini API trả phí (paid tier) **không dùng dữ liệu để huấn luyện**. Cân nhắc trước khi chạy.
