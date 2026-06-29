"""Xử lý 1 folder căn hộ.

Tách 2 nửa để UI chèn bước người sửa nhãn vào giữa:
  classify_folder()      -> chạy vision model, ghi _index.csv (nhãn từng trang)
  assemble_from_index()  -> đọc nhãn (đã có thể sửa tay) -> ghép 6 file + QA
process_folder() = ghép cả hai (cho CLI).
"""
from __future__ import annotations

import csv
import glob
import json
import os
from dataclasses import dataclass, field
from typing import Callable

from assemble import PdfAssembler, page_count
from classify import ClassifyResult

INDEX_NAME = "_index.csv"
USAGE_NAME = "_usage.json"


@dataclass
class FolderResult:
    folder: str
    prefix: str
    status: str = "ok"                       # ok | flagged | error
    reasons: list[str] = field(default_factory=list)
    outputs: list[str] = field(default_factory=list)
    categories_found: dict[str, int] = field(default_factory=dict)   # name -> số trang
    categories_missing: list[str] = field(default_factory=list)
    total_pages: int = 0                     # tổng trang mọi PDF nguồn
    classified_pages: int = 0                # số trang được gán loại (khác khong_thuoc)
    low_conf_pages: int = 0
    prompt_tokens: int = 0                   # token THẬT từ API (đầu vào)
    output_tokens: int = 0                   # token THẬT từ API (đầu ra)
    total_tokens: int = 0
    real_cost: float = 0.0                   # chi phí thực = token thật × đơn giá (config)
    error: str = ""


def _order_maps(categories: list[dict]):
    cat_name = {c["key"]: c["name"] for c in categories}
    cat_required = {c["key"]: c.get("required", False) for c in categories}
    sub_order: dict[str, dict[str, int]] = {}
    for c in categories:
        sub_order[c["key"]] = {s["id"]: i for i, s in enumerate(c.get("subtypes", []))}
    return cat_name, cat_required, sub_order


def list_pdfs(folder: str, out_subdir: str) -> list[str]:
    """PDF nguồn trong folder (loại trừ thư mục output), sắp theo tên cho thứ tự ổn định."""
    return sorted(
        p for p in glob.glob(os.path.join(folder, "*.pdf"))
        if os.path.basename(os.path.dirname(p)) != out_subdir
    )


def classify_folder(folder: str, config: dict,
                    classify: Callable[[str], ClassifyResult]) -> list[dict]:
    """Chạy classify cho mọi PDF, ghi _index.csv + _usage.json (token thật), trả list entry."""
    out_subdir = config.get("output_subdir", "output")
    pdfs = list_pdfs(folder, out_subdir)
    if not pdfs:
        raise RuntimeError("không có file PDF nào")

    entries: list[dict] = []
    usage = {"prompt_tokens": 0, "output_tokens": 0, "total_tokens": 0,
             "n_calls": 0, "model": config.get("model", "")}
    for fi, pdf in enumerate(pdfs):
        result = classify(pdf)
        for lb in result.labels:
            entries.append(dict(file_index=fi, file=os.path.basename(pdf), page=lb.page,
                                category=lb.category, subtype=lb.subtype,
                                confidence=lb.confidence, evidence=lb.evidence,
                                rotation=getattr(lb, "rotation", 0)))
        usage["prompt_tokens"] += result.prompt_tokens
        usage["output_tokens"] += result.output_tokens
        usage["total_tokens"] += result.total_tokens
        usage["n_calls"] += 1
    out_dir = os.path.join(folder, out_subdir)
    os.makedirs(out_dir, exist_ok=True)
    write_index(os.path.join(out_dir, INDEX_NAME), entries)
    with open(os.path.join(out_dir, USAGE_NAME), "w", encoding="utf-8") as f:
        json.dump(usage, f, ensure_ascii=False, indent=2)
    return entries


def assemble_from_index(folder: str, config: dict, entries: list[dict],
                        dry_run: bool = False) -> FolderResult:
    """Gom theo loại -> sắp thứ tự -> ghép 6 file + tính QA. Dùng cho cả CLI lẫn UI."""
    categories = config["categories"]
    threshold = float(config.get("confidence_threshold", 0.75))
    out_subdir = config.get("output_subdir", "output")
    cat_name, cat_required, sub_order = _order_maps(categories)

    prefix = os.path.basename(os.path.normpath(folder)).strip()
    res = FolderResult(folder=folder, prefix=prefix)
    out_dir = os.path.join(folder, out_subdir)
    pdfs = list_pdfs(folder, out_subdir)
    res.total_pages = sum(page_count(p) for p in pdfs)

    # Phát hiện model bỏ sót trang (so trang đã gán nhãn với số trang thật).
    for pdf in pdfs:
        name = os.path.basename(pdf)
        labeled = {e["page"] for e in entries if e["file"] == name}
        miss = sorted(set(range(1, page_count(pdf) + 1)) - labeled)
        if miss:
            res.reasons.append(f"{name}: thiếu nhãn trang {miss}")

    assembler = PdfAssembler()
    try:
        for c in categories:
            key, name = c["key"], c["name"]
            bucket = [e for e in entries if e["category"] == key]
            
            # Khắc phục lỗi LLM trả sai subtype: Nếu evidence HOẶC tên file chứa chữ "chuyển nhượng", "chuyen nhuong", "hdcn", "vbcn"...
            if key == "hop_dong":
                for e in bucket:
                    ev = str(e.get("evidence", "")).lower()
                    fname = str(e.get("file", "")).lower()
                    kw_list = ["chuyển nhượng", "chuyen nhuong", "hdcn", "vbcn", "sang nhượng", "sang nhuong"]
                    if any(k in ev for k in kw_list) or any(k in fname for k in kw_list):
                        e["subtype"] = "vb_chuyen_nhuong_hd"

            if not bucket:
                if cat_required.get(key):
                    res.categories_missing.append(name)
                continue
            order = sub_order.get(key, {})
            bucket.sort(key=lambda e: (order.get(e["subtype"], 999),
                                       e["file_index"], e["page"]))
            pages = [(os.path.join(folder, e["file"]), e["page"], int(e.get("rotation", 0))) for e in bucket]
            out_path = os.path.join(out_dir, f"{prefix}_{name}.pdf")
            if dry_run:
                written = len(pages)
            else:
                written = assembler.build(out_path, pages)
                res.outputs.append(out_path)
            res.categories_found[name] = written
            res.classified_pages += written
            if len({e["file_index"] for e in bucket}) > 1:
                res.reasons.append(f"'{name}' xuất hiện ở nhiều file (nghi trùng bản)")
    finally:
        assembler.cleanup()

    res.low_conf_pages = sum(
        1 for e in entries
        if e["category"] != "khong_thuoc" and float(e["confidence"]) < threshold)
    if res.low_conf_pages:
        res.reasons.append(f"{res.low_conf_pages} trang confidence < {threshold}")
    if res.categories_missing:
        res.reasons.append("thiếu loại: " + ", ".join(res.categories_missing))

    usage = read_usage(folder, config)
    if usage:
        res.prompt_tokens = int(usage.get("prompt_tokens", 0))
        res.output_tokens = int(usage.get("output_tokens", 0))
        res.total_tokens = int(usage.get("total_tokens", 0))
        res.real_cost = real_cost(usage, config)

    res.status = "flagged" if res.reasons else "ok"
    return res


def read_usage(folder: str, config: dict) -> dict | None:
    """Đọc _usage.json (token thật API trả về). None nếu chưa có."""
    p = os.path.join(folder, config.get("output_subdir", "output"), USAGE_NAME)
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    return None


def real_cost(usage: dict, config: dict) -> float:
    """Chi phí thực = token thật × đơn giá (USD/1 triệu token) trong config."""
    p_in = float(config.get("price_input_per_mtok", 0.30))
    p_out = float(config.get("price_output_per_mtok", 2.50))
    return usage.get("prompt_tokens", 0) / 1e6 * p_in \
        + usage.get("output_tokens", 0) / 1e6 * p_out


def process_folder(folder: str, config: dict,
                   classify: Callable[[str], ClassifyResult],
                   dry_run: bool = False) -> FolderResult:
    """CLI: classify + ghép một lượt. Lỗi -> trả FolderResult status=error (không sập batch)."""
    prefix = os.path.basename(os.path.normpath(folder)).strip()
    try:
        entries = classify_folder(folder, config, classify)
    except Exception as e:
        return FolderResult(folder=folder, prefix=prefix, status="error",
                            error=f"{type(e).__name__}: {e}")
    return assemble_from_index(folder, config, entries, dry_run=dry_run)


# ---- Đọc/ghi _index.csv (để UI sửa nhãn) ----
def write_index(path: str, entries: list[dict]):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["file", "page", "category", "subtype", "confidence", "evidence", "rotation"])
        for e in sorted(entries, key=lambda e: (e["file_index"], e["page"])):
            w.writerow([e["file"], e["page"], e["category"], e["subtype"],
                        f"{float(e['confidence']):.2f}", e["evidence"], e.get("rotation", 0)])


def read_index(folder: str, config: dict) -> list[dict]:
    """Đọc _index.csv -> entries (gắn lại file_index theo thứ tự PDF trong folder)."""
    out_subdir = config.get("output_subdir", "output")
    path = os.path.join(folder, out_subdir, INDEX_NAME)
    if not os.path.exists(path):
        return []
    file_index = {os.path.basename(p): i for i, p in enumerate(list_pdfs(folder, out_subdir))}
    entries: list[dict] = []
    with open(path, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            entries.append(dict(
                file_index=file_index.get(row["file"], 0), file=row["file"],
                page=int(row["page"]), category=row["category"],
                subtype=row.get("subtype", ""), confidence=float(row["confidence"] or 0),
                evidence=row.get("evidence", ""), rotation=int(row.get("rotation", 0) or 0)))
    return entries
