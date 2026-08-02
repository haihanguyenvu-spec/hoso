"""Giao diện web (Streamlit) cho tool phân loại & ghép hồ sơ PDF căn hộ.

Chạy:  streamlit run hoso_tool/app.py
Mọi xử lý chạy LOCAL; ảnh hồ sơ chỉ gửi lên vision model ở bước "Phân loại".
"""
from __future__ import annotations

import copy
import logging
import os
import subprocess
import sys
import tempfile
import time
import uuid

import pandas as pd
import streamlit as st
import yaml

sys.path.insert(0, os.path.dirname(__file__))
import pipeline  # noqa: E402
from classify import _provider_of_model, make_classifier  # noqa: E402
from run import make_retrying_classify  # noqa: E402  (tự thử lại khi API lỗi tạm thời)

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.yaml")
NONE_LABEL = "(Không thuộc)"
log = logging.getLogger("hoso")


# ---------- Lưu trữ key (ghi được kể cả khi app cài trong Program Files) ----------
def user_data_dir() -> str:
    """Thư mục cấu hình của người dùng, luôn ghi được.

    Windows: %APPDATA%\\HoSoPDF   ·   macOS/Linux: ~/.config/hoso_tool
    """
    if os.name == "nt":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        d = os.path.join(base, "HoSoPDF")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
        d = os.path.join(base, "hoso_tool")
    os.makedirs(d, exist_ok=True)
    return d


def key_path(name: str) -> str:
    return os.path.join(user_data_dir(), name)


# ---------- Helpers ----------
@st.cache_data
def load_config() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    # Cho phép người dùng chỉnh danh sách loại chứng từ + quy tắc ngay trên giao
    # diện (lưu ở thư mục user, KHÔNG đụng config.yaml gốc). Nếu có → dùng đè.
    override = read_doc_types_override()
    if override:
        if override.get("categories"):
            cfg["categories"] = override["categories"]
        if override.get("rules") is not None:
            cfg["rules"] = override["rules"]
    return cfg


# ---------- Cấu hình loại chứng từ do người dùng chỉnh (lưu ở thư mục user) ----------
def doc_types_path() -> str:
    return key_path("doc_types.json")


def read_doc_types_override() -> dict | None:
    import json
    p = doc_types_path()
    if os.path.exists(p):
        try:
            with open(p, encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, dict) and d.get("categories"):
                return d
        except Exception:
            pass
    return None


def save_doc_types(categories: list[dict], rules: list[str]):
    import json
    with open(doc_types_path(), "w", encoding="utf-8") as f:
        json.dump({"categories": categories, "rules": rules}, f,
                  ensure_ascii=False, indent=2)


def reset_doc_types():
    """Xóa file override → quay về mặc định trong config.yaml."""
    p = doc_types_path()
    if os.path.exists(p):
        try:
            os.remove(p)
        except Exception:
            pass


def slugify_key(text: str, fallback: str = "loai") -> str:
    """Sinh 'key' ascii ổn định từ tên tiếng Việt (bỏ dấu, thay khoảng trắng)."""
    import re
    import unicodedata
    t = unicodedata.normalize("NFD", str(text))
    t = "".join(c for c in t if unicodedata.category(c) != "Mn")
    t = t.replace("đ", "d").replace("Đ", "D")
    t = re.sub(r"[^A-Za-z0-9]+", "_", t).strip("_").lower()
    return t or fallback


def label_maps(cfg: dict):
    """key <-> tên hiển thị (thêm khong_thuoc)."""
    name_by_key = {c["key"]: c["name"] for c in cfg["categories"]}
    name_by_key["khong_thuoc"] = NONE_LABEL
    key_by_name = {v: k for k, v in name_by_key.items()}
    return name_by_key, key_by_name


def get_api_key() -> str | None:
    key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if key:
        return key.strip()
    here = os.path.dirname(__file__)
    # Ưu tiên key đã lưu ở thư mục user (%APPDATA%), sau đó các vị trí cũ cạnh code.
    cands = [key_path(".gemini_key"),
             os.path.join(here, "key.py"), os.path.join(here, "..", "key.py"),
             os.path.join(here, ".gemini_key"), os.path.join(here, "..", ".gemini_key")]
    for p in cands:
        if os.path.exists(p):
            return open(p, encoding="utf-8").read().strip()
    return None


def get_api_key_2() -> str | None:
    key = os.environ.get("GEMINI_API_KEY_2")
    if key:
        return key.strip()
    here = os.path.dirname(__file__)
    cands = [key_path(".gemini_key_2"),
             os.path.join(here, ".gemini_key_2"), os.path.join(here, "..", ".gemini_key_2")]
    for p in cands:
        if os.path.exists(p):
            return open(p, encoding="utf-8").read().strip()
    return None


DEFAULT_PROVIDER = "gemini"
# Nhà cung cấp cho từng API key (cùng 1 key dùng mọi model của nhà đó).
PROVIDER_LABELS = {"gemini": "Google (Gemini)", "qwen": "Qwen (DashScope)", "openai": "OpenAI"}


def load_key_specs() -> list[dict]:
    """Trả list [{'provider':..., 'key':...}]. Tự chuyển định dạng CŨ ({model,key} hoặc mảng chuỗi)."""
    import json
    p = key_path("keys.json")
    if os.path.exists(p):
        try:
            with open(p, encoding="utf-8") as f:
                data = json.load(f)
            specs = []
            for item in (data or []):
                if isinstance(item, dict) and (item.get("key") or "").strip():
                    prov = (item.get("provider") or "").strip().lower()
                    if not prov:                      # spec cũ {model,key} -> suy provider
                        prov = _provider_of_model(item.get("model") or DEFAULT_PROVIDER)
                    specs.append({"provider": prov or DEFAULT_PROVIDER, "key": item["key"].strip()})
                elif isinstance(item, str) and item.strip():
                    specs.append({"provider": DEFAULT_PROVIDER, "key": item.strip()})
            if specs:
                return specs
        except Exception:
            pass
    # Legacy: các file .gemini_key rời
    legacy = []
    for k in (get_api_key(), get_api_key_2()):
        if k:
            legacy.append({"provider": DEFAULT_PROVIDER, "key": k})
    return legacy


def save_key_specs(specs: list[dict]):
    import json
    p = key_path("keys.json")
    clean = [{"provider": (s.get("provider") or DEFAULT_PROVIDER).strip().lower(), "key": s["key"].strip()}
             for s in specs if (s.get("key") or "").strip()]
    try:
        with open(p, "w", encoding="utf-8") as f:
            json.dump(clean, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


@st.cache_data(show_spinner=False)
def render_page(pdf_path: str, page: int, dpi: int = 110) -> bytes:
    from assemble import safe_input_path, get_safe_temp_dir
    with tempfile.TemporaryDirectory(dir=get_safe_temp_dir()) as d:
        root = os.path.join(d, "p")
        with safe_input_path(pdf_path) as safe_path:
            subprocess.run(["pdftoppm", "-png", "-r", str(dpi), "-f", str(page), "-l", str(page),
                            safe_path, root], check=True, capture_output=True)
        png = next(f for f in os.listdir(d) if f.endswith(".png"))
        return open(os.path.join(d, png), "rb").read()


def folder_status(folder: str, cfg: dict) -> str:
    out = os.path.join(folder, cfg.get("output_subdir", "output"))
    if os.path.exists(os.path.join(out, ".done")):
        return "✅ đã ghép"
    if os.path.exists(os.path.join(out, pipeline.INDEX_NAME)):
        return "📝 đã phân loại (chờ review)"
    return "⬜ chưa chạy"


@st.cache_data(ttl=5, show_spinner=False)
def discover(input_root: str, cfg: dict) -> tuple[list[str], str]:
    """Trả (danh sách folder hồ sơ, thông báo lỗi nếu không quét được).

    Với thư mục Google Drive bị macOS chặn quyền, os.path.isdir vẫn TRUE (stat
    được) nhưng os.listdir ném PermissionError — trước đây lỗi này làm sập cả
    app kèm traceback thô thay vì chỉ chỗ cấp quyền.
    """
    out_subdir = cfg.get("output_subdir", "output")
    res: list[str] = []
    if not os.path.isdir(input_root):
        return res, ""
    try:
        names = sorted(os.listdir(input_root))
    except OSError as e:
        return res, f"{type(e).__name__}: {e}"
    for name in names:
        d = os.path.join(input_root, name)
        if os.path.isdir(d) and name not in (out_subdir, "_review") \
                and pipeline.list_pdfs(d, out_subdir):
            res.append(d)
    return res, ""


def render_doc_types_editor(cfg: dict):
    """Trình chỉnh danh sách loại chứng từ ngay trên giao diện (thêm/bớt/sửa/sắp thứ tự).

    Dùng cấu hình hiện tại làm mặc định; lưu vào thư mục user để áp dụng cho các
    lần phân loại sau. Không đổi kết quả các folder đã ghép.
    """
    DT = "doc_types_edit"
    if DT not in st.session_state:
        cats = copy.deepcopy(cfg["categories"])
        for c in cats:
            c["_uid"] = uuid.uuid4().hex[:8]   # id tạm để widget bám đúng khi sắp lại
        st.session_state[DT] = cats
        st.session_state["rules_edit"] = list(cfg.get("rules", []))
    cats = st.session_state[DT]

    st.caption("Thêm/bớt/sửa tên loại và nhóm con theo từng thời kỳ. **Thứ tự từ trên xuống = "
               "thứ tự sắp trong file ghép** (văn bản chính nên để trên). Sau khi **Lưu**, cấu hình "
               "áp dụng cho các lần phân loại tiếp theo — không đổi các folder đã chạy.")

    for ci, cat in enumerate(cats):
        title = cat.get("name") or "(chưa đặt tên)"
        req_tag = " · bắt buộc" if cat.get("required") else ""
        with st.expander(f"📁 {ci + 1}. {title}  ·  {len(cat.get('subtypes', []))} nhóm con{req_tag}",
                         expanded=not cat.get("name")):
            uid = cat["_uid"]
            c1, c2, c3 = st.columns([5, 1.4, 1.6])
            cat["name"] = c1.text_input("Tên loại (dùng đặt tên file output)",
                                        value=cat.get("name", ""), key=f"cn_{uid}")
            cat["required"] = c2.checkbox("Bắt buộc", value=bool(cat.get("required")), key=f"cr_{uid}",
                                          help="Bật: folder thiếu loại này sẽ bị gắn cờ 'cần kiểm'.")
            with c3:
                m1, m2, m3 = st.columns(3)
                if m1.button("⬆️", key=f"up_{uid}", disabled=(ci == 0), help="Đưa lên"):
                    cats[ci - 1], cats[ci] = cats[ci], cats[ci - 1]
                    st.rerun()
                if m2.button("⬇️", key=f"dn_{uid}", disabled=(ci == len(cats) - 1), help="Đưa xuống"):
                    cats[ci + 1], cats[ci] = cats[ci], cats[ci + 1]
                    st.rerun()
                if m3.button("🗑️", key=f"del_{uid}", help="Xóa loại này"):
                    cats.pop(ci)
                    st.rerun()

            st.markdown("**Nhóm con** — văn bản chính để trên, đính kèm để dưới:")
            sub_rows = [{"Tên nhóm con / mô tả": s.get("desc") or s.get("id", ""),
                         "Từ khóa nhận diện (cách nhau bởi dấu phẩy)": ", ".join(s.get("keywords", []))}
                        for s in cat.get("subtypes", [])]
            df = (pd.DataFrame(sub_rows) if sub_rows else
                  pd.DataFrame({"Tên nhóm con / mô tả": pd.Series(dtype=str),
                                "Từ khóa nhận diện (cách nhau bởi dấu phẩy)": pd.Series(dtype=str)}))
            edited = st.data_editor(
                df, num_rows="dynamic", width="stretch", hide_index=True,
                key=f"subs_{uid}",
                column_config={
                    "Tên nhóm con / mô tả": st.column_config.TextColumn(width="medium"),
                    "Từ khóa nhận diện (cách nhau bởi dấu phẩy)": st.column_config.TextColumn(width="large"),
                })
            prev = {(s.get("desc") or s.get("id", "")): s for s in cat.get("subtypes", [])}
            new_subs = []
            for _, row in edited.iterrows():
                nm = row.get("Tên nhóm con / mô tả")
                nm = "" if (nm is None or (isinstance(nm, float) and pd.isna(nm))) else str(nm).strip()
                if not nm:
                    continue
                kw_raw = row.get("Từ khóa nhận diện (cách nhau bởi dấu phẩy)")
                kw_raw = "" if (kw_raw is None or (isinstance(kw_raw, float) and pd.isna(kw_raw))) else str(kw_raw)
                kws = [k.strip() for k in kw_raw.split(",") if k.strip()]
                sid = prev.get(nm, {}).get("id") or slugify_key(nm, "nhom")
                new_subs.append({"id": sid, "desc": nm, "keywords": kws})
            cat["subtypes"] = new_subs

    st.markdown("")
    a1, a2, a3 = st.columns(3)
    if a1.button("➕ Thêm loại chứng từ", width="stretch"):
        cats.append({"name": "", "required": False, "subtypes": [], "_uid": uuid.uuid4().hex[:8]})
        st.rerun()

    with st.expander("⚙️ Quy tắc nghiệp vụ nâng cao (tùy chọn)"):
        st.caption("Mỗi dòng một quy tắc — nhồi vào prompt giúp model phân loại chính xác hơn "
                   "(vd phân biệt ủy quyền bên bán vs bên mua).")
        txt = st.text_area("Quy tắc", value="\n".join(st.session_state.get("rules_edit", [])),
                           height=180, key="rules_txt", label_visibility="collapsed")
        st.session_state["rules_edit"] = [ln.strip() for ln in txt.splitlines() if ln.strip()]

    if a2.button("💾 Lưu cấu hình", type="primary", width="stretch"):
        clean, used = [], set()
        for cat in cats:
            name = (cat.get("name") or "").strip()
            if not name:
                continue
            key = cat.get("key") or slugify_key(name, "loai")
            base, i = key, 2
            while key in used:
                key = f"{base}_{i}"
                i += 1
            used.add(key)
            clean.append({"key": key, "name": name, "required": bool(cat.get("required")),
                          "subtypes": cat.get("subtypes", []) or []})
        if not clean:
            st.error("Cần ít nhất 1 loại có tên.")
        else:
            save_doc_types(clean, st.session_state.get("rules_edit", []))
            load_config.clear()
            st.session_state.pop(DT, None)
            st.session_state.pop("rules_edit", None)
            st.success(f"✅ Đã lưu {len(clean)} loại chứng từ. Áp dụng cho các lần phân loại sau.")
            st.rerun()

    if a3.button("↺ Khôi phục mặc định", width="stretch",
                 help="Xóa cấu hình đã lưu, quay về mặc định gốc của tool."):
        reset_doc_types()
        load_config.clear()
        st.session_state.pop(DT, None)
        st.session_state.pop("rules_edit", None)
        st.toast("Đã khôi phục cấu hình gốc.")
        st.rerun()


def render_api_keys_editor(cfg: dict):
    """Trình quản lý API key (mỗi key gắn 1 NHÀ CUNG CẤP). Render ở vùng chính (panel Cài đặt)."""
    st.caption("Mỗi key chỉ cần chọn **nhà cung cấp** (cùng key dùng mọi model của nhà đó). "
               "**Model** do **chế độ chạy** (thanh bên) quyết định; hệ thống tự lấy key của nhà "
               "tương ứng và fallback lần lượt khi lỗi.")
    prov_ids = list(PROVIDER_LABELS.keys())
    to_delete = None
    for idx, spec in enumerate(st.session_state.key_specs):
        cur = (spec.get("provider") or DEFAULT_PROVIDER).lower()
        if cur not in prov_ids:
            cur = DEFAULT_PROVIDER
        c_prov, c_key, c_del = st.columns([2, 4, 0.6])
        with c_prov:
            spec["provider"] = st.selectbox(
                f"Nhà cung cấp key #{idx+1}", options=prov_ids, index=prov_ids.index(cur),
                format_func=lambda p: PROVIDER_LABELS[p],
                key=f"key_prov_{idx}", label_visibility="collapsed")
        with c_key:
            spec["key"] = st.text_input(
                f"Key #{idx+1}", value=spec.get("key", ""), type="password",
                key=f"key_val_{idx}", label_visibility="collapsed",
                placeholder=f"Dán API key #{idx+1}…").strip()
        with c_del:
            if st.button("❌", key=f"del_key_{idx}", help=f"Xóa key #{idx+1}"):
                to_delete = idx

    if to_delete is not None:
        st.session_state.key_specs.pop(to_delete)
        for i in range(len(st.session_state.key_specs) + 1):
            st.session_state.pop(f"key_val_{i}", None)
            st.session_state.pop(f"key_prov_{i}", None)
        if not st.session_state.key_specs:
            st.session_state.key_specs = [{"provider": DEFAULT_PROVIDER, "key": ""}]
        save_key_specs(st.session_state.key_specs)
        st.rerun()

    a1, a2 = st.columns(2)
    if a1.button("➕ Thêm key", width="stretch"):
        last_prov = (st.session_state.key_specs[-1].get("provider")
                     if st.session_state.key_specs else DEFAULT_PROVIDER)
        st.session_state.key_specs.append({"provider": last_prov or DEFAULT_PROVIDER, "key": ""})
        st.rerun()
    if a2.button("💾 Lưu API keys", width="stretch", type="primary"):
        st.session_state.key_specs = ([s for s in st.session_state.key_specs if s.get("key")]
                                      or [{"provider": DEFAULT_PROVIDER, "key": ""}])
        save_key_specs(st.session_state.key_specs)
        st.toast("Đã lưu danh sách API Keys!")
        st.rerun()


# ---------- Nhật ký chạy (ghi ra file, có mốc thời gian) ----------
def setup_logging() -> str:
    """Bật ghi log ra file cho giao diện.

    Trước đây app.py KHÔNG cấu hình logging: mọi cảnh báo của classify (503, xoay
    key, nhãn hỏng) chỉ ra stderr rồi mất. Khi batch đứng thì không còn gì để dò.
    """
    import logging
    from logging.handlers import RotatingFileHandler

    d = os.path.join(user_data_dir(), "logs")
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, "hoso.log")
    root = logging.getLogger()
    if not any(getattr(h, "_hoso_handler", False) for h in root.handlers):
        h = RotatingFileHandler(path, maxBytes=5_000_000, backupCount=5, encoding="utf-8")
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
        h._hoso_handler = True          # dấu để rerun sau không gắn thêm handler trùng
        root.addHandler(h)
        root.setLevel(logging.INFO)
        # httpx log từng request kèm mã 200/503 và thời điểm — chính là thứ đã
        # dựng lại được timeline khi truy nguyên sự cố, nên giữ.
        logging.getLogger("httpx").setLevel(logging.INFO)
    return path


# ---------- Báo cáo tổng ----------
SUMMARY_COLS = ["folder", "status", "sample_check", "total_pages", "classified_pages",
                "low_conf", "missing", "est_cost_usd", "real_tokens", "real_cost_usd",
                "reasons"]


def upsert_summary(input_root: str, res, cfg: dict):
    """Ghi đè đúng 1 dòng của folder vào _review/summary.csv.

    Dùng chung cho tab ① và nút ghép ở tab ②. Trước đây tab ② chỉ ghi .done nên
    báo cáo tổng vẫn giữ trạng thái 'error' của lần chạy hỏng trước đó.
    """
    import csv

    from run import est_cost

    review_dir = os.path.join(input_root, "_review")
    os.makedirs(review_dir, exist_ok=True)
    path = os.path.join(review_dir, "summary.csv")
    rows: list[dict] = []
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8-sig") as f:
                rows = [r for r in csv.DictReader(f) if r.get("folder") != res.prefix]
        except Exception:
            rows = []
    rows.append({
        "folder": res.prefix, "status": res.status, "sample_check": "",
        "total_pages": str(res.total_pages), "classified_pages": str(res.classified_pages),
        "low_conf": str(res.low_conf_pages), "missing": ", ".join(res.categories_missing),
        "est_cost_usd": f"{est_cost(res.total_pages, cfg):.4f}",
        "real_tokens": str(res.total_tokens), "real_cost_usd": f"{res.real_cost:.4f}",
        "reasons": " | ".join(res.reasons) or res.error,
    })
    rows.sort(key=lambda r: r.get("folder", ""))
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=SUMMARY_COLS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


# ---------- Hàng đợi batch (sống sót qua rerun) ----------
BATCH = "batch_state"


def fmt_dur(sec: float) -> str:
    sec = int(max(0, sec))
    if sec >= 3600:
        return f"{sec // 3600}h{(sec % 3600) // 60:02d}m"
    if sec >= 60:
        return f"{sec // 60}m{sec % 60:02d}s"
    return f"{sec}s"


def start_batch(folders: list[str], force: bool):
    st.session_state[BATCH] = {
        "queue": list(folders), "total": len(folders), "force": force,
        "ok": 0, "flagged": 0, "errors": 0, "skipped": 0,
        "pages": 0, "tokens": 0, "cost": 0.0,
        "log": [], "attempts": {}, "durations": [],
        "running": True, "started": time.time(),
    }


def stop_batch():
    """on_click của nút Dừng — chạy TRƯỚC thân script nên folder kế sẽ không khởi động."""
    b = st.session_state.get(BATCH)
    if b:
        b["running"] = False


def get_classifier(cfg: dict, key_specs: list[dict]):
    """Dựng classifier một lần rồi tái dùng qua các rerun.

    Mỗi lần dựng là mở ngần ấy client HTTP mới; dựng lại cho từng folder sẽ rò
    connection pool. Chỉ dựng lại khi chế độ hoặc bộ key đổi.
    """
    sig = (cfg.get("mode"), tuple(sorted((s["provider"], s["key"][-6:]) for s in key_specs)))
    if st.session_state.get("classifier_sig") != sig:
        cfg["key_specs"] = key_specs
        st.session_state["classifier"] = make_classifier(cfg)
        st.session_state["classifier_sig"] = sig
    return st.session_state["classifier"]


# ---------- UI ----------
st.set_page_config(page_title="Phân loại & Ghép hồ sơ PDF", page_icon="📄", layout="wide", initial_sidebar_state="expanded")
# Ẩn các nút/chrome mặc định của Streamlit (menu ⋮, Deploy, footer "Made with Streamlit")
# -> giao diện chỉ còn các nút của tool.
st.markdown("""
<style>
#MainMenu {visibility: hidden;}
footer {visibility: hidden;}
[data-testid="stHeaderActionElements"] {display: none;}
</style>
""", unsafe_allow_html=True)
LOG_PATH = setup_logging()
cfg = load_config()
name_by_key, key_by_name = label_maps(cfg)

if "key_specs" not in st.session_state:
    st.session_state.key_specs = load_key_specs() or [{"provider": DEFAULT_PROVIDER, "key": ""}]

st.sidebar.title("📂 Phân loại & Ghép hồ sơ PDF")
st.sidebar.caption("© Nguyễn Vũ Hải Hà @ 2026")

# --- Chế độ chạy (Nhanh / Chậm) — thay cho việc chọn model lẻ ---
MODE_LABELS = {"cham": "🐢 Chậm", "nhanh": "⚡ Nhanh"}
if "run_mode" not in st.session_state:
    st.session_state.run_mode = (cfg.get("default_mode") or "cham").lower()

with st.sidebar:
    _mkeys = list(MODE_LABELS.keys())
    _cur = st.session_state.run_mode if st.session_state.run_mode in _mkeys else "cham"
    run_mode = st.radio(
        "⚙️ Chế độ chạy",
        options=_mkeys,
        index=_mkeys.index(_cur),
        format_func=lambda m: MODE_LABELS[m],
        horizontal=True,
        key="run_mode_selector",
        help="Nhanh: " + " → ".join(cfg.get("modes", {}).get("nhanh", []))
             + "\n\nChậm: " + " → ".join(cfg.get("modes", {}).get("cham", [])),
    )
    st.session_state.run_mode = run_mode
    cfg["mode"] = run_mode
    _mode_models = cfg.get("modes", {}).get(run_mode, [])
    st.caption("Model: **" + (" → ".join(_mode_models) or "?") + "**")

st.sidebar.markdown("---")

# --- Nút CÀI ĐẶT: ẩn API key + danh mục chứng từ khỏi màn hình vận hành ---
# Dùng cờ session (không phải widget-key) để nút bật/tắt được an toàn.
if "settings_open" not in st.session_state:
    st.session_state.settings_open = False
with st.sidebar:
    _n_keys = sum(1 for s in st.session_state.key_specs if s.get("key"))
    _ov = read_doc_types_override() is not None
    st.caption(f"🔑 {_n_keys} key · 📄 danh mục: {'tùy chỉnh' if _ov else 'mặc định'}")
    if st.button(("✖️ Đóng cài đặt" if st.session_state.settings_open else "⚙️ Cài đặt"),
                 width="stretch"):
        st.session_state.settings_open = not st.session_state.settings_open
        st.rerun()

# --- Panel CÀI ĐẶT (chiếm trọn vùng chính; ẩn phần vận hành khi mở) ---
if st.session_state.settings_open:
    st.markdown("## ⚙️ Cài đặt")
    if st.button("← Quay lại vận hành", key="back_to_ops"):
        st.session_state.settings_open = False
        st.rerun()
    tab_keys, tab_docs = st.tabs(["🔑 API Keys", "📄 Danh mục chứng từ"])
    with tab_keys:
        render_api_keys_editor(cfg)
    with tab_docs:
        render_doc_types_editor(cfg)
    st.stop()

# --- Ô nhập thư mục gốc (trước tabs, dùng chung cho cả 3 tab) ---
st.markdown("### 📁 Thư mục gốc chứa các folder hồ sơ")
col_path, col_hint = st.columns([4, 1])
with col_path:
    input_root = st.text_input(
        "Đường dẫn thư mục gốc",
        value=cfg["input_root"],
        placeholder="/path/to/Cr8-3",
        help="Mỗi subfolder bên trong = 1 bộ hồ sơ. Nhập đường dẫn rồi nhấn Enter.",
        label_visibility="collapsed",
    )
with col_hint:
    st.caption("Nhập xong nhấn Enter ↵")

if not input_root:
    st.warning("⚠️ Vui lòng nhập đường dẫn thư mục gốc.")
    st.stop()
if not os.path.isdir(input_root):
    st.error(f"❌ Thư mục không tồn tại hoặc không truy cập được: `{input_root}`")
    st.info("💡 Mở Finder → tìm thư mục Cr8-3 → kéo thả vào ô trên để lấy đường dẫn.  "
            "Hoặc vào System Settings → Privacy & Security → Full Disk Access → bật Terminal.")
    st.stop()

folders, scan_error = discover(input_root, cfg)
if scan_error:
    st.error(f"❌ Không đọc được nội dung thư mục gốc — `{scan_error}`")
    st.info("💡 Thư mục tồn tại nhưng macOS chặn quyền liệt kê (rất hay gặp với "
            "thư mục Google Drive). Mở **System Settings → Privacy & Security → "
            "Full Disk Access**, bật cho Terminal (hoặc ứng dụng đang chạy tool), "
            "rồi khởi động lại tool.")
    st.stop()

st.divider()
tab1, tab2, tab3 = st.tabs(["① Phân loại", "② Review & Sửa nhãn", "③ Tổng kết"])


# ===== Tab 1: Phân loại =====
def render_batch_progress(b: dict):
    """Bảng tiến trình: đang làm gì, xong bao nhiêu, còn bao lâu, tốn bao nhiêu."""
    done = b["total"] - len(b["queue"])
    st.progress(done / b["total"] if b["total"] else 1.0,
                text=f"Folder {done}/{b['total']}")

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("✅ Xong", b["ok"] + b["flagged"])
    c2.metric("⚠️ Cần kiểm", b["flagged"])
    c3.metric("❌ Lỗi", b["errors"])
    c4.metric("⏭️ Bỏ qua", b["skipped"])

    avg = sum(b["durations"]) / len(b["durations"]) if b["durations"] else 0.0
    bits = [f"⏱️ đã chạy **{fmt_dur(time.time() - b['started'])}**"]
    if avg:
        bits.append(f"TB **{fmt_dur(avg)}**/folder")
        if b["queue"]:
            bits.append(f"còn khoảng **{fmt_dur(avg * len(b['queue']))}**")
    if b["tokens"]:
        bits.append(f"token **{b['tokens']:,}** · chi phí **${b['cost']:.4f}**")
    st.caption(" · ".join(bits))

    if b["log"]:
        icons = {"ok": "✅", "flagged": "⚠️", "error": "❌", "skip": "⏭️"}
        with st.expander(f"📜 Nhật ký ({len(b['log'])} folder)", expanded=False):
            for status, name, msg, dt in reversed(b["log"]):
                st.markdown(f"{icons.get(status, '•')} **{name}** · {fmt_dur(dt)} — {msg}")


with tab1:
    st.subheader("Phân loại trang bằng vision model")

    out_subdir = cfg.get("output_subdir", "output")
    key_specs_active = [{"provider": s.get("provider", DEFAULT_PROVIDER), "key": s["key"].strip()}
                        for s in st.session_state.key_specs if s.get("key", "").strip()]
    batch = st.session_state.get(BATCH)
    batch_running = bool(batch and batch["running"] and batch["queue"])

if batch_running:
    # ---- ĐANG CHẠY: mỗi lần rerun xử lý ĐÚNG MỘT folder rồi tự gọi lại ----
    # Trước đây cả vòng lặp nằm trong `if st.button(...)`, nên mọi rerun (websocket
    # reconnect khi tab bị ngủ, lỡ chạm widget, F5) đều ném RerunException/
    # StopException — hai lớp kế thừa BaseException nên `except Exception` không
    # bắt được — batch chết lặng lẽ và nút trở về chưa-bấm. Giữ hàng đợi trong
    # session_state thì rerun chỉ mất tối đa folder đang dở, rồi chạy tiếp.
    with tab1:
        render_batch_progress(batch)
        st.button("⏹ Dừng", on_click=stop_batch, width="stretch",
                  help="Dừng ngay. Folder đang dở sẽ được làm lại từ đầu ở lần chạy sau.")

        folder = batch["queue"][0]
        fname = os.path.basename(folder)
        done_marker = os.path.join(folder, out_subdir, ".done")

        if os.path.exists(done_marker) and not batch["force"]:
            batch["queue"].pop(0)
            batch["skipped"] += 1
            batch["log"].append(("skip", fname, "đã ghép trước đó", 0.0))
            st.rerun()

        # Folder bị gián đoạn đi gián đoạn lại thì bỏ, tránh kẹt vòng lặp vô hạn.
        batch["attempts"][folder] = batch["attempts"].get(folder, 0) + 1
        if batch["attempts"][folder] > 2:
            batch["queue"].pop(0)
            batch["errors"] += 1
            batch["log"].append(("error", fname, "bị gián đoạn 2 lần liên tiếp — bỏ qua", 0.0))
            st.rerun()

        status = st.status(f"⏳ Đang xử lý **{fname}**…", expanded=True)
        stage = status.empty()

        def on_progress(i_file, n_files, name, step):
            # Mỗi lần gọi là một lệnh st.* → đẩy tin lên trình duyệt, giữ kết nối
            # sống và cho người dùng thấy máy vẫn đang chạy chứ không phải treo.
            stage.markdown(f"**File {i_file}/{n_files}** · `{name}`  \n{step}")

        def on_notify(text):
            stage.warning(f"⚠️ {text}")

        n_done = batch["total"] - len(batch["queue"]) + 1
        log.info("=== BẮT ĐẦU [%d/%d] %s ===", n_done, batch["total"], fname)
        t0 = time.time()
        res, err = None, ""
        try:
            classify = make_retrying_classify(
                get_classifier(cfg, key_specs_active),
                int(cfg.get("max_retries", 3)), notify=on_notify)
            res = pipeline.process_folder(folder, cfg, classify, dry_run=False,
                                          progress=on_progress)
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            log.exception("Folder %s hỏng ngoài dự kiến", fname)
        dt = time.time() - t0

        batch["queue"].pop(0)
        batch["durations"].append(dt)
        if res is None:
            batch["errors"] += 1
            batch["log"].append(("error", fname, err, dt))
            status.update(label=f"❌ {fname} — {err}", state="error", expanded=False)
        else:
            upsert_summary(input_root, res, cfg)
            batch["pages"] += res.total_pages
            batch["tokens"] += res.total_tokens
            batch["cost"] += res.real_cost
            if res.status == "error":
                batch["errors"] += 1
                batch["log"].append(("error", fname, res.error, dt))
                status.update(label=f"❌ {fname} — {res.error}", state="error", expanded=False)
            else:
                os.makedirs(os.path.dirname(done_marker), exist_ok=True)
                open(done_marker, "w").close()
                batch["attempts"].pop(folder, None)
                note = "; ".join(res.reasons) if res.reasons else "OK"
                batch["flagged" if res.status == "flagged" else "ok"] += 1
                batch["log"].append((res.status, fname,
                                     f"{len(res.outputs)} file · "
                                     f"{res.classified_pages}/{res.total_pages} trang · {note}", dt))
                status.update(label=f"✅ {fname} — {res.classified_pages}/{res.total_pages} trang",
                              state="complete", expanded=False)
        log.info("=== XONG [%d/%d] %s sau %s ===", n_done, batch["total"], fname, fmt_dur(dt))
        st.rerun()

elif batch:
    # ---- ĐÃ DỪNG / ĐÃ XONG ----
    with tab1:
        b = batch
        total_done = b["ok"] + b["flagged"]
        head = (f"{total_done} folder xong ({b['flagged']} cần kiểm) · "
                f"{b['errors']} lỗi · {b['skipped']} bỏ qua · "
                f"tổng {fmt_dur(time.time() - b['started'])}")
        if b["queue"]:
            st.warning(f"⏸️ Đã dừng — {head}. Còn **{len(b['queue'])}** folder chưa chạy.")
        elif b["errors"]:
            st.warning(f"⚠️ Hoàn thành — {head}")
        else:
            st.success(f"🎉 Hoàn thành — {head}. Sang tab ② để review nhãn nếu cần.")
        render_batch_progress(b)

        c1, c2 = st.columns(2)
        if b["queue"] and c1.button("▶ Chạy tiếp", type="primary", width="stretch"):
            b["running"] = True
            b["started"] = time.time() - sum(b["durations"])
            st.rerun()
        if c2.button("✖️ Đóng bảng kết quả", width="stretch"):
            del st.session_state[BATCH]
            st.rerun()

elif not folders:
    with tab1:
        st.info("Không thấy folder hồ sơ nào trong thư mục gốc. "
                "Hãy chắc chắn mỗi subfolder chứa ít nhất 1 file PDF.")

else:
    with tab1:
        rows = [{"Folder": os.path.basename(f), "Số PDF": len(pipeline.list_pdfs(f, out_subdir)),
                 "Trạng thái": folder_status(f, cfg)} for f in folders]
        st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)

        todo = [f for f in folders if "chưa chạy" in folder_status(f, cfg)]

        col_pick, col_force = st.columns([5, 2])
        with col_pick:
            pick = st.multiselect(
                "Chọn folder để phân loại (mặc định: các folder chưa chạy)",
                options=[os.path.basename(f) for f in folders],
                default=[os.path.basename(f) for f in todo])
        with col_force:
            force_rerun = st.checkbox(
                "🔄 Chạy lại folder đã xong",
                value=False,
                help="Nếu bật: cho phép xử lý lại các folder đã có dấu ✅. "
                     "Nếu tắt (mặc định): các folder đã ghép sẽ bị bỏ qua dù có chọn.")
        # Nếu chế độ ưu tiên nhà cung cấp chưa có key → vẫn chạy bằng key sẵn có (fallback), chỉ báo nhẹ.
        _need = {_provider_of_model(m) for m in cfg.get("modes", {}).get(cfg.get("mode", ""), [])}
        _have = {s["provider"] for s in key_specs_active}
        if key_specs_active and _need and not (_have & _need):
            _lbl = ", ".join(PROVIDER_LABELS.get(p, p) for p in _need)
            st.info(f"ℹ️ Chế độ **{cfg.get('mode')}** ưu tiên **{_lbl}** (chưa có key) → "
                    "sẽ chạy bằng key hiện có làm dự phòng. Thêm key ở ⚙️ Cài đặt nếu muốn dùng đúng model.")
        if not key_specs_active:
            st.warning("⚠️ Chưa có API key nào. Vào ⚙️ **Cài đặt → API Keys** để nhập.")
        if st.button("▶ Phân loại & Tạo 6 file PDF", type="primary",
                     disabled=not (pick and key_specs_active), width="stretch"):
            sel = [f for f in folders if os.path.basename(f) in pick]
            log.info("### BẮT ĐẦU ĐỢT CHẠY: %d folder | chế độ=%s | %d key ###",
                     len(sel), cfg.get("mode"), len(key_specs_active))
            start_batch(sel, force_rerun)
            st.rerun()
        st.caption(f"📝 Nhật ký chi tiết ghi tại `{LOG_PATH}` — gửi file này khi cần dò lỗi.")


# ===== Tab 2: Review & sửa nhãn =====
with tab2:
    reviewable = [f for f in folders
                  if os.path.exists(os.path.join(f, cfg["output_subdir"], pipeline.INDEX_NAME))]
    if not reviewable:
        st.info("Chưa folder nào được phân loại. Làm tab ① trước.")
    else:
        fname = st.selectbox("Chọn folder", [os.path.basename(f) for f in reviewable])
        folder = next(f for f in reviewable if os.path.basename(f) == fname)

        skey = f"entries::{folder}"
        if skey not in st.session_state:
            st.session_state[skey] = pipeline.read_index(folder, cfg)
        entries = st.session_state[skey]

        col_l, col_r = st.columns([3, 2])
        with col_l:
            only_check = st.checkbox("Chỉ hiện trang cần kiểm (confidence thấp / Không thuộc)")
            thr = float(cfg.get("confidence_threshold", 0.75))
            df = pd.DataFrame(entries)
            df["loại"] = df["category"].map(name_by_key).fillna(NONE_LABEL)
            view = df.copy()
            if only_check:
                view = view[(view["confidence"] < thr) | (view["category"] == "khong_thuoc")]
            disp = view[["file", "page", "loại", "confidence", "evidence"]].copy()
            disp["confidence"] = (disp["confidence"] * 100).round().astype(int)  # 0.9 -> 90
            edited = st.data_editor(
                disp,
                column_config={
                    "file": st.column_config.TextColumn("File", disabled=True),
                    "page": st.column_config.NumberColumn("Trang", disabled=True),
                    "loại": st.column_config.SelectboxColumn(
                        "Loại (sửa ở đây)", options=list(key_by_name.keys()), required=True),
                    "confidence": st.column_config.NumberColumn("Tin cậy", disabled=True, format="%d%%"),
                    "evidence": st.column_config.TextColumn("Căn cứ", disabled=True),
                },
                width="stretch", hide_index=True, height=520, key=f"editor::{folder}")

            # Áp nhãn đã sửa ngược lại vào entries (khớp theo file+page).
            for _, r in edited.iterrows():
                new_key = key_by_name.get(r["loại"], "khong_thuoc")
                for e in entries:
                    if e["file"] == r["file"] and e["page"] == int(r["page"]):
                        e["category"] = new_key
                        break

        with col_r:
            opts = [f'{e["file"]} — p{e["page"]} [{name_by_key.get(e["category"], "?")}]'
                    for e in sorted(entries, key=lambda e: (e["file_index"], e["page"]))]
            sel = st.selectbox("Xem trang", opts, key=f"prev::{folder}")
            if sel:
                fpart, rest = sel.split(" — p", 1)
                pg = int(rest.split(" ", 1)[0])
                try:
                    st.image(render_page(os.path.join(folder, fpart), pg),
                             caption=sel, width="stretch")
                except Exception as e:
                    st.warning(f"Không render được trang: {e}")

        c1, c2 = st.columns(2)
        if c1.button("💾 Lưu nhãn", key=f"save::{folder}"):
            out_dir = os.path.join(folder, cfg["output_subdir"])
            os.makedirs(out_dir, exist_ok=True)
            pipeline.write_index(os.path.join(out_dir, pipeline.INDEX_NAME), entries)
            st.toast("Đã lưu _index.csv")
        if c2.button("📦 Tạo 6 file PDF", type="primary", key=f"build::{folder}"):
            res = pipeline.assemble_from_index(folder, cfg, entries)
            open(os.path.join(folder, cfg["output_subdir"], ".done"), "w").close()
            # Cập nhật luôn báo cáo tổng: trước đây nút này chỉ ghi .done nên tab ③
            # vẫn hiện trạng thái 'error' của lần chạy hỏng trước.
            upsert_summary(input_root, res, cfg)
            st.success(f"Đã tạo {len(res.outputs)} file ({res.classified_pages}/{res.total_pages} trang).")
            if res.total_tokens:
                st.caption(f"💰 Token thật: {res.total_tokens:,} · Chi phí thực ≈ ${res.real_cost:.4f} "
                           "(số chính xác xem ở Google AI Studio → Usage)")
            if res.reasons:
                st.warning("Lưu ý: " + "; ".join(res.reasons))
            for o in res.outputs:
                with open(o, "rb") as fh:
                    st.download_button(f"⬇ {os.path.basename(o)}", fh.read(),
                                       file_name=os.path.basename(o), key=f"dl::{o}")


# ===== Tab 3: Tổng kết =====
COLS_VN = {
    "folder": "Folder", "status": "Trạng thái", "sample_check": "Kiểm mẫu",
    "total_pages": "Tổng trang", "classified_pages": "Đã xếp loại",
    "low_conf": "Trang nghi ngờ", "missing": "Thiếu loại",
    "est_cost_usd": "Ước tính ($)", "real_tokens": "Token thực",
    "real_cost_usd": "Chi phí thực ($)", "reasons": "Lý do",
}
STATUS_VN = {"ok": "✅ ok", "flagged": "⚠️ cần kiểm", "error": "❌ lỗi"}

with tab3:
    summ = os.path.join(input_root, "_review", "summary.csv")
    if not os.path.exists(summ):
        st.info("Chưa có báo cáo tổng. Báo cáo này sinh ra khi chạy cả lô bằng CLI "
                "(`run.py`). Nếu đang làm từng folder thì xem trực tiếp ở tab ①/②.")
    else:
        df = pd.read_csv(summ)
        n_ok = int((df["status"] == "ok").sum())
        n_flag = int((df["status"] == "flagged").sum())
        n_err = int((df["status"] == "error").sum())
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Tổng folder", len(df))
        c2.metric("✅ OK", n_ok)
        c3.metric("⚠️ Cần kiểm", n_flag)
        c4.metric("❌ Lỗi", n_err)

        # Dự phóng chi phí từ token THẬT của các folder đã chạy.
        if "real_cost_usd" in df.columns:
            ran = pd.to_numeric(df["real_cost_usd"], errors="coerce").fillna(0)
            ran = ran[ran > 0]
            if not ran.empty:
                avg = float(ran.mean())
                tokens = int(pd.to_numeric(df.get("real_tokens", 0), errors="coerce").fillna(0).sum())
                k1, k2 = st.columns([1, 2])
                target = k1.number_input("Tổng folder mục tiêu", min_value=1,
                                         value=int(cfg.get("project_total_folders", 800)))
                k2.success(f"💰 Đã chạy {len(ran)} folder · token thật {tokens:,} · "
                           f"TB ${avg:.4f}/folder → **dự phóng {target} folder ≈ ${avg*target:.2f}**\n\n"
                           "(số tiền chính xác xem Google AI Studio → Usage)")

        mode = st.radio("Hiển thị", ["Chỉ folder cần chú ý", "Tất cả"], horizontal=True)
        view = df if mode == "Tất cả" else df[df["status"] != "ok"]
        if view.empty:
            st.success("Không có folder nào cần chú ý 🎉")
        else:
            view = view.copy()
            view["status"] = view["status"].map(STATUS_VN).fillna(view["status"])
            view = view.rename(columns=COLS_VN).fillna("")
            st.dataframe(view, width="stretch", hide_index=True)
