"""Cắt & ghép PDF bằng poppler (pdfseparate + pdfunite + pdfinfo).

Giữ nguyên chất lượng trang gốc (KHÔNG render lại thành ảnh để ghép).
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile


def page_count(pdf_path: str) -> int:
    out = subprocess.run(["pdfinfo", pdf_path], capture_output=True, encoding="utf-8", errors="ignore", check=True)
    m = re.search(r"^Pages:\s+(\d+)", out.stdout, re.MULTILINE)
    if not m:
        raise RuntimeError(f"Không đọc được số trang: {pdf_path}")
    return int(m.group(1))


def has_text_layer(pdf_path: str, sample_chars: int = 20) -> bool:
    """True nếu PDF có lớp text (không phải scan thuần ảnh)."""
    out = subprocess.run(["pdftotext", pdf_path, "-"], capture_output=True, encoding="utf-8", errors="ignore")
    return len(out.stdout.strip()) >= sample_chars


class PdfAssembler:
    """Tách từng file nguồn ra trang-đơn (cache), rồi ghép theo danh sách yêu cầu."""

    def __init__(self, workdir: str | None = None):
        self._owns_workdir = workdir is None
        self.workdir = workdir or tempfile.mkdtemp(prefix="hoso_asm_")
        self._separated: dict[str, dict[int, str]] = {}  # src_path -> {page_no: single_pdf}

    def _separate(self, src_pdf: str) -> dict[int, str]:
        if src_pdf in self._separated:
            return self._separated[src_pdf]
        key = re.sub(r"[^0-9A-Za-z]+", "_", os.path.basename(src_pdf))[:40]
        outdir = os.path.join(self.workdir, key)
        os.makedirs(outdir, exist_ok=True)
        pattern = os.path.join(outdir, "p-%d.pdf")
        subprocess.run(["pdfseparate", src_pdf, pattern], check=True,
                       capture_output=True, encoding="utf-8", errors="ignore")
        pages: dict[int, str] = {}
        for fn in os.listdir(outdir):
            m = re.match(r"p-(\d+)\.pdf$", fn)
            if m:
                pages[int(m.group(1))] = os.path.join(outdir, fn)
        self._separated[src_pdf] = pages
        return pages

    def build(self, out_path: str, pages: list[tuple[str, int]]) -> int:
        """Ghép `pages` = [(src_pdf, page_no), ...] theo đúng thứ tự -> out_path.

        Trả về số trang đã ghi. Bỏ qua (src, page) không tồn tại (đã log ở pipeline).
        """
        single_pdfs: list[str] = []
        for src_pdf, page_no in pages:
            sep = self._separate(src_pdf)
            if page_no in sep:
                single_pdfs.append(sep[page_no])
        if not single_pdfs:
            return 0
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        if len(single_pdfs) == 1:
            shutil.copyfile(single_pdfs[0], out_path)
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
