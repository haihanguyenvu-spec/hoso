"""Giao diện web (Streamlit) cho tool phân loại & ghép hồ sơ PDF căn hộ.

Chạy:  streamlit run hoso_tool/app.py
Mọi xử lý chạy LOCAL; ảnh hồ sơ chỉ gửi lên vision model ở bước "Phân loại".
"""
from __future__ import annotations

import copy
import os
import subprocess
import sys
import tempfile
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
def discover(input_root: str, cfg: dict) -> list[str]:
    out_subdir = cfg.get("output_subdir", "output")
    res = []
    if not os.path.isdir(input_root):
        return res
    for name in sorted(os.listdir(input_root)):
        d = os.path.join(input_root, name)
        if os.path.isdir(d) and name not in (out_subdir, "_review") \
                and pipeline.list_pdfs(d, out_subdir):
            res.append(d)
    return res


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

folders = discover(input_root, cfg)

st.divider()
tab1, tab2, tab3 = st.tabs(["① Phân loại", "② Review & Sửa nhãn", "③ Tổng kết"])


# ===== Tab 1: Phân loại =====
with tab1:
    st.subheader("Phân loại trang bằng vision model")

    if not folders:
        st.info("Không thấy folder hồ sơ nào trong thư mục gốc. Hãy chắc chắn mỗi subfolder chứa ít nhất 1 file PDF.")
    else:
        rows = [{"Folder": os.path.basename(f), "Số PDF": len(pipeline.list_pdfs(f, cfg["output_subdir"])),
                 "Trạng thái": folder_status(f, cfg)} for f in folders]
        st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)

        todo = [f for f in folders if "chưa chạy" in folder_status(f, cfg)]
        done_folders = [f for f in folders if "đã ghép" in folder_status(f, cfg)]

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

        out_subdir = cfg.get("output_subdir", "output")
        key_specs_active = [{"provider": s.get("provider", DEFAULT_PROVIDER), "key": s["key"].strip()}
                            for s in st.session_state.key_specs if s.get("key", "").strip()]
        # Nếu chế độ ưu tiên nhà cung cấp chưa có key → vẫn chạy bằng key sẵn có (fallback), chỉ báo nhẹ.
        _need = {_provider_of_model(m) for m in cfg.get("modes", {}).get(cfg.get("mode", ""), [])}
        _have = {s["provider"] for s in key_specs_active}
        if key_specs_active and _need and not (_have & _need):
            _lbl = ", ".join(PROVIDER_LABELS.get(p, p) for p in _need)
            st.info(f"ℹ️ Chế độ **{cfg.get('mode')}** ưu tiên **{_lbl}** (chưa có key) → "
                    "sẽ chạy bằng key hiện có làm dự phòng. Thêm key ở ⚙️ Cài đặt nếu muốn dùng đúng model.")
        btn_disabled = not (pick and key_specs_active)
        if st.button("▶ Phân loại & Tạo 6 file PDF", type="primary", disabled=btn_disabled):
            st.toast("⏳ Đang khởi tạo bộ xử lý...", icon="🚀")
            with st.spinner("Đang chuẩn bị mô hình..."):
                cfg["key_specs"] = key_specs_active
                classifier = make_classifier(cfg)
                classify = make_retrying_classify(classifier, int(cfg.get("max_retries", 4)))
                sel = [f for f in folders if os.path.basename(f) in pick]
                n_total = len(sel)
            bar = st.progress(0.0)
            status_box = st.empty()
            n_err = 0
            n_skip = 0
            for i, f in enumerate(sel, 1):
                fname = os.path.basename(f)
                done_marker = os.path.join(f, out_subdir, ".done")

                # --- Bỏ qua folder đã xong (trừ khi bật force_rerun) ---
                if os.path.exists(done_marker) and not force_rerun:
                    n_skip += 1
                    st.info(f"⏭️ Bỏ qua **{fname}** — đã ghép trước đó (bật 'Chạy lại folder đã xong' để xử lý lại).")
                    bar.progress(i / n_total)
                    continue

                # --- Banner tiến độ ---
                status_box.info(f"⏳ **Đang xử lý folder {i}/{n_total}: {fname}** "
                                f"— đang gọi vision model, vui lòng chờ...")
                try:
                    from pipeline import process_folder
                    res = process_folder(f, cfg, classify, dry_run=False)
                    
                    # Đánh dấu đã hoàn thành
                    if res.status in ("ok", "flagged"):
                        open(done_marker, "w").close()
                        
                    # Hiển thị kết quả
                    if res.reasons:
                        st.warning(f"⚠️ {fname}: {len(res.outputs)} file · {res.classified_pages}/{res.total_pages} trang · "
                                   + "; ".join(res.reasons))
                    else:
                        st.success(f"✅ {fname}: {len(res.outputs)} file · {res.classified_pages}/{res.total_pages} trang")
                        
                    # Ghi nhận vào báo cáo tổng
                    from run import write_summary, est_cost
                    review_dir = os.path.join(input_root, "_review")
                    os.makedirs(review_dir, exist_ok=True)
                    summ_path = os.path.join(review_dir, "summary.csv")
                    
                    # Đọc summary cũ (nếu có)
                    import csv
                    rows = []
                    if os.path.exists(summ_path):
                        with open(summ_path, "r", encoding="utf-8-sig") as csv_f:
                            reader = csv.DictReader(csv_f)
                            rows = list(reader)
                    
                    # Xóa dòng cũ của folder này (nếu đã từng chạy)
                    rows = [row for row in rows if row.get("folder") != res.prefix]
                    
                    # Thêm dòng mới
                    new_row = {
                        "folder": res.prefix,
                        "status": res.status,
                        "sample_check": "",
                        "total_pages": str(res.total_pages),
                        "classified_pages": str(res.classified_pages),
                        "low_conf": str(res.low_conf_pages),
                        "missing": ", ".join(res.categories_missing),
                        "est_cost_usd": f"{est_cost(res.total_pages, cfg):.4f}",
                        "real_tokens": str(res.total_tokens),
                        "real_cost_usd": f"{res.real_cost:.4f}",
                        "reasons": " | ".join(res.reasons) or res.error
                    }
                    rows.append(new_row)
                    rows.sort(key=lambda x: x.get("folder", ""))
                    
                    # Ghi lại file
                    if rows:
                        with open(summ_path, "w", newline="", encoding="utf-8-sig") as csv_f:
                            w = csv.DictWriter(csv_f, fieldnames=list(rows[0].keys()))
                            w.writeheader()
                            w.writerows(rows)
                            
                except Exception as e:
                    n_err += 1
                    msg = str(e)
                    if any(t in msg for t in ("503", "UNAVAILABLE", "high demand",
                                               "fallback cũng lỗi", "Cả primary lẫn fallback")):
                        both = "cả 2 key đều lỗi" if cfg.get("fallback", {}).get("provider") == "gemini" else "503"
                        st.error(f"✗ {fname}: Gemini quá tải ({both}). "
                                 "Đợi vài phút rồi bấm chạy lại — folder chưa xong sẽ tự chạy lại.")
                    else:
                        st.error(f"✗ {fname}: {msg}")
                bar.progress(i / n_total)

            # Tổng kết
            if n_err:
                status_box.warning(f"⚠️ Hoàn thành với {n_err} folder lỗi. Bấm chạy lại để xử lý các folder chưa xong.")
            elif n_skip == n_total:
                status_box.info(f"ℹ️ Tất cả {n_total} folder đã được xử lý trước đó — không có gì mới. "
                                "Bật 'Chạy lại folder đã xong' nếu muốn xử lý lại.")
            else:
                processed = n_total - n_skip - n_err
                status_box.success(f"🎉 Hoàn thành {processed} folder mới"
                                   + (f" (bỏ qua {n_skip} folder đã xong)" if n_skip else "")
                                   + "! Sang tab ② để review nhãn nếu cần.")


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
