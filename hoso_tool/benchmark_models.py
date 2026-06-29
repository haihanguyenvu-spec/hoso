"""Benchmark tốc độ Gemini 2.5 Flash vs 3.5 Flash.

Chạy: .venv/bin/python hoso_tool/benchmark_models.py
"""
import os
import sys
import time
import yaml

sys.path.insert(0, os.path.dirname(__file__))

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.yaml")

# --- Load config ---
with open(CONFIG_PATH, encoding="utf-8") as f:
    cfg = yaml.safe_load(f)

# --- Load API keys ---
import json
def load_keys():
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
                return [k.strip() for k in keys if k.strip()]
    # Fallback env
    k = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    return [k.strip()] if k else []

keys = load_keys()
if not keys:
    print("❌ Không tìm thấy API key! Vui lòng nhập key trên giao diện trước.")
    sys.exit(1)

# --- Tìm 1 file PDF nhỏ để test ---
TEST_PDF = "/Users/haihanguyenvu/Downloads/document_94a41cd1-491f-4b28-b002-d0e7d890a7fa.pdf"
if not os.path.exists(TEST_PDF):
    # Tìm bất kỳ PDF nào nhỏ nhất trong Downloads
    pdfs = []
    for f in os.listdir(os.path.expanduser("~/Downloads")):
        if f.endswith(".pdf"):
            fp = os.path.join(os.path.expanduser("~/Downloads"), f)
            pdfs.append((os.path.getsize(fp), fp))
    if pdfs:
        pdfs.sort()
        TEST_PDF = pdfs[0][1]
    else:
        print("❌ Không tìm thấy file PDF nào để test!")
        sys.exit(1)

file_size_mb = os.path.getsize(TEST_PDF) / 1024 / 1024
print(f"📄 File test: {os.path.basename(TEST_PDF)} ({file_size_mb:.1f} MB)")
print(f"🔑 Dùng API key đầu tiên (***{keys[0][-6:]})")
print()

# --- Benchmark ---
from classify import GeminiClassifier

MODELS = ["gemini-2.5-flash", "gemini-3.5-flash"]
results = {}

for model_name in MODELS:
    print(f"{'='*50}")
    print(f"🤖 Đang test: {model_name}")
    print(f"{'='*50}")
    
    try:
        classifier = GeminiClassifier(
            model=model_name,
            categories=cfg["categories"],
            api_keys=keys[:1],  # Chỉ dùng 1 key để so sánh công bằng
            media_resolution=cfg.get("media_resolution", "medium"),
            rules=cfg.get("rules"),
        )
        
        t0 = time.time()
        result = classifier.classify(TEST_PDF)
        elapsed = time.time() - t0
        
        results[model_name] = {
            "time": elapsed,
            "pages": len(result.labels),
            "prompt_tokens": result.prompt_tokens,
            "output_tokens": result.output_tokens,
            "total_tokens": result.total_tokens,
            "error": None,
        }
        
        print(f"  ✅ Thành công trong {elapsed:.1f}s")
        print(f"  📊 Số trang phân loại: {len(result.labels)}")
        print(f"  🪙 Token: {result.prompt_tokens:,} input + {result.output_tokens:,} output = {result.total_tokens:,} total")
        print()
        
    except Exception as e:
        elapsed = time.time() - t0
        results[model_name] = {
            "time": elapsed,
            "pages": 0,
            "prompt_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "error": str(e)[:200],
        }
        print(f"  ❌ Lỗi sau {elapsed:.1f}s: {str(e)[:200]}")
        print()

# --- So sánh ---
print()
print(f"{'='*60}")
print(f"📊 KẾT QUẢ SO SÁNH")
print(f"{'='*60}")
print(f"{'Model':<25} {'Thời gian':>10} {'Token':>12} {'Lỗi?':>8}")
print(f"{'-'*60}")
for model_name in MODELS:
    r = results.get(model_name)
    if r:
        err = "CÓ" if r["error"] else "Không"
        print(f"{model_name:<25} {r['time']:>8.1f}s {r['total_tokens']:>10,} {err:>8}")

if all(r and not r["error"] for r in results.values()):
    t1 = results[MODELS[0]]["time"]
    t2 = results[MODELS[1]]["time"]
    faster = MODELS[0] if t1 < t2 else MODELS[1]
    diff = abs(t1 - t2)
    pct = (diff / max(t1, t2)) * 100
    print()
    print(f"🏆 {faster} nhanh hơn {diff:.1f}s ({pct:.0f}%)")
