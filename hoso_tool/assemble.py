"""Cắt & ghép PDF bằng poppler (pdfseparate + pdfunite + pdfinfo).

Giữ nguyên chất lượng trang gốc (KHÔNG render lại thành ảnh để ghép).
"""
from __future__ import annotations

import contextlib
import functools
import logging
import os
import re
import shutil
import subprocess
import tempfile

logger = logging.getLogger("hoso")

# Ngưỡng tin cậy tối thiểu của Tesseract OSD — CHỈ dùng cho phát hiện trang
# xoay NGANG (90/270). Với 0 vs 180 ta dùng phương pháp đếm chữ OCR (bên dưới)
# vì OSD hay cho confidence thấp trên trang bảng/phụ lục → bỏ sót trang ngược.
OSD_MIN_CONFIDENCE = float(os.environ.get("HOSO_OSD_MIN_CONFIDENCE", "8"))

# Tham số phát hiện xoay 0/180 bằng OCR (đếm số từ đọc được ở mỗi chiều).
# Chiều nào ra nhiều chữ hơn rõ rệt là chiều đúng — ổn định cả trên trang chữ
# thưa (bảng vật liệu, phụ lục) nơi Tesseract OSD không đáng tin.
ORIENT_DPI = int(os.environ.get("HOSO_ORIENT_DPI", "120"))
ORIENT_MIN_WORDS = int(os.environ.get("HOSO_ORIENT_MIN_WORDS", "10"))
ORIENT_MARGIN = float(os.environ.get("HOSO_ORIENT_MARGIN", "1.3"))
ORIENT_MIN_DIFF = int(os.environ.get("HOSO_ORIENT_MIN_DIFF", "6"))


@functools.lru_cache(maxsize=1)
def _ensure_tesseract() -> bool:
    """Đảm bảo pytesseract + chương trình `tesseract` dùng được. Trả True nếu sẵn sàng.

    Tự dò các vị trí cài đặt phổ biến (macOS Homebrew, Linux, Windows) khi
    `tesseract` không nằm trên PATH — trường hợp hay gặp khi app được mở từ
    Finder hoặc bản đóng gói (PATH không có /opt/homebrew/bin).

    Log cảnh báo MỘT lần nếu không tìm thấy, để người dùng biết chức năng tự
    động sửa xoay đang bị TẮT thay vì âm thầm bỏ qua.
    """
    try:
        import pytesseract
    except ImportError:
        logger.warning(
            "pytesseract chưa được cài — TẮT tự động sửa xoay trang (OSD). "
            "Cài bằng: pip install pytesseract")
        return False
    cmd = shutil.which("tesseract")
    if not cmd:
        for cand in (
            "/opt/homebrew/bin/tesseract", "/usr/local/bin/tesseract",
            "/usr/bin/tesseract",
            r"C:\Program Files\Tesseract-OCR\tesseract.exe",
            r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
        ):
            if os.path.exists(cand):
                cmd = cand
                break
    if not cmd:
        logger.warning(
            "Không tìm thấy chương trình 'tesseract' — TẮT tự động sửa xoay trang (OSD). "
            "macOS: brew install tesseract · Ubuntu: apt install tesseract-ocr")
        return False
    pytesseract.pytesseract.tesseract_cmd = cmd
    return True


def _render_gray(page_pdf: str, dpi: int):
    """Render trang 1 của PDF thành ảnh xám PIL (tôn trọng /Rotate). None nếu lỗi."""
    import glob
    from PIL import Image
    with tempfile.TemporaryDirectory(dir=get_safe_temp_dir()) as d:
        root = os.path.join(d, "pg")
        subprocess.run(
            ["pdftoppm", "-gray", "-png", "-r", str(dpi), page_pdf, root],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        pngs = glob.glob(root + "*.png")
        if not pngs:
            return None
        return Image.open(pngs[0]).copy()


def _readable_word_count(img) -> int:
    """Số 'từ' Tesseract đọc được với confidence khá (>40) và dài >=3 ký tự.

    Là thước đo mức 'đọc được' của ảnh theo chiều hiện tại — ảnh đúng chiều sẽ
    cho nhiều từ hơn hẳn ảnh bị xoay ngược.
    """
    import pytesseract
    d = pytesseract.image_to_data(img, output_type=pytesseract.Output.DICT)
    n = 0
    for t, c in zip(d["text"], d["conf"]):
        try:
            conf = float(c)
        except (TypeError, ValueError):
            continue
        if conf > 40 and len(t.strip()) >= 3:
            n += 1
    return n


def _osd_rotation(img) -> tuple[int, float]:
    """(góc, độ_tin_cậy) từ Tesseract OSD. (0, 0.0) nếu lỗi/không xác định."""
    import pytesseract
    try:
        osd = pytesseract.image_to_osd(img)
    except Exception:
        return 0, 0.0
    rot_m = re.search(r"Rotate:\s*(\d+)", osd)
    conf_m = re.search(r"Orientation confidence:\s*([\d.]+)", osd)
    return (int(rot_m.group(1)) % 360 if rot_m else 0,
            float(conf_m.group(1)) if conf_m else 0.0)


def detect_page_rotation(page_pdf: str) -> int:
    """Đoán góc cần xoay (theo chiều kim đồng hồ) để trang nằm thẳng. 0 nếu đã thẳng.

    Chiến lược:
      1) 0 vs 180 — SO SÁNH SỐ CHỮ ĐỌC ĐƯỢC ở hai chiều (OCR). Chiều 180 thắng
         rõ rệt thì trang bị lộn ngược → trả 180. Cách này ổn định cả trên trang
         bảng/phụ lục chữ thưa (nơi OSD confidence thấp nên trước đây bỏ sót).
      2) Nếu quá ít chữ theo chiều ngang (trang xoay NGANG, hoặc trắng/ảnh) →
         nhờ Tesseract OSD; chỉ nhận 90/270 khi đủ tin cậy.
    Trả 0 khi OSD/OCR không sẵn sàng hoặc không xác định được (an toàn: không xoay).
    """
    if not _ensure_tesseract():
        return 0
    try:
        img = _render_gray(page_pdf, ORIENT_DPI)
        if img is None:
            return 0
        w0 = _readable_word_count(img)
        w180 = _readable_word_count(img.rotate(180, expand=True))
        if (w180 >= ORIENT_MIN_WORDS and w180 >= w0 * ORIENT_MARGIN
                and (w180 - w0) >= ORIENT_MIN_DIFF):
            return 180
        if w0 >= ORIENT_MIN_WORDS:
            return 0  # đủ chữ theo chiều đứng và không thua chiều 180 → đã thẳng
        # Ít chữ ngang: có thể trang xoay 90/270, hoặc trắng/ảnh → để OSD quyết.
        osd_rot, osd_conf = _osd_rotation(img)
        if osd_rot in (90, 270) and osd_conf >= OSD_MIN_CONFIDENCE:
            return osd_rot
        return 0
    except Exception as e:
        logger.debug("Phát hiện xoay không xử lý được %s: %s",
                     os.path.basename(page_pdf), e)
        return 0


def build_upright_pdf(src_pdf: str) -> tuple[str, dict[int, int]]:
    """Tạo bản PDF đã XOAY các trang về đúng chiều, để GỬI MODEL PHÂN LOẠI.

    Vì model (Gemini) đọc trang lộn ngược rất kém → gán nhãn sai; xoay đứng
    trước khi gửi giúp phân loại chính xác hơn hẳn.

    Trả về (đường_dẫn_pdf, {page_no: góc_đã_phát_hiện}). Góc này (OCR) cũng dùng
    lại cho bước ghép (lưu vào index) nên KHÔNG phải dò lại. Nếu không trang nào
    cần xoay (hoặc thiếu tesseract) → trả (src_pdf, {...} hoặc {}), giữ nguyên bản gốc.
    Caller nên xóa file trả về nếu nó KHÁC src_pdf.
    """
    if not _ensure_tesseract():
        return src_pdf, {}
    tmpdir = tempfile.mkdtemp(prefix="hoso_up_", dir=get_safe_temp_dir())
    try:
        pattern = os.path.join(tmpdir, "p-%d.pdf")
        with safe_input_path(src_pdf) as safe:
            subprocess.run(["pdfseparate", safe, pattern], check=True,
                           capture_output=True, encoding="utf-8", errors="ignore")
        pages: dict[int, str] = {}
        for fn in os.listdir(tmpdir):
            m = re.match(r"p-(\d+)\.pdf$", fn)
            if m:
                pages[int(m.group(1))] = os.path.join(tmpdir, fn)
        if not pages:
            return src_pdf, {}

        from pypdf import PdfReader, PdfWriter
        rotations: dict[int, int] = {}
        rebuilt: list[str] = []
        any_rot = False
        for i in sorted(pages):
            pf = pages[i]
            rot = detect_page_rotation(pf)
            rotations[i] = rot
            if rot:
                any_rot = True
                reader = PdfReader(pf)
                writer = PdfWriter()
                page = reader.pages[0]
                page.rotate(rot)
                writer.add_page(page)
                outp = os.path.join(tmpdir, f"r-{i}.pdf")
                with open(outp, "wb") as f:
                    writer.write(f)
                rebuilt.append(outp)
            else:
                rebuilt.append(pf)

        if not any_rot:
            return src_pdf, rotations  # đã đứng hết → dùng bản gốc, khỏi ghép lại

        fd, out_pdf = tempfile.mkstemp(suffix=".pdf", dir=get_safe_temp_dir())
        os.close(fd)
        subprocess.run(["pdfunite", *rebuilt, out_pdf], check=True,
                       capture_output=True, encoding="utf-8", errors="ignore")
        return out_pdf, rotations
    except Exception as e:
        logger.warning("Không tạo được bản PDF xoay đứng cho %s (%s) — gửi bản gốc.",
                       os.path.basename(src_pdf), e)
        return src_pdf, {}
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def get_safe_temp_dir() -> str:
    t = tempfile.gettempdir()
    if os.name == "nt" and any(ord(c) > 127 for c in t):
        for path in ["C:\\Temp", os.path.join(os.getcwd(), ".hoso_temp")]:
            try:
                os.makedirs(path, exist_ok=True)
                return path
            except Exception:
                pass
    return t


@contextlib.contextmanager
def safe_input_path(path: str):
    """Nếu đường dẫn chứa ký tự Unicode và chạy trên Windows, copy file sang một đường dẫn ASCII tạm thời."""
    if os.name == "nt" and any(ord(c) > 127 for c in path):
        fd, temp_path = tempfile.mkstemp(suffix=".pdf", dir=get_safe_temp_dir())
        os.close(fd)
        try:
            shutil.copyfile(path, temp_path)
            yield temp_path
        finally:
            try:
                os.remove(temp_path)
            except Exception:
                pass
    else:
        yield path


def page_count(pdf_path: str) -> int:
    with safe_input_path(pdf_path) as safe_path:
        out = subprocess.run(["pdfinfo", safe_path], capture_output=True, encoding="utf-8", errors="ignore", check=True)
        m = re.search(r"^Pages:\s+(\d+)", out.stdout, re.MULTILINE)
        if not m:
            raise RuntimeError(f"Không đọc được số trang: {pdf_path}")
        return int(m.group(1))


def has_text_layer(pdf_path: str, sample_chars: int = 20) -> bool:
    """True nếu PDF có lớp text (không phải scan thuần ảnh)."""
    with safe_input_path(pdf_path) as safe_path:
        out = subprocess.run(["pdftotext", safe_path, "-"], capture_output=True, encoding="utf-8", errors="ignore")
        return len(out.stdout.strip()) >= sample_chars


class PdfAssembler:
    """Tách từng file nguồn ra trang-đơn (cache), rồi ghép theo danh sách yêu cầu."""

    def __init__(self, workdir: str | None = None):
        self._owns_workdir = workdir is None
        self.workdir = workdir or tempfile.mkdtemp(prefix="hoso_asm_", dir=get_safe_temp_dir())
        self._separated: dict[str, dict[int, str]] = {}  # src_path -> {page_no: single_pdf}

    def _separate(self, src_pdf: str) -> dict[int, str]:
        if src_pdf in self._separated:
            return self._separated[src_pdf]
        import hashlib
        key_base = os.path.basename(src_pdf).encode("utf-8")
        key = hashlib.md5(key_base).hexdigest()
        outdir = os.path.join(self.workdir, key)
        os.makedirs(outdir, exist_ok=True)
        pattern = os.path.join(outdir, "p-%d.pdf")
        with safe_input_path(src_pdf) as safe_src:
            subprocess.run(["pdfseparate", safe_src, pattern], check=True,
                           capture_output=True, encoding="utf-8", errors="ignore")
        pages: dict[int, str] = {}
        for fn in os.listdir(outdir):
            m = re.match(r"p-(\d+)\.pdf$", fn)
            if m:
                pages[int(m.group(1))] = os.path.join(outdir, fn)
        self._separated[src_pdf] = pages
        return pages

    def build(self, out_path: str, pages: list[tuple[str, int, int]]) -> int:
        """Ghép `pages` = [(src_pdf, page_no, rotation), ...] theo đúng thứ tự -> out_path.

        Trả về số trang đã ghi. Bỏ qua (src, page) không tồn tại (đã log ở pipeline).
        """
        single_pdfs: list[str] = []
        for src_pdf, page_no, rotation in pages:
            sep = self._separate(src_pdf)
            if page_no in sep:
                page_file = sep[page_no]
                # Góc xoay đã được phát hiện (OCR) và lưu vào index ở bước phân loại
                # (build_upright_pdf) → chỉ cần áp dụng, KHÔNG dò lại (tránh OCR 2 lần).
                if rotation != 0:
                    try:
                        from pypdf import PdfReader, PdfWriter
                        reader = PdfReader(page_file)
                        writer = PdfWriter()
                        page = reader.pages[0]
                        # Quay trang theo chiều kim đồng hồ (cộng dồn vào /Rotate sẵn có)
                        page.rotate(rotation)
                        writer.add_page(page)

                        # Tạo một file tạm đã xoay
                        fd, rot_path = tempfile.mkstemp(suffix=".pdf", dir=get_safe_temp_dir())
                        os.close(fd)
                        with open(rot_path, "wb") as f:
                            writer.write(f)
                        page_file = rot_path
                    except Exception as e:
                        logger.error("Lỗi xoay trang %d của %s: %s", page_no, src_pdf, e)
                single_pdfs.append(page_file)
        if not single_pdfs:
            return 0
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        if len(single_pdfs) == 1:
            shutil.copyfile(single_pdfs[0], out_path)
        else:
            if os.name == "nt" and any(ord(c) > 127 for c in out_path):
                fd, temp_out = tempfile.mkstemp(suffix=".pdf", dir=get_safe_temp_dir())
                os.close(fd)
                try:
                    subprocess.run(["pdfunite", *single_pdfs, temp_out], check=True,
                                   capture_output=True, encoding="utf-8", errors="ignore")
                    shutil.copyfile(temp_out, out_path)
                finally:
                    try:
                        os.remove(temp_out)
                    except Exception:
                        pass
            else:
                subprocess.run(["pdfunite", *single_pdfs, out_path], check=True,
                               capture_output=True, encoding="utf-8", errors="ignore")
        return len(single_pdfs)

    def cleanup(self):
        if self._owns_workdir and os.path.isdir(self.workdir):
            shutil.rmtree(self.workdir, ignore_errors=True)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.cleanup()
