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

# Ngưỡng tin cậy tối thiểu của Tesseract OSD để CHẤP NHẬN tự động xoay.
# Thực nghiệm trên hồ sơ scan: phát hiện đúng thường có confidence > 12,
# còn nhiễu (trang bản vẽ, con dấu, chữ thưa) thường < 9. Đặt mặc định 8 để
# lọc nhiễu nhưng vẫn bắt được trang lệch thật. Có thể chỉnh qua biến môi trường.
OSD_MIN_CONFIDENCE = float(os.environ.get("HOSO_OSD_MIN_CONFIDENCE", "8"))


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


def detect_rotation_osd(page_pdf: str) -> tuple[int, float]:
    """Dùng Tesseract OSD đoán góc cần xoay để chữ nằm thẳng cho MỘT trang PDF.

    Render trang thành ảnh (tôn trọng /Rotate sẵn có) rồi chạy OSD.
    Trả về (góc_xoay, độ_tin_cậy). (0, 0.0) nếu OSD không sẵn sàng hoặc không
    xác định được (trang trắng, quá ít chữ, lỗi...).
    """
    if not _ensure_tesseract():
        return 0, 0.0
    try:
        import glob
        import pytesseract
        from PIL import Image
        with tempfile.TemporaryDirectory(dir=get_safe_temp_dir()) as ocr_tmp:
            img_p = os.path.join(ocr_tmp, "ocr_page")
            subprocess.run(
                ["pdftoppm", "-png", "-r", "150", page_pdf, img_p],
                check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            pngs = glob.glob(img_p + "*.png")
            if not pngs:
                return 0, 0.0
            osd = pytesseract.image_to_osd(Image.open(pngs[0]))
        rot_m = re.search(r"Rotate:\s*(\d+)", osd)
        conf_m = re.search(r"Orientation confidence:\s*([\d.]+)", osd)
        rot = int(rot_m.group(1)) % 360 if rot_m else 0
        conf = float(conf_m.group(1)) if conf_m else 0.0
        return rot, conf
    except Exception as e:
        # Trang quá ít chữ hay lỗi nhận diện: coi như không xác định được.
        logger.debug("OSD không xử lý được %s: %s", os.path.basename(page_pdf), e)
        return 0, 0.0


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
                # Fallback: khi Gemini KHÔNG báo góc xoay (rotation == 0), tự dùng
                # Tesseract OSD để phát hiện trang bị lệch. CHỈ ghi đè khi độ tin cậy
                # đủ cao — tránh nhiễu trên trang bản vẽ/con dấu làm hỏng trang vốn
                # đã thẳng. Không đụng tới góc Gemini đã báo (>0): thực nghiệm cho
                # thấy Gemini đáng tin hơn OSD trên đúng nhóm trang khó này.
                if rotation == 0:
                    osd_rot, osd_conf = detect_rotation_osd(page_file)
                    if osd_rot != 0 and osd_conf >= OSD_MIN_CONFIDENCE:
                        rotation = osd_rot
                        logger.info(
                            "OSD phát hiện trang %d của %s bị xoay (conf %.1f) — tự sửa: xoay %d độ",
                            page_no, os.path.basename(src_pdf), osd_conf, osd_rot)
                    elif osd_rot != 0:
                        logger.debug(
                            "OSD nghi trang %d của %s xoay %d nhưng conf %.1f < %.1f — bỏ qua",
                            page_no, os.path.basename(src_pdf), osd_rot, osd_conf, OSD_MIN_CONFIDENCE)

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
