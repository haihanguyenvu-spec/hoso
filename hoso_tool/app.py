"""Giao diện web (Streamlit) cho tool phân loại & ghép hồ sơ PDF căn hộ.

Chạy:  streamlit run hoso_tool/app.py
Mọi xử lý chạy LOCAL; ảnh hồ sơ chỉ gửi lên vision model ở bước "Phân loại".
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile

import pandas as pd
import streamlit as st
import yaml

sys.path.insert(0, os.path.dirname(__file__))
import pipeline  # noqa: E402
from classify import make_classifier  # noqa: E402
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
        return yaml.safe_load(f)


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


def load_keys() -> list[str]:
    import json
    p = key_path("keys.json")
    if os.path.exists(p):
        try:
            with open(p, encoding="utf-8") as f:
                keys = json.load(f)
                if isinstance(keys, list):
                    return [k.strip() for k in keys if k.strip()]
        except Exception:
            pass
    # Fallback legacy
    legacy = []
    k1 = get_api_key()
    if k1:
        legacy.append(k1)
    k2 = get_api_key_2()
    if k2:
        legacy.append(k2)
    return legacy


def save_keys(keys: list[str]):
    import json
    p = key_path("keys.json")
    try:
        with open(p, "w", encoding="utf-8") as f:
            json.dump([k.strip() for k in keys if k.strip()], f, ensure_ascii=False, indent=2)
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


# ---------- UI ----------
st.set_page_config(page_title="Phân loại hồ sơ PDF căn hộ", page_icon="📄", layout="wide", initial_sidebar_state="expanded")
# Ẩn các nút/chrome mặc định của Streamlit (menu ⋮, Deploy, footer "Made with Streamlit")
# -> giao diện chỉ còn các nút của tool.
st.markdown("""
<style>
#MainMenu {visibility: hidden;}
[data-testid="stToolbar"] {visibility: hidden; height: 0;}
[data-testid="stDecoration"] {display: none;}
footer {visibility: hidden;}
/* Bắt buộc hiển thị nút mở/đóng thanh bên */
[data-testid="collapsedControl"], [data-testid="stSidebarCollapseButton"] {
    visibility: visible !important;
    display: block !important;
    color: black !important;
    background-color: #f0f2f6 !important;
    border-radius: 4px;
}
</style>
""", unsafe_allow_html=True)
cfg = load_config()
name_by_key, key_by_name = label_maps(cfg)

if "api_keys" not in st.session_state:
    st.session_state.api_keys = load_keys() or [""]

st.sidebar.title("📂 Hồ sơ PDF căn hộ")
st.sidebar.caption(f"Model: **{cfg['provider']} / {cfg['model']}**")
st.sidebar.markdown("---")

with st.sidebar:
    st.markdown("**🔑 Danh sách API Keys (Gemini)**")
    
    new_keys_len = len(st.session_state.api_keys)
    to_delete = None
    
    for idx in range(new_keys_len):
        col_in, col_btn = st.columns([5, 1])
        with col_in:
            val = st.text_input(
                f"Key #{idx+1}",
                value=st.session_state.api_keys[idx],
                type="password",
                key=f"key_input_val_{idx}"
            ).strip()
            # Cập nhật giá trị vào session state ngay lập tức
            st.session_state.api_keys[idx] = val
        with col_btn:
            st.markdown("<div style='height: 28px;'></div>", unsafe_allow_html=True)
            if st.button("❌", key=f"del_key_{idx}", help=f"Xóa Key #{idx+1}"):
                to_delete = idx

    if to_delete is not None:
        st.session_state.api_keys.pop(to_delete)
        # Xóa widget states để tránh lưu dữ liệu rác
        for i in range(len(st.session_state.api_keys) + 1):
            if f"key_input_val_{i}" in st.session_state:
                del st.session_state[f"key_input_val_{i}"]
        if not st.session_state.api_keys:
            st.session_state.api_keys = [""]
        save_keys([k for k in st.session_state.api_keys if k])
        st.rerun()

    if st.button("➕ Thêm API Key", use_container_width=True):
        st.session_state.api_keys.append("")
        st.rerun()

    if st.button("💾 Lưu tất cả key", use_container_width=True):
        final_keys = [k for k in st.session_state.api_keys if k]
        st.session_state.api_keys = final_keys if final_keys else [""]
        save_keys(final_keys)
        st.toast("Đã lưu danh sách API Keys!")
        st.rerun()

    st.sidebar.markdown("---")
    st.sidebar.caption("© Nguyễn Vũ Hải Hà @ 2026")

# --- Ô nhập thư mục gốc (trước tabs, dùng chung cho cả 3 tab) ---
st.markdown("### 📁 Thư mục gốc chứa các folder căn hộ")
col_path, col_hint = st.columns([4, 1])
with col_path:
    input_root = st.text_input(
        "Đường dẫn thư mục gốc",
        value=cfg["input_root"],
        placeholder="/path/to/Cr8-3",
        help="Mỗi subfolder bên trong = 1 căn hộ. Nhập đường dẫn rồi nhấn Enter.",
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
        st.info("Không thấy folder căn hộ nào trong thư mục gốc. Hãy chắc chắn mỗi subfolder chứa ít nhất 1 file PDF.")
    else:
        rows = [{"Folder": os.path.basename(f), "Số PDF": len(pipeline.list_pdfs(f, cfg["output_subdir"])),
                 "Trạng thái": folder_status(f, cfg)} for f in folders]
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

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

        api_keys_active = [k for k in st.session_state.api_keys if k.strip()]
        btn_disabled = not (pick and api_keys_active)
        if st.button("▶ Phân loại & Tạo 6 file PDF", type="primary", disabled=btn_disabled):
            cfg["api_keys"] = api_keys_active
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
                    # Bước 1: Phân loại (gọi vision model)
                    entries = pipeline.classify_folder(f, cfg, classify)
                    # Bước 2: Ghép 6 file PDF ngay lập tức
                    status_box.info(f"⏳ **Đang xử lý folder {i}/{n_total}: {fname}** "
                                    f"— đang ghép 6 file PDF...")
                    res = pipeline.assemble_from_index(f, cfg, entries)
                    # Đánh dấu đã hoàn thành
                    open(done_marker, "w").close()
                    # Hiển thị kết quả
                    if res.reasons:
                        st.warning(f"⚠️ {fname}: {len(res.outputs)} file · {res.classified_pages}/{res.total_pages} trang · "
                                   + "; ".join(res.reasons))
                    else:
                        st.success(f"✅ {fname}: {len(res.outputs)} file · {res.classified_pages}/{res.total_pages} trang")
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
                use_container_width=True, hide_index=True, height=520, key=f"editor::{folder}")

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
                             caption=sel, use_container_width=True)
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
            st.dataframe(view, use_container_width=True, hide_index=True)
