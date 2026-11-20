"""Gemini'ning o'z (native) REST API si: Google qidiruvi, ovozni matnga aylantirish, rasm.

OpenAI-mos endpoint bu ikki imkoniyatni to'liq bermaydi, shuning uchun bu yerda
`generateContent` to'g'ridan-to'g'ri chaqiriladi (httpx — openai paketi bilan birga keladi,
yangi paket o'rnatilmagan). API kaliti faqat `x-goog-api-key` sarlavhasida yuboriladi,
URL ga yoki logga hech qachon tushmaydi.
"""

from __future__ import annotations

import base64
import binascii
import logging
from dataclasses import dataclass

import httpx

from app.config import Settings

logger = logging.getLogger(__name__)


class GeminiNativeError(Exception):
    def __init__(self, status: int | None, message: str = "") -> None:
        super().__init__(f"status={status} {message[:200]}")
        self.status = status
        self.detail = message[:600]  # Google izohi (kalit bo'lmaydi) — admin diagnostikasi uchun
        self.tried: list[str] = []  # rasm: sinab ko'rilgan modellar


@dataclass(frozen=True)
class WebSource:
    url: str
    title: str


@dataclass(frozen=True)
class NativeResult:
    text: str
    sources: list[WebSource]
    model: str


@dataclass(frozen=True)
class GeneratedImage:
    data: bytes
    mime: str
    model: str  # AI modeli yoki "pexels"
    credit: str = ""  # tayyor HTML: muallif va manba (stok foto uchun)

    @property
    def filename(self) -> str:
        return "idea." + IMAGE_TYPES.get(self.mime, "png")


IMAGE_TYPES = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}
MAX_IMAGE_BYTES = 9 * 1024 * 1024  # Telegram rasm limiti 10 MB
MAX_IMAGE_MODEL_TRIES = 4


def image_error_hint(exc: GeminiNativeError) -> str:
    """Rasm xatosini admin uchun oddiy tilda tushuntiradi."""
    detail = exc.detail.lower()
    if exc.status == 429 and ("limit: 0" in detail or "free_tier" in detail or "free tier" in detail):
        return (
            "Bu API kalit bepul tarifda: Google rasm modellari uchun bepul kvota bermaydi (limit 0). "
            "Rasm uchun Google AI Studio'da shu kalit loyihasiga billing (to'lov) yoqilishi kerak."
        )
    if exc.status == 429:
        return "Rasm modeli kvotasi tugagan yoki juda ko'p so'rov bo'ldi. Keyinroq qayta urinib ko'ring."
    if exc.status == 404:
        return "Kalitga ochiq rasm modeli topilmadi. /aitest bilan ro'yxatni ko'ring."
    if exc.status == 403:
        return "Kalitga bu modeldan foydalanish ruxsati yo'q (API yoki hudud cheklovi)."
    if exc.status == 400:
        return "Model bu so'rovni qabul qilmadi — /post_images model bilan boshqa model tanlang."
    if exc.status == 422:
        return "Model rasm qaytarmadi (xavfsizlik filtri). Qayta urinib ko'ring."
    return "Tarmoq yoki Google serveri xatosi. Keyinroq qayta urinib ko'ring."


def _api_root(settings: Settings) -> str:
    base = settings.gemini_base_url.rstrip("/")
    return base.removesuffix("/openai")  # .../v1beta


def _retryable(exc: GeminiNativeError) -> bool:
    return exc.status is None or exc.status in (404, 429) or exc.status >= 500


class GeminiNative:
    def __init__(self, settings: Settings, http: httpx.AsyncClient | None = None) -> None:
        self._s = settings
        self._http = http
        self._detected_image_model = ""  # oxirgi muvaffaqiyatli rasm modeli
        self._image_model_list: list[str] = []

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=self._s.openai_timeout_seconds)
        return self._http

    async def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        key = self._s.gemini_api_key.get_secret_value()
        url = f"{_api_root(self._s)}/{path}"
        try:
            resp = await self._client().request(method, url, json=body, headers={"x-goog-api-key": key})
        except httpx.TimeoutException as exc:
            raise GeminiNativeError(None, "timeout") from exc
        except httpx.HTTPError as exc:
            raise GeminiNativeError(None, type(exc).__name__) from exc
        if resp.status_code != 200:
            detail = ""
            try:
                detail = str(resp.json().get("error", {}).get("message", ""))
            except (ValueError, AttributeError):
                pass
            raise GeminiNativeError(resp.status_code, detail)
        try:
            data = resp.json()
        except ValueError as exc:
            raise GeminiNativeError(502, "javob JSON emas") from exc
        return data if isinstance(data, dict) else {}

    async def _generate(self, model: str, body: dict) -> NativeResult:
        data = await self._request("POST", f"models/{model}:generateContent", body)
        cand = (data.get("candidates") or [{}])[0]
        parts = (cand.get("content") or {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts if isinstance(p, dict)).strip()
        sources: list[WebSource] = []
        seen: set[str] = set()
        for chunk in (cand.get("groundingMetadata") or {}).get("groundingChunks") or []:
            web = chunk.get("web") or {}
            uri = str(web.get("uri", ""))
            if uri.startswith("https://") and uri not in seen:
                seen.add(uri)
                sources.append(WebSource(uri, str(web.get("title") or uri)[:200]))
        return NativeResult(text, sources[:12], model)

    async def generate(self, body: dict) -> NativeResult:
        """Asosiy va zaxira modellarni navbat bilan sinaydi."""
        last: GeminiNativeError | None = None
        for model in self._s.gemini_models:
            try:
                return await self._generate(model, body)
            except GeminiNativeError as exc:
                if not _retryable(exc):
                    raise
                last = exc
                logger.warning("gemini_native_failed model=%s status=%s", model, exc.status)
        assert last is not None
        raise last

    async def search(self, question: str, max_tokens: int = 3000) -> NativeResult:
        """Google qidiruvi bilan javob (grounding). Manbalar — API qaytargan haqiqiy havolalar."""
        body = {
            "contents": [{"role": "user", "parts": [{"text": question}]}],
            "tools": [{"google_search": {}}],
            "generationConfig": {"maxOutputTokens": max_tokens, "temperature": 0.2},
        }
        return await self.generate(body)

    async def transcribe(self, audio: bytes, mime: str) -> str:
        body = {
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {
                            "text": "Bu Telegram ovozli xabari. Uni so'zma-so'z matnga aylantir. "
                            "O'zbekcha bo'lsa — lotin yozuvida yoz. Faqat aytilgan matnni qaytar, "
                            "izoh qo'shma. Nutq bo'lmasa — bo'sh javob qaytar."
                        },
                        {"inline_data": {"mime_type": mime, "data": base64.b64encode(audio).decode()}},
                    ],
                }
            ],
            "generationConfig": {"maxOutputTokens": 2000, "temperature": 0},
        }
        return (await self.generate(body)).text

    # ------------------------------------------------------------------ rasm

    async def image_models(self) -> list[str]:
        """Kalitga ochiq, rasm qaytara oladigan Gemini modellari (eng mosi birinchi)."""
        data = await self._request("GET", "models?pageSize=1000")
        names: list[str] = []
        for m in data.get("models") or []:
            name = str(m.get("name", "")).removeprefix("models/")
            methods = m.get("supportedGenerationMethods") or []
            if "image" in name and "generateContent" in methods and not name.startswith("imagen"):
                names.append(name)
        names.sort(reverse=True)  # yangi versiyalar oldinda
        # Arzonroq "flash" va barqaror (preview/exp bo'lmagan) modellar ustun.
        names.sort(key=lambda n: ("flash" not in n, "preview" in n or "exp" in n))
        return names

    async def _image_candidates(self) -> list[str]:
        """Sinab ko'riladigan rasm modellari: qo'lda tanlangani yoki avtomatik (ishlagani birinchi)."""
        if self._s.gemini_image_model:
            return [self._s.gemini_image_model]
        if not self._image_model_list:
            self._image_model_list = await self.image_models()
            logger.info("gemini_image_models_detected count=%d", len(self._image_model_list))
        models = self._image_model_list[:MAX_IMAGE_MODEL_TRIES]
        if self._detected_image_model in models:
            models.remove(self._detected_image_model)
            models.insert(0, self._detected_image_model)
        if not models:
            raise GeminiNativeError(404, "kalitga ochiq rasm yaratadigan model topilmadi")
        return models

    async def generate_image(self, prompt: str, aspect_ratio: str = "16:9") -> GeneratedImage:
        """Matn bo'yicha rasm yaratadi. Model kvotasi/ruxsati bo'lmasa, keyingi rasm modelini sinaydi."""
        last: GeminiNativeError | None = None
        tried: list[str] = []
        for model in await self._image_candidates():
            tried.append(model)
            try:
                image = await self._generate_image_with(model, prompt, aspect_ratio)
            except GeminiNativeError as exc:
                if exc.status not in (403, 404, 429):
                    exc.tried = tried
                    raise
                last = exc
                logger.warning("gemini_image_model_failed model=%s status=%s", model, exc.status)
                continue
            self._detected_image_model = model
            return image
        assert last is not None
        last.tried = tried
        raise last

    async def _generate_image_with(self, model: str, prompt: str, aspect_ratio: str) -> GeneratedImage:
        config: dict = {"responseModalities": ["TEXT", "IMAGE"], "imageConfig": {"aspectRatio": aspect_ratio}}
        body = {"contents": [{"role": "user", "parts": [{"text": prompt}]}], "generationConfig": config}
        path = f"models/{model}:generateContent"
        try:
            data = await self._request("POST", path, body)
        except GeminiNativeError as exc:
            if exc.status != 400:
                raise
            config.pop("imageConfig")  # eski modellar o'lcham sozlamasini qabul qilmaydi
            data = await self._request("POST", path, body)
        cand = (data.get("candidates") or [{}])[0]
        for part in (cand.get("content") or {}).get("parts") or []:
            inline = (part.get("inlineData") or part.get("inline_data")) if isinstance(part, dict) else None
            if not inline or not inline.get("data"):
                continue
            mime = str(inline.get("mimeType") or inline.get("mime_type") or "image/png").lower()
            if mime not in IMAGE_TYPES:
                continue
            try:
                raw = base64.b64decode(inline["data"], validate=True)
            except (binascii.Error, ValueError) as exc:
                raise GeminiNativeError(502, "rasm ma'lumoti buzilgan") from exc
            if not raw or len(raw) > MAX_IMAGE_BYTES:
                raise GeminiNativeError(502, "rasm hajmi noto'g'ri")
            return GeneratedImage(raw, mime, model)
        reason = str(cand.get("finishReason") or (data.get("promptFeedback") or {}).get("blockReason") or "")
        raise GeminiNativeError(422, f"javobda rasm yo'q {reason}".strip())
