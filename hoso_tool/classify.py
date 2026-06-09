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
    ]
    return "\n".join(lines)


# ============================================================
# GeminiClassifier (provider mặc định)
# ============================================================
class GeminiClassifier:
    """Classifier mặc định: Gemini đọc PDF scan native qua Files API."""

    def __init__(self, model: str, categories: list[dict],
                 api_key: str | None = None, media_resolution: str = "medium",
                 rules: list[str] | None = None):
        from google import genai  # import trễ để provider khác không cần SDK này

        key = api_key or os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        if not key:
            raise RuntimeError(
                "Thiếu GEMINI_API_KEY. Export biến môi trường trước khi chạy:\n"
                "  export GEMINI_API_KEY=...")
        self._genai = genai
        self.client = genai.Client(api_key=key)
        self.model = model
        self.media_resolution = media_resolution
        self.prompt = build_prompt(categories, rules)
        self.valid_categories = {c["key"] for c in categories} | {"khong_thuoc"}

    def classify(self, pdf_path: str) -> ClassifyResult:
        types = self._genai.types
        with open(pdf_path, "rb") as f:
            uploaded = self.client.files.upload(
                file=f,
                config=dict(mime_type="application/pdf")
            )
        try:
            cfg_kwargs = dict(
                temperature=0,
                response_mime_type="application/json",
                response_schema=list[_PageClass],
            )
            try:
                cfg = types.GenerateContentConfig(
                    media_resolution=f"MEDIA_RESOLUTION_{self.media_resolution.upper()}",
                    **cfg_kwargs,
                )
            except (TypeError, ValueError):
                cfg = types.GenerateContentConfig(**cfg_kwargs)

            resp = self.client.models.generate_content(
                model=self.model,
                contents=[uploaded, self.prompt],
                config=cfg,
            )
        finally:
            try:
                self.client.files.delete(name=uploaded.name)
            except Exception:
                pass

        labels = self._parse(resp)
        um = getattr(resp, "usage_metadata", None)
        pt = int(getattr(um, "prompt_token_count", 0) or 0)
        ot = int(getattr(um, "candidates_token_count", 0) or 0)
        tt = int(getattr(um, "total_token_count", 0) or (pt + ot))
        return ClassifyResult(labels=labels, prompt_tokens=pt, output_tokens=ot,
                              total_tokens=tt, provider_used="gemini")

    def _parse(self, resp) -> list[PageLabel]:
        raw = getattr(resp, "parsed", None)
        if raw is None:
            text = (resp.text or "").strip()
            if text.startswith("```"):
                text = text.strip("`")
                text = text[text.find("["):]
            raw = json.loads(text)

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
                 api_key: str | None = None, dpi: int = 150,
                 rules: list[str] | None = None):
        key = api_key or os.environ.get("OPENAI_API_KEY")
        if not key:
            raise RuntimeError(
                "Thiếu OPENAI_API_KEY. Export biến môi trường:\n"
                "  export OPENAI_API_KEY=sk-...")
        self.api_key = key
        self.model = model
        self.dpi = dpi
        self.prompt = build_prompt(categories, rules)
        self.valid_categories = {c["key"] for c in categories} | {"khong_thuoc"}

    def _pdf_to_images_b64(self, pdf_path: str) -> list[str]:
        """Render tất cả trang PDF thành PNG (base64). Cần pdftoppm (poppler)."""
        with tempfile.TemporaryDirectory() as d:
            root = os.path.join(d, "page")
            subprocess.run(
                ["pdftoppm", "-png", "-r", str(self.dpi), pdf_path, root],
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
        try:
            out = subprocess.check_output(["pdfinfo", pdf_path], stderr=subprocess.DEVNULL)
            for line in out.decode(errors="replace").splitlines():
                if line.startswith("Pages:"):
                    return int(line.split(":")[1].strip())
        except Exception:
            pass
        return 0

    def classify(self, pdf_path: str) -> ClassifyResult:
        try:
            from openai import OpenAI
        except ImportError as e:
            raise RuntimeError(
                "Chưa cài openai SDK. Chạy: pip install openai>=1.0") from e

        client = OpenAI(api_key=self.api_key)
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

        resp = client.chat.completions.create(
            model=self.model,
            messages=[{"role": "user", "content": content}],
            temperature=0,
            response_format={"type": "json_object"},
            max_tokens=4096,
        )

        raw_text = resp.choices[0].message.content or ""
        labels = self._parse(raw_text, n_pages)

        usage = resp.usage
        pt = int(getattr(usage, "prompt_tokens", 0) or 0)
        ot = int(getattr(usage, "completion_tokens", 0) or 0)
        return ClassifyResult(labels=labels, prompt_tokens=pt, output_tokens=ot,
                              total_tokens=pt + ot, provider_used="openai")

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
            # Fallback: trả khong_thuoc cho tất cả trang
            return [PageLabel(page=i, category="khong_thuoc", subtype="",
                              confidence=0.0, evidence="parse error")
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
            ))
        labels.sort(key=lambda x: x.page)
        return labels


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
def make_classifier(config: dict) -> Classifier:
    provider = config.get("provider", "gemini").lower()
    fallback_cfg = config.get("fallback", {})

    # Tạo primary classifier
    if provider == "gemini":
        primary: Classifier = GeminiClassifier(
            model=config["model"],
            categories=config["categories"],
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
            api_key=fallback_key,
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
