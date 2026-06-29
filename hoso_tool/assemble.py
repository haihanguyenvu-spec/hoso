"""Cắt & ghép PDF bằng poppler (pdfseparate + pdfunite + pdfinfo).

Giữ nguyên chất lượng trang gốc (KHÔNG render lại thành ảnh để ghép).
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile


import contextlib

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
        key = re.sub(r"[^0-9A-Za-z]+", "_", os.path.basename(src_pdf))[:40]
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
                # Fallback: Tự động chạy Tesseract OSD nếu không có metadata rotation
                if rotation == 0:
                    try:
                        import pytesseract
                        from PIL import Image
                        # Render trang đơn này thành file ảnh tạm thời để OSD nhận diện
                        with tempfile.TemporaryDirectory(dir=get_safe_temp_dir()) as ocr_tmp:
                            img_p = os.path.join(ocr_tmp, "ocr_page")
                            subprocess.run(
                                ["pdftoppm", "-png", "-r", "150", page_file, img_p],
                                check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                            )
                            # Tìm file png được render ra
                            import glob
                            pngs = glob.glob(img_p + "*.png")
                            if pngs:
                                osd = pytesseract.image_to_osd(Image.open(pngs[0]))
                                m = re.search(r"Rotate:\s*(\d+)", osd)
                                if m:
                                    tess_rot = int(m.group(1))
                                    if tess_rot != 0:
                                        rotation = tess_rot
                                        import logging
                                        logging.getLogger("hoso").info(
                                            f"Tesseract OCR phát hiện trang {page_no} của {os.path.basename(src_pdf)} bị xoay lệch, tự động sửa: xoay {tess_rot} độ"
                                        )
                    except Exception as ocr_err:
                        # Bỏ qua nếu không có tesseract hoặc lỗi nhận diện
                        pass

                if rotation != 0:
                    try:
                        from pypdf import PdfReader, PdfWriter
                        reader = PdfReader(page_file)
                        writer = PdfWriter()
                        page = reader.pages[0]
                        # Quay trang theo chiều kim đồng hồ
                        page.rotate(rotation)
                        writer.add_page(page)
                        
                        # Tạo một file tạm đã xoay
                        fd, rot_path = tempfile.mkstemp(suffix=".pdf", dir=get_safe_temp_dir())
                        os.close(fd)
                        with open(rot_path, "wb") as f:
                            writer.write(f)
                        page_file = rot_path
                    except Exception as e:
                        import logging
                        logging.getLogger("hoso").error(f"Lỗi xoay trang {page_no} của {src_pdf}: {e}")
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
