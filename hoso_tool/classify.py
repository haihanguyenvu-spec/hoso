"""Phân loại từng trang PDF bằng vision model.

Kiến trúc tách-vendor: mọi classifier hiện thực giao thức `Classifier`.
Đổi model = đổi 1 class, KHÔNG phải sửa pipeline.

Thứ tự fallback (nếu cấu hình):
  GeminiClassifier (key 1) → GeminiClassifier (key 2, account khác) khi key 1 bị 503/quá tải.
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import tempfile
from dataclasses import dataclass
from typing import Protocol

from pydantic import BaseModel


# ---- Kết quả phân loại 1 trang ----
@dataclass
class PageLabel:
    page: int            # số trang trong file (1-based)
    category: str        # key của loại, hoặc "khong_thuoc"
    subtype: str         # id sub-type, hoặc ""
    confidence: float    # 0..1
    evidence: str        # cụm chữ model thấy (để con người đối chiếu)
    rotation: int        # 0, 90, 180, 270 (số độ cần xoay để chữ nằm thẳng)


@dataclass
class ClassifyResult:
    """Nhãn các trang + SỐ TOKEN THẬT mà API báo về (để tính chi phí chính xác)."""
    labels: list[PageLabel]
    prompt_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    provider_used: str = ""   # "gemini" | "openai" | ... để log/UI biết ai làm


class Classifier(Protocol):
    """Nhận 1 file PDF, trả nhãn từng trang + token thật."""

    def classify(self, pdf_path: str) -> ClassifyResult:
        ...


# ---- Schema cho structured output ----
class _PageClass(BaseModel):
    page: int
    category: str
    subtype: str
    confidence: float
    evidence: str
    rotation: int


def _salvage_json_objects(text: str) -> list[dict]:
    """Cứu các object {...} HOÀN CHỈNH từ một mảng JSON bị cắt cụt.

    Khi response model quá dài bị cắt giữa chừng, json.loads hỏng toàn bộ. Hàm này
    quét theo độ sâu ngoặc để lấy ra tối đa các phần tử đã đóng đầy đủ (bỏ phần tử
    dở dang cuối). Trả list dict; [] nếu không cứu được gì.
    """
    out: list[dict] = []
    depth = 0
    start = -1
    in_str = False
    esc = False
    for i, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                try:
                    out.append(json.loads(text[start:i + 1]))
                except json.JSONDecodeError:
                    pass
                start = -1
    return out


def build_prompt(categories: list[dict], rules: list[str] | None = None) -> str:
    """Dựng prompt tiếng Việt liệt kê 6 loại + sub-type + từ khóa + quy tắc nghiệp vụ."""
    lines = [
        "Bạn là trợ lý phân loại hồ sơ pháp lý nhà đất của Việt Nam.",
        "Tài liệu là bản SCAN (ảnh), tiếng Việt có dấu. Hãy đọc kỹ tiêu đề/nội dung TỪNG TRANG.",
        "",
        "Phân loại MỖI trang vào đúng một trong các loại dưới đây (dùng đúng giá trị 'key').",
        "Nếu trang không thuộc loại nào (bìa, trang trắng, tờ ngăn) thì category = 'khong_thuoc'.",
        "",
        "DANH SÁCH LOẠI:",
    ]
    if rules:
        lines = lines[:-2] + ["QUY TẮC NGHIỆP VỤ QUAN TRỌNG (ưu tiên cao):"] \
            + [f"  {i+1}. {r.strip()}" for i, r in enumerate(rules)] \
            + ["", "DANH SÁCH LOẠI:"]
    for c in categories:
        lines.append(f"- key = {c['key']}  ({c['name']})")
        for s in c.get("subtypes", []):
            kw = ", ".join(s.get("keywords", []))
            lines.append(f"    • subtype = {s['id']}: {s['desc']}  | từ khóa: {kw}")
    lines += [
        "",
        "YÊU CẦU TRẢ VỀ:",
        "- Trả về JSON: một mảng, MỖI TRANG một phần tử, theo thứ tự trang tăng dần.",
        "- page: số trang (bắt đầu từ 1). Phải phủ HẾT mọi trang của file, không bỏ trang nào.",
        "- category: đúng một 'key' ở trên, hoặc 'khong_thuoc'.",
        "- subtype: đúng một 'subtype' của loại đó, hoặc '' nếu không chắc subtype.",
        "- confidence: 0..1 (độ chắc chắn của bạn cho trang đó).",
        "- evidence: trích NGẮN tiêu đề/cụm chữ tiếng Việt bạn nhìn thấy làm căn cứ.",
        "- rotation: số nguyên 0, 90, 180, hoặc 270. Hãy nhìn chiều của chữ trong ảnh. Nếu ảnh bị ngược 180 độ, trả về 180. Nếu nằm ngang, trả về 90 hoặc 270 tương ứng để xoay ảnh thẳng đứng lại. Nếu ảnh đã thẳng đứng (đọc được bình thường), trả về 0.",
    ]
    return "\n".join(lines)


# ============================================================
# GeminiClassifier (provider mặc định)
# ============================================================
class GeminiClassifier:
    """Classifier mặc định: Gemini đọc PDF scan native qua Files API. hỗ trợ xoay vòng nhiều API key."""

    def __init__(self, model: str, categories: list[dict],
                 api_keys: list[str] | None = None, media_resolution: str = "medium",
                 rules: list[str] | None = None):
        from google import genai  # import trễ để provider khác không cần SDK này

        # Nếu không truyền danh sách key, tìm từ môi trường hoặc key đơn
        keys = []
        if api_keys:
            keys = [k.strip() for k in api_keys if k.strip()]
        if not keys:
            env_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
            if env_key:
                keys = [env_key.strip()]
        
        if not keys:
            raise RuntimeError(
                "Thiếu GEMINI_API_KEY. Vui lòng nhập ít nhất một API key trên giao diện hoặc cấu hình.")
                
        self._genai = genai
        self.api_keys = keys
        self.clients = [genai.Client(api_key=k) for k in keys]
        self.current_idx = 0
        self.model = model
        self.media_resolution = media_resolution
        self.prompt = build_prompt(categories, rules)
        self.valid_categories = {c["key"] for c in categories} | {"khong_thuoc"}

    def classify(self, pdf_path: str) -> ClassifyResult:
        types = self._genai.types
        n_keys = len(self.clients)
        last_error = None

        for attempt in range(n_keys):
            idx = (self.current_idx + attempt) % n_keys
            client = self.clients[idx]
            
            try:
                with open(pdf_path, "rb") as f:
                    uploaded = client.files.upload(
                        file=f,
                        config=dict(mime_type="application/pdf")
                    )
                
                # Chờ file xử lý xong trên server của Gemini
                import time
                while uploaded.state.name == "PROCESSING":
                    time.sleep(2)
                    uploaded = client.files.get(name=uploaded.name)
                if uploaded.state.name == "FAILED":
                    client.files.delete(name=uploaded.name)
                    raise RuntimeError("File upload lên Gemini bị lỗi xử lý (FAILED).")

                try:
                    cfg_kwargs = dict(
                        temperature=0,
                        response_mime_type="application/json",
                        response_schema=list[_PageClass],
                        # Hồ sơ dài (>90 trang) sinh JSON lớn; nếu không nới trần
                        # output, response bị CẮT CỤT → thiếu nhãn các trang cuối.
                        max_output_tokens=int(os.environ.get("HOSO_MAX_OUTPUT_TOKENS", "65000")),
                    )
                    try:
                        cfg = types.GenerateContentConfig(
                            media_resolution=f"MEDIA_RESOLUTION_{self.media_resolution.upper()}",
                            **cfg_kwargs,
                        )
                    except (TypeError, ValueError):
                        cfg = types.GenerateContentConfig(**cfg_kwargs)

                    resp = client.models.generate_content(
                        model=self.model,
                        contents=[uploaded, self.prompt],
                        config=cfg,
                    )
                finally:
                    try:
                        client.files.delete(name=uploaded.name)
                    except Exception:
                        pass

                # Lưu lại index của key chạy thành công
                self.current_idx = idx
                labels = self._parse(resp)
                um = getattr(resp, "usage_metadata", None)
                pt = int(getattr(um, "prompt_token_count", 0) or 0)
                ot = int(getattr(um, "candidates_token_count", 0) or 0)
                tt = int(getattr(um, "total_token_count", 0) or (pt + ot))
                
                provider_str = "gemini" if n_keys == 1 else f"gemini (Key #{idx+1})"
                return ClassifyResult(labels=labels, prompt_tokens=pt, output_tokens=ot,
                                      total_tokens=tt, provider_used=provider_str)
            except Exception as e:
                last_error = e
                if n_keys > 1:
                    import logging
                    logging.getLogger("hoso").warning(
                        "API Key #%d bị lỗi (%s). Thử chuyển sang API Key #%d...",
                        idx + 1, str(e)[:120], ((idx + 1) % n_keys) + 1
                    )
                else:
                    raise

        raise last_error

    def _parse(self, resp) -> list[PageLabel]:
        raw = getattr(resp, "parsed", None)
        if raw is None:
            text = (resp.text or "").strip()
            if text.startswith("```"):
                text = text.strip("`")
                text = text[text.find("["):]
            try:
                raw = json.loads(text)
            except json.JSONDecodeError:
                # Response bị CẮT CỤT (JSON không đóng) → cứu các object hoàn chỉnh
                # thay vì hỏng toàn bộ. Tốt hơn là mất vài trang cuối còn hơn mất cả file.
                raw = _salvage_json_objects(text)
                if not raw:
                    raise

        labels: list[PageLabel] = []
        for item in raw:
            d = item.model_dump() if isinstance(item, _PageClass) else dict(item)
            cat = d.get("category", "khong_thuoc")
            if cat not in self.valid_categories:
                cat = "khong_thuoc"
            labels.append(PageLabel(
                page=int(d["page"]),
                category=cat,
                subtype=str(d.get("subtype") or ""),
                confidence=float(d.get("confidence", 0.0)),
                evidence=str(d.get("evidence") or "")[:200],
                rotation=int(d.get("rotation", 0)),
            ))
        labels.sort(key=lambda x: x.page)
        return labels


# ============================================================
# OpenAIClassifier — fallback với gpt-4o-mini
# Chi phí: $0.15/1M input · $0.60/1M output  (~rẻ hơn Gemini 2.5 Flash)
# PDF không native → render từng trang thành PNG bằng pdftoppm rồi gửi ảnh.
# ============================================================
class OpenAIClassifier:
    """Fallback sử dụng OpenAI gpt-4o-mini vision.

    Vì OpenAI không nhận PDF trực tiếp, mỗi trang được render thành PNG (300 DPI)
    bằng pdftoppm (poppler) rồi encode base64 và gửi lên API.
    Chi phí: ~$0.15/M input tokens + $0.60/M output tokens.
    """

    # Giá USD / 1 triệu token
    PRICE_IN  = 0.15
    PRICE_OUT = 0.60

    def __init__(self, model: str, categories: list[dict],
                 api_key: str | None = None, api_keys: list[str] | None = None,
                 dpi: int = 150, rules: list[str] | None = None,
                 base_url: str | None = None, provider_name: str = "openai",
                 env_key: str = "OPENAI_API_KEY"):
        keys = [k.strip() for k in (api_keys or []) if k and k.strip()]
        if not keys and api_key:
            keys = [api_key.strip()]
        if not keys:
            env = os.environ.get(env_key)
            if env:
                keys = [env.strip()]
        if not keys:
            raise RuntimeError(
                f"Thiếu API key cho {provider_name}. Nhập key trên UI hoặc export {env_key}=...")
        self.api_keys = keys
        self.model = model
        self.dpi = dpi
        self.base_url = base_url
        self.provider_name = provider_name
        self.prompt = build_prompt(categories, rules)
        self.valid_categories = {c["key"] for c in categories} | {"khong_thuoc"}

    def _pdf_to_images_b64(self, pdf_path: str) -> list[str]:
        """Render tất cả trang PDF thành PNG (base64). Cần pdftoppm (poppler)."""
        from assemble import safe_input_path, get_safe_temp_dir
        with tempfile.TemporaryDirectory(dir=get_safe_temp_dir()) as d:
            root = os.path.join(d, "page")
            with safe_input_path(pdf_path) as safe_path:
                subprocess.run(
                    ["pdftoppm", "-png", "-r", str(self.dpi), safe_path, root],
                    check=True, capture_output=True
                )
            pngs = sorted(f for f in os.listdir(d) if f.endswith(".png"))
            result = []
            for fname in pngs:
                with open(os.path.join(d, fname), "rb") as f:
                    result.append(base64.b64encode(f.read()).decode())
        return result

    def _page_count(self, pdf_path: str) -> int:
        """Đếm số trang PDF bằng pdfinfo."""
        from assemble import page_count
        try:
            return page_count(pdf_path)
        except Exception:
            pass
        return 0

    def classify(self, pdf_path: str) -> ClassifyResult:
        try:
            from openai import OpenAI
        except ImportError as e:
            raise RuntimeError(
                "Chưa cài openai SDK. Chạy: pip install openai>=1.0") from e

        images_b64 = self._pdf_to_images_b64(pdf_path)
        n_pages = len(images_b64)

        # Xây dựng message: prompt text + tất cả ảnh trang
        content: list[dict] = [{"type": "text", "text": self.prompt}]
        for idx, b64 in enumerate(images_b64, 1):
            content.append({
                "type": "text",
                "text": f"\n--- Trang {idx}/{n_pages} ---"
            })
            content.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{b64}", "detail": "auto"}
            })

        # Trần output đủ lớn cho hồ sơ dài (tránh cắt cụt JSON). Có thể chỉnh qua env.
        max_tok = int(os.environ.get("HOSO_OPENAI_MAX_TOKENS", "16000"))
        last_error = None
        for i, key in enumerate(self.api_keys):
            try:
                client = (OpenAI(api_key=key, base_url=self.base_url)
                          if self.base_url else OpenAI(api_key=key))
                resp = client.chat.completions.create(
                    model=self.model,
                    messages=[{"role": "user", "content": content}],
                    temperature=0,
                    response_format={"type": "json_object"},
                    max_tokens=max_tok,
                )
                raw_text = resp.choices[0].message.content or ""
                labels = self._parse(raw_text, n_pages)
                usage = resp.usage
                pt = int(getattr(usage, "prompt_tokens", 0) or 0)
                ot = int(getattr(usage, "completion_tokens", 0) or 0)
                pname = (self.provider_name if len(self.api_keys) == 1
                         else f"{self.provider_name} (Key #{i + 1})")
                return ClassifyResult(labels=labels, prompt_tokens=pt, output_tokens=ot,
                                      total_tokens=pt + ot, provider_used=pname)
            except Exception as e:
                last_error = e
                if len(self.api_keys) > 1:
                    import logging
                    logging.getLogger("hoso").warning(
                        "%s Key #%d lỗi (%s) — thử key kế...",
                        self.provider_name, i + 1, str(e)[:120])
                else:
                    raise
        raise last_error if last_error else RuntimeError("Không gọi được API")

    def _parse(self, raw_text: str, n_pages: int) -> list[PageLabel]:
        """Parse JSON từ OpenAI. Model trả về {"pages": [...]} hoặc trực tiếp [...]."""
        text = raw_text.strip()
        if text.startswith("```"):
            text = text.strip("`")
            idx = text.find("[")
            if idx == -1:
                idx = text.find("{")
            text = text[idx:] if idx != -1 else text

        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            # Response bị cắt cụt → cứu các object hoàn chỉnh trước khi bỏ cuộc.
            parsed = _salvage_json_objects(text)
            if not parsed:
                # Fallback cuối: trả khong_thuoc cho tất cả trang
                return [PageLabel(page=i, category="khong_thuoc", subtype="",
                                  confidence=0.0, evidence="parse error", rotation=0)
                        for i in range(1, n_pages + 1)]

        # Có thể là {"pages": [...]} hoặc {"results": [...]} hoặc trực tiếp [...]
        if isinstance(parsed, dict):
            for key in ("pages", "results", "data", "labels"):
                if key in parsed and isinstance(parsed[key], list):
                    parsed = parsed[key]
                    break
            else:
                # Tìm list đầu tiên trong dict
                for v in parsed.values():
                    if isinstance(v, list):
                        parsed = v
                        break

        labels: list[PageLabel] = []
        for item in parsed:
            if not isinstance(item, dict):
                continue
            cat = item.get("category", "khong_thuoc")
            if cat not in self.valid_categories:
                cat = "khong_thuoc"
            labels.append(PageLabel(
                page=int(item.get("page", 0)),
                category=cat,
                subtype=str(item.get("subtype") or ""),
                confidence=float(item.get("confidence", 0.0)),
                evidence=str(item.get("evidence") or "")[:200],
                rotation=int(item.get("rotation", 0)),
            ))
        labels.sort(key=lambda x: x.page)
        return labels


# ============================================================
# QwenClassifier — Qwen-VL qua DashScope (API tương thích OpenAI)
# Render mỗi trang thành ảnh (như OpenAI) rồi gửi. Mạnh về OCR chữ Á Đông.
# ============================================================
class QwenClassifier(OpenAIClassifier):
    """Qwen-VL (Alibaba DashScope). Dùng chung luồng render-ảnh của OpenAIClassifier,
    chỉ khác endpoint (base_url) và tên biến môi trường key."""

    DEFAULT_BASE_URL = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"

    def __init__(self, model: str, categories: list[dict],
                 api_key: str | None = None, api_keys: list[str] | None = None,
                 dpi: int = 150, rules: list[str] | None = None,
                 base_url: str | None = None):
        super().__init__(
            model=model, categories=categories, api_key=api_key, api_keys=api_keys,
            dpi=dpi, rules=rules, base_url=base_url or self.DEFAULT_BASE_URL,
            provider_name="qwen", env_key="DASHSCOPE_API_KEY")


# ============================================================
# ChainClassifier — thử lần lượt nhiều model/vendor (tự fallback chéo)
# ============================================================
class ChainClassifier:
    """Thử các classifier theo THỨ TỰ; lỗi ở cái này → chuyển sang cái kế.

    Dùng để nối nhiều model/nhà cung cấp theo chế độ (vd Gemini 3.5 → Qwen 3.7-plus).
    Mỗi classifier con đã tự xoay vòng key của nó; ChainClassifier lo fallback GIỮA
    các model/vendor.
    """

    def __init__(self, classifiers: list, labels: list[str] | None = None):
        self.classifiers = classifiers
        self.labels = labels or [getattr(c, "model", "?") for c in classifiers]

    def classify(self, pdf_path: str) -> ClassifyResult:
        import logging
        last_error = None
        for i, clf in enumerate(self.classifiers):
            try:
                return clf.classify(pdf_path)
            except Exception as e:
                last_error = e
                logging.getLogger("hoso").warning(
                    "Model '%s' lỗi (%s) — chuyển sang model kế tiếp...",
                    self.labels[i], str(e)[:140])
        raise last_error if last_error else RuntimeError("Không có model nào để chạy")


# ============================================================
# FallbackClassifier — tự động chuyển sang fallback khi primary lỗi
# ============================================================
_TRANSIENT_ERRORS = ("503", "UNAVAILABLE", "high demand",
                     "ResourceExhausted", "quota", "504", "502",
                     "rate_limit", "overloaded", "529")


def _is_transient(err: str) -> bool:
    return any(t in err for t in _TRANSIENT_ERRORS)


class FallbackClassifier:
    """Dùng primary (Gemini). Khi gặp lỗi tạm thời → tự chuyển sang fallback (OpenAI).

    Sau max_primary_fails lần liên tiếp thất bại ở primary → tạm chuyển sang
    fallback cho đến khi primary khỏe trở lại (reset sau mỗi lần primary thành công).
    """

    def __init__(self, primary: Classifier, fallback: Classifier,
                 max_primary_fails: int = 2):
        self.primary = primary
        self.fallback = fallback
        self.max_primary_fails = max_primary_fails
        self._consecutive_fails = 0   # đếm lỗi liên tiếp của primary

    def classify(self, pdf_path: str) -> ClassifyResult:
        # Nếu primary đang "ốm" → dùng fallback luôn
        if self._consecutive_fails >= self.max_primary_fails:
            try:
                result = self.fallback.classify(pdf_path)
                return result
            except Exception as fe:
                # Fallback cũng lỗi → ném lỗi gốc để make_retrying_classify xử lý
                raise RuntimeError(
                    f"Cả primary lẫn fallback đều lỗi. Fallback error: {fe}") from fe

        # Thử primary trước
        try:
            result = self.primary.classify(pdf_path)
            self._consecutive_fails = 0   # primary OK → reset
            return result
        except Exception as e:
            err_msg = str(e)
            if _is_transient(err_msg):
                self._consecutive_fails += 1
                import logging
                logging.getLogger("hoso").warning(
                    "Primary (Gemini) lỗi tạm thời lần %d/%d — chuyển sang fallback (OpenAI): %s",
                    self._consecutive_fails, self.max_primary_fails, err_msg[:120])
                try:
                    result = self.fallback.classify(pdf_path)
                    return result
                except Exception as fe:
                    raise RuntimeError(
                        f"Primary lỗi tạm thời, fallback cũng lỗi: {fe}") from fe
            else:
                # Lỗi không phải tạm thời (lỗi key, lỗi code...) → ném ra ngay
                raise


# ============================================================
# Factory
# ============================================================
def _provider_of_model(model: str) -> str:
    """Suy nhà cung cấp từ tiền tố model: qwen* -> qwen · gpt/o* -> openai · còn lại -> gemini."""
    m = (model or "").lower()
    if m.startswith("qwen"):
        return "qwen"
    if m.startswith(("gpt", "o1", "o3", "o4", "openai")):
        return "openai"
    return "gemini"


def _default_model_for_provider(provider: str, config: dict) -> str:
    """Model mặc định của 1 nhà cung cấp — dùng khi provider có key nhưng không nằm
    trong chế độ (làm fallback). Lấy từ config['provider_default_model'] nếu có."""
    dm = config.get("provider_default_model", {}) or {}
    if provider in dm:
        return dm[provider]
    return {"gemini": config.get("model", "gemini-2.5-flash"),
            "qwen": "qwen3.6-flash",
            "openai": "gpt-4o-mini"}.get(provider, config.get("model", "gemini-2.5-flash"))


def _make_one_classifier(model: str, keys: list[str], config: dict) -> Classifier:
    """Tạo 1 classifier cho 1 model (vendor suy ra từ tiền tố model)."""
    m = model.lower()
    cats, rules = config["categories"], config.get("rules")
    if m.startswith("qwen"):
        return QwenClassifier(model=model, categories=cats, api_keys=keys,
                              dpi=int(config.get("render_dpi", 150)), rules=rules,
                              base_url=config.get("qwen_base_url"))
    if m.startswith(("gpt", "o1", "o3", "o4", "openai")):
        return OpenAIClassifier(model=model, categories=cats, api_keys=keys,
                                dpi=int(config.get("render_dpi", 150)), rules=rules)
    # mặc định: Gemini (gemini-*)
    return GeminiClassifier(model=model, categories=cats, api_keys=keys,
                            media_resolution=config.get("media_resolution", "medium"), rules=rules)


def _make_classifier_from_specs(config: dict, key_specs: list[dict]) -> Classifier:
    """Dựng chuỗi classifier từ danh sách key (mỗi key gắn 1 NHÀ CUNG CẤP) + CHẾ ĐỘ.

    Key chỉ gắn provider (google/qwen/openai) vì cùng 1 key dùng được mọi model của
    nhà đó. CHẾ ĐỘ ('nhanh'/'chậm') quy định danh sách MODEL theo thứ tự; với mỗi
    model, lấy key của provider tương ứng để tạo classifier, rồi nối ChainClassifier
    (tự fallback chéo model/vendor; trong 1 provider thì xoay vòng nhiều key).
    """
    import logging
    log = logging.getLogger("hoso")
    by_provider: dict[str, list[str]] = {}
    for s in key_specs:
        prov = (s.get("provider") or "").strip().lower()
        key = (s.get("key") or "").strip()
        if not prov and s.get("model"):          # tương thích spec cũ {model,key}
            prov = _provider_of_model(s["model"])
        if prov and key:
            by_provider.setdefault(prov, []).append(key)
    if not by_provider:
        raise RuntimeError("Chưa có API key nào. Vào Cài đặt → nhập key và chọn nhà cung cấp.")

    mode = (config.get("mode") or config.get("default_mode") or "").lower()
    mode_models = config.get("modes", {}).get(mode, []) or [config.get("model", "gemini-2.5-flash")]

    chain, labels, used = [], [], set()
    # 1) Ưu tiên các model của CHẾ ĐỘ (nếu có key của provider tương ứng).
    for model in mode_models:
        prov = _provider_of_model(model)
        keys = by_provider.get(prov)
        if keys:
            chain.append(_make_one_classifier(model, keys, config))
            labels.append(model)
            used.add(prov)
    # 2) FALLBACK: provider nào CÓ KEY mà chưa dùng → thêm model mặc định của provider đó.
    #    (vd chế độ Chậm cần Qwen nhưng chỉ có key Gemini → vẫn chạy bằng Gemini.)
    for prov, keys in by_provider.items():
        if prov in used:
            continue
        model = _default_model_for_provider(prov, config)
        chain.append(_make_one_classifier(model, keys, config))
        labels.append(model)
        used.add(prov)

    if not chain:
        raise RuntimeError("Chưa có API key nào. Vào Cài đặt → nhập key và chọn nhà cung cấp.")
    if len(chain) == 1:
        return chain[0]
    log.info("Chế độ '%s' → thử lần lượt: %s", mode or "(mặc định)", " → ".join(labels))
    return ChainClassifier(chain, labels=labels)


def make_classifier(config: dict) -> Classifier:
    # Luồng MỚI: key_specs (mỗi key gắn 1 model) + mode. Ưu tiên nếu có.
    if config.get("key_specs"):
        return _make_classifier_from_specs(config, config["key_specs"])

    # ---- Tương thích ngược: provider/model + api_keys ----
    provider = config.get("provider", "gemini").lower()
    fallback_cfg = config.get("fallback", {})

    # Tạo primary classifier
    if provider == "gemini":
        primary: Classifier = GeminiClassifier(
            model=config["model"],
            categories=config["categories"],
            api_keys=config.get("api_keys", []),
            media_resolution=config.get("media_resolution", "medium"),
            rules=config.get("rules"),
        )
    elif provider == "openai":
        primary = OpenAIClassifier(
            model=config.get("model", "gpt-4o-mini"),
            categories=config["categories"],
            dpi=int(config.get("render_dpi", 150)),
            rules=config.get("rules"),
        )
    elif provider == "deepseek":
        raise NotImplementedError(
            "DeepSeek V4 Vision chưa được xác minh. Cung cấp model ID + endpoint rồi implement.")
    else:
        raise ValueError(f"provider không hỗ trợ: {provider}")

    # Nếu có cấu hình fallback → bọc trong FallbackClassifier
    fallback_provider = fallback_cfg.get("provider", "").lower()
    if fallback_provider == "gemini":
        # Key 2 từ biến môi trường riêng (account/project Google khác — cùng chất lượng)
        key_env = fallback_cfg.get("api_key_env", "GEMINI_API_KEY_2")
        fallback_key = os.environ.get(key_env) or fallback_cfg.get("api_key")
        if not fallback_key:
            import logging
            logging.getLogger("hoso").warning(
                "Fallback Gemini key không tìm thấy (%s) — chạy không có fallback.", key_env)
            return primary
        fallback: Classifier = GeminiClassifier(
            model=fallback_cfg.get("model", config["model"]),
            categories=config["categories"],
            api_keys=[fallback_key],
            media_resolution=fallback_cfg.get("media_resolution", config.get("media_resolution", "medium")),
            rules=config.get("rules"),
        )
        return FallbackClassifier(
            primary=primary,
            fallback=fallback,
            max_primary_fails=int(fallback_cfg.get("max_primary_fails", 2)),
        )
    elif fallback_provider == "openai":
        fallback = OpenAIClassifier(
            model=fallback_cfg.get("model", "gpt-4o-mini"),
            categories=config["categories"],
            dpi=int(fallback_cfg.get("render_dpi", 150)),
            rules=config.get("rules"),
        )
        return FallbackClassifier(
            primary=primary,
            fallback=fallback,
            max_primary_fails=int(fallback_cfg.get("max_primary_fails", 2)),
        )

    return primary
