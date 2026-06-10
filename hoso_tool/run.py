#!/usr/bin/env python3
"""CLI batch: phân loại & ghép hồ sơ PDF cho nhiều folder căn hộ.

Ví dụ:
  # Calibrate 1 folder:
  python run.py --folder /home/tinphan/workspace/haiha/CR8-3_B4.01
  # Dry-run cả batch (chỉ classify + ước tính chi phí, không ghép):
  python run.py --input-root /path/to/root --dry-run
  # Chạy thật cả batch:
  python run.py --input-root /path/to/root --workers 4
"""
from __future__ import annotations

import argparse
import csv
import glob
import logging
import os
import random
import time
# Không dùng ThreadPoolExecutor nữa — chạy tuần tự để tránh API quá tải

import yaml

from classify import make_classifier
from pipeline import FolderResult, process_folder

# Ước tính token/trang theo media_resolution của Gemini (ảnh scan).
_TOK_PER_PAGE = {"low": 280, "medium": 560, "high": 1120}
# Giá input USD / 1 triệu token (ước tính 6/2026; chỉnh trong config nếu cần).
_PRICE_INPUT = {
    "flash-lite": 0.10, "flash": 0.30, "pro": 1.25,
}
log = logging.getLogger("hoso")


def load_config(path: str, overrides: dict) -> dict:
    with open(path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    for k, v in overrides.items():
        if v is not None:
            cfg[k] = v
    return cfg


def price_per_mtok(model: str, cfg: dict) -> float:
    if cfg.get("price_per_mtok_input") is not None:
        return float(cfg["price_per_mtok_input"])
    m = model.lower()
    for tag, price in _PRICE_INPUT.items():
        if tag in m:
            return price
    return 0.30  # mặc định ~ Flash


def est_cost(total_pages: int, cfg: dict) -> float:
    tok = total_pages * _TOK_PER_PAGE.get(cfg.get("media_resolution", "medium"), 560)
    return tok / 1e6 * price_per_mtok(cfg["model"], cfg)


def discover_folders(input_root: str, output_subdir: str) -> list[str]:
    """Subfolder chứa >=1 PDF trực tiếp = 1 folder căn hộ."""
    out = []
    for name in sorted(os.listdir(input_root)):
        d = os.path.join(input_root, name)
        if not os.path.isdir(d) or name in (output_subdir, "_review"):
            continue
        if glob.glob(os.path.join(d, "*.pdf")):
            out.append(d)
    return out


def make_retrying_classify(classifier, max_retries: int):
    def _classify(pdf_path: str):
        delay = 3.0
        actual_max = max(max_retries, 6)
        for attempt in range(1, actual_max + 1):
            try:
                return classifier.classify(pdf_path)
            except Exception as e:
                err_msg = str(e)
                is_transient = any(term in err_msg for term in ["503", "UNAVAILABLE", "high demand", "ResourceExhausted", "quota", "504", "502"])
                
                # If it's not a transient error, or if we have exhausted all retries
                if attempt == actual_max or (not is_transient and attempt == max_retries):
                    raise
                
                # Jitter: add random delay to avoid request collision
                sleep_time = delay + random.uniform(1.0, 3.0)
                
                # Log warning
                log.warning("classify lỗi (%s) lần %d, thử lại sau %.1fs: %s",
                            os.path.basename(pdf_path), attempt, sleep_time, err_msg)
                
                # UI Notification (toast) if run under Streamlit
                try:
                    import streamlit as st
                    st.toast(f"⚠️ API bận ({os.path.basename(pdf_path)}), đang tự thử lại lần {attempt}/{actual_max} sau {sleep_time:.1f}s...")
                except Exception:
                    pass
                
                time.sleep(sleep_time)
                delay *= 2.0
    return _classify



def main():
    ap = argparse.ArgumentParser(description="Phân loại & ghép hồ sơ PDF căn hộ")
    ap.add_argument("--config", default=os.path.join(os.path.dirname(__file__), "config.yaml"))
    ap.add_argument("--input-root")
    ap.add_argument("--folder", help="Chỉ xử lý 1 folder căn hộ (calibrate)")
    ap.add_argument("--model")
    ap.add_argument("--workers", type=int)
    ap.add_argument("--confidence-threshold", type=float, dest="confidence_threshold")
    ap.add_argument("--media-resolution", dest="media_resolution", choices=["low", "medium", "high"])
    ap.add_argument("--dry-run", action="store_true", help="Chỉ classify + ước tính, không ghép")
    ap.add_argument("--force", action="store_true", help="Bỏ qua marker .done, chạy lại tất cả")
    args = ap.parse_args()

    cfg = load_config(args.config, dict(
        input_root=args.input_root, model=args.model, workers=args.workers,
        confidence_threshold=args.confidence_threshold,
        media_resolution=args.media_resolution,
    ))

    if args.folder:
        folders = [os.path.normpath(args.folder)]
        input_root = os.path.dirname(folders[0])
    else:
        input_root = cfg["input_root"]
        folders = discover_folders(input_root, cfg.get("output_subdir", "output"))

    review_dir = os.path.join(input_root, "_review")
    os.makedirs(review_dir, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(), logging.FileHandler(os.path.join(review_dir, "run.log"))])

    out_subdir = cfg.get("output_subdir", "output")
    if not args.force:
        before = len(folders)
        folders = [f for f in folders if not os.path.exists(os.path.join(f, out_subdir, ".done"))]
        if before - len(folders):
            log.info("Resume: bỏ qua %d folder đã xong", before - len(folders))

    log.info("Xử lý %d folder | provider=%s model=%s | dry_run=%s",
             len(folders), cfg.get("provider"), cfg.get("model"), args.dry_run)
    if not folders:
        log.info("Không có folder nào để xử lý.")
        return

    try:
        import json
        if os.name == "nt":
            base = os.environ.get("APPDATA") or os.path.expanduser("~")
            d = os.path.join(base, "HoSoPDF")
        else:
            base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
            d = os.path.join(base, "hoso_tool")
        p = os.path.join(d, "keys.json")
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                keys = json.load(f)
                if isinstance(keys, list):
                    cfg["api_keys"] = [k.strip() for k in keys if k.strip()]
    except Exception:
        pass
    classifier = make_classifier(cfg)
    classify = make_retrying_classify(classifier, int(cfg.get("max_retries", 4)))

    results: list[FolderResult] = []
    n_total = len(folders)
    for i, f in enumerate(folders, 1):
        prefix = os.path.basename(os.path.normpath(f))
        log.info("[%d/%d] Bắt đầu xử lý folder: %s", i, n_total, prefix)
        r = process_folder(f, cfg, classify, args.dry_run)
        results.append(r)
        log.info("[%d/%d] [%s] %s | %d/%d trang | %s",
                 i, n_total, r.status.upper(), r.prefix,
                 r.classified_pages, r.total_pages,
                 "; ".join(r.reasons) if r.reasons else "OK")
        if r.status in ("ok", "flagged") and not args.dry_run:
            open(os.path.join(r.folder, out_subdir, ".done"), "w").close()

    write_summary(os.path.join(review_dir, "summary.csv"), results, cfg)
    _print_aggregate(results, cfg, args.dry_run)


def write_summary(path: str, results: list[FolderResult], cfg: dict):
    sample_rate = float(cfg.get("sample_rate", 0.05))
    ok_idx = [i for i, r in enumerate(results) if r.status == "ok"]
    k = max(1, round(len(ok_idx) * sample_rate)) if ok_idx else 0
    sampled = set(random.sample(ok_idx, k=k)) if ok_idx else set()
    sampled_folders = {id(results[i]) for i in sampled}
    results = sorted(results, key=lambda r: r.prefix)
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["folder", "status", "sample_check", "total_pages", "classified_pages",
                    "low_conf", "missing", "est_cost_usd", "real_tokens", "real_cost_usd",
                    "reasons"])
        for r in results:
            w.writerow([
                r.prefix, r.status, "yes" if id(r) in sampled_folders else "",
                r.total_pages, r.classified_pages, r.low_conf_pages,
                ", ".join(r.categories_missing), f"{est_cost(r.total_pages, cfg):.4f}",
                r.total_tokens, f"{r.real_cost:.4f}",
                " | ".join(r.reasons) or r.error,
            ])
    log.info("Đã ghi summary: %s", path)


def _print_aggregate(results: list[FolderResult], cfg: dict, dry_run: bool):
    n = len(results)
    by = lambda s: sum(1 for r in results if r.status == s)
    pages = sum(r.total_pages for r in results)
    est = sum(est_cost(r.total_pages, cfg) for r in results)
    real_tok = sum(r.total_tokens for r in results)
    real = sum(r.real_cost for r in results)
    log.info("==== TỔNG KẾT ====")
    log.info("Folder: %d (ok=%d, flagged=%d, error=%d)", n, by("ok"), by("flagged"), by("error"))
    log.info("Tổng trang: %d | Ước tính trước: ~$%.2f (%s)", pages, est, cfg.get("model"))
    log.info("TOKEN THẬT: %s | CHI PHÍ THỰC: $%.4f (đơn giá in=$%.2f out=$%.2f /1M)",
             f"{real_tok:,}", real,
             float(cfg.get("price_input_per_mtok", 0.30)),
             float(cfg.get("price_output_per_mtok", 2.50)))
    log.info("(Số tiền chính xác cuối cùng xem ở Google AI Studio → Usage.)")

    # Dự phóng cho toàn bộ dự án dựa trên các folder vừa chạy thật.
    ran = [r for r in results if r.total_tokens > 0]
    target = int(cfg.get("project_total_folders", 0) or 0)
    if ran and target:
        avg_cost = real / len(ran)
        avg_pages = sum(r.total_pages for r in ran) / len(ran)
        log.info("DỰ PHÓNG: TB $%.4f/folder (~%.0f trang/folder, đo trên %d folder) "
                 "→ %d folder ≈ $%.2f", avg_cost, avg_pages, len(ran), target, avg_cost * target)
    if dry_run:
        log.info("(dry-run: chưa ghép file. Bỏ --dry-run để xuất PDF.)")


if __name__ == "__main__":
    main()
