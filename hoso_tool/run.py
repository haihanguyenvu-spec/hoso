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

from classify import _is_transient, make_classifier
from pipeline import FolderResult, process_folder

# Ước tính token/trang theo media_resolution của Gemini (ảnh scan).
_TOK_PER_PAGE = {"low": 280, "medium": 560, "high": 1120}
log = logging.getLogger("hoso")


def load_config(path: str, overrides: dict) -> dict:
    with open(path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    for k, v in overrides.items():
        if v is not None:
            cfg[k] = v
    return cfg


def est_tokens(total_pages: int, cfg: dict) -> int:
    """Ước tính SỐ TOKEN đầu vào theo số trang (không quy ra tiền).

    Tool cố tình KHÔNG tự tính tiền nữa: token của các lượt gọi hỏng
    (timeout/429) không bao giờ về tới ta, nên mọi con số tiền tự tính đều là mức
    sàn và gây hiểu nhầm. Số tiền đúng chỉ có ở Google AI Studio → Usage.
    """
    return total_pages * _TOK_PER_PAGE.get(cfg.get("media_resolution", "medium"), 560)


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


def make_retrying_classify(classifier, max_retries: int, notify=None):
    """Bọc classifier: thử lại khi lỗi TẠM THỜI (503/quota/timeout), backoff luỹ thừa.

    Lỗi KHÔNG tạm thời (sai key, lỗi mã hoá tên file, lỗi code...) ném ra NGAY.
    Thử lại mấy loại đó chỉ tốn thêm ~30s rồi vẫn hỏng, mà làm folder đứng lâu vô ích.

    `notify(text)` (tùy chọn) cho giao diện báo "đang chờ thử lại" — cố ý không
    import streamlit ở đây để run.py vẫn chạy thuần CLI được.
    """
    def _classify(pdf_path: str):
        delay = 3.0
        name = os.path.basename(pdf_path)
        for attempt in range(1, max_retries + 1):
            try:
                return classifier.classify(pdf_path)
            except Exception as e:
                err_msg = str(e)
                if attempt >= max_retries or not _is_transient(err_msg):
                    raise
                sleep_time = delay + random.uniform(1.0, 3.0)   # jitter: tránh dồn request
                log.warning("classify lỗi (%s) lần %d/%d, thử lại sau %.1fs: %s",
                            name, attempt, max_retries, sleep_time, err_msg[:300])
                if notify is not None:
                    notify(f"API bận ({name}) — thử lại lần {attempt + 1}/{max_retries} "
                           f"sau {sleep_time:.0f}s")
                time.sleep(sleep_time)
                delay *= 2.0
    _classify.classifier = classifier   # để pipeline đọc được bộ đếm lượt gọi hỏng
    return _classify



def main():
    ap = argparse.ArgumentParser(description="Phân loại & ghép hồ sơ PDF căn hộ")
    ap.add_argument("--config", default=os.path.join(os.path.dirname(__file__), "config.yaml"))
    ap.add_argument("--input-root")
    ap.add_argument("--folder", help="Chỉ xử lý 1 folder căn hộ (calibrate)")
    ap.add_argument("--model")
    ap.add_argument("--mode", choices=["nhanh", "cham"], help="Chế độ chạy (mặc định theo config)")
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
                data = json.load(f)
            from classify import _provider_of_model
            specs = []
            for item in (data or []):
                if isinstance(item, dict) and (item.get("key") or "").strip():
                    prov = (item.get("provider") or "").strip().lower()
                    if not prov:                       # migrate spec cũ {model,key}
                        prov = _provider_of_model(item.get("model") or "gemini")
                    specs.append({"provider": prov, "key": item["key"].strip()})
                elif isinstance(item, str) and item.strip():   # định dạng cũ (mảng chuỗi)
                    specs.append({"provider": "gemini", "key": item.strip()})
            if specs:
                cfg["key_specs"] = specs
    except Exception:
        pass
    # Chế độ chạy: CLI --mode > config default_mode
    cfg["mode"] = (args.mode or cfg.get("default_mode") or "cham").lower()
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


SUMMARY_COLS = ["folder", "status", "sample_check", "total_pages", "classified_pages",
                "low_conf", "missing", "est_tokens", "real_tokens", "thinking_tokens",
                "failed_calls", "reasons"]


def write_summary(path: str, results: list[FolderResult], cfg: dict):
    sample_rate = float(cfg.get("sample_rate", 0.05))
    ok_idx = [i for i, r in enumerate(results) if r.status == "ok"]
    k = max(1, round(len(ok_idx) * sample_rate)) if ok_idx else 0
    sampled = set(random.sample(ok_idx, k=k)) if ok_idx else set()
    sampled_folders = {id(results[i]) for i in sampled}
    results = sorted(results, key=lambda r: r.prefix)
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(SUMMARY_COLS)
        for r in results:
            w.writerow([
                r.prefix, r.status, "yes" if id(r) in sampled_folders else "",
                r.total_pages, r.classified_pages, r.low_conf_pages,
                ", ".join(r.categories_missing), est_tokens(r.total_pages, cfg),
                r.total_tokens, r.thinking_tokens, r.failed_calls,
                " | ".join(r.reasons) or r.error,
            ])
    log.info("Đã ghi summary: %s", path)


def _print_aggregate(results: list[FolderResult], cfg: dict, dry_run: bool):
    n = len(results)
    by = lambda s: sum(1 for r in results if r.status == s)
    pages = sum(r.total_pages for r in results)
    real_tok = sum(r.total_tokens for r in results)
    think_tok = sum(r.thinking_tokens for r in results)
    n_failed = sum(r.failed_calls for r in results)
    log.info("==== TỔNG KẾT ====")
    log.info("Folder: %d (ok=%d, flagged=%d, error=%d)", n, by("ok"), by("flagged"), by("error"))
    log.info("Tổng trang: %d | Ước tính trước: ~%s token", pages,
             f"{sum(est_tokens(r.total_pages, cfg) for r in results):,}")
    log.info("TOKEN THẬT ĐO ĐƯỢC: %s (trong đó %s token suy nghĩ)",
             f"{real_tok:,}", f"{think_tok:,}")
    if n_failed:
        log.warning("CÓ %d LƯỢT GỌI HỎNG — token của các lượt này KHÔNG đếm được "
                    "nhưng Google vẫn có thể đã tính tiền.", n_failed)
    log.info("SỐ TIỀN THẬT: xem Google AI Studio → Usage (tool không tự quy ra tiền).")

    # Dự phóng khối lượng cho toàn bộ dự án (theo token, không theo tiền).
    ran = [r for r in results if r.total_tokens > 0]
    target = int(cfg.get("project_total_folders", 0) or 0)
    if ran and target:
        avg_tok = real_tok / len(ran)
        avg_pages = sum(r.total_pages for r in ran) / len(ran)
        log.info("DỰ PHÓNG: TB %s token/folder (~%.0f trang/folder, đo trên %d folder) "
                 "→ %d folder ≈ %s token", f"{avg_tok:,.0f}", avg_pages, len(ran),
                 target, f"{avg_tok * target:,.0f}")
    if dry_run:
        log.info("(dry-run: chưa ghép file. Bỏ --dry-run để xuất PDF.)")


if __name__ == "__main__":
    main()
