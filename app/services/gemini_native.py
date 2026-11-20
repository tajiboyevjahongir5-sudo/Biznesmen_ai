"""Gemini'ning o'z (native) REST API si: Google qidiruvi va ovozni matnga aylantirish.

OpenAI-mos endpoint bu ikki imkoniyatni to'liq bermaydi, shuning uchun bu yerda
`generateContent` to'g'ridan-to'g'ri chaqiriladi (httpx — openai paketi bilan birga keladi,
yangi paket o'rnatilmagan). API kaliti faqat `x-goog-api-key` sarlavhasida yuboriladi,
URL ga yoki logga hech qachon tushmaydi.
"""

from __future__ import annotations

import base64
import logging
from dataclasses import dataclass

import httpx

from app.config import Settings

logger = logging.getLogger(__name__)


class GeminiNativeError(Exception):
    def __init__(self, status: int | None, message: str = "") -> None:
        super().__init__(f"status={status} {message[:200]}")
        self.status = status


@dataclass(frozen=True)
class WebSource:
    url: str
    title: str


@dataclass(frozen=True)
class NativeResult:
    text: str
    sources: list[WebSource]
    model: str


def _api_root(settings: Settings) -> str:
    base = settings.gemini_base_url.rstrip("/")
    return base.removesuffix("/openai")  # .../v1beta


def _retryable(exc: GeminiNativeError) -> bool:
    return exc.status is None or exc.status in (404, 429) or exc.status >= 500


class GeminiNative:
    def __init__(self, settings: Settings, http: httpx.AsyncClient | None = None) -> None:
        self._s = settings
        self._http = http

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=self._s.openai_timeout_seconds)
        return self._http

    async def _generate(self, model: str, body: dict) -> NativeResult:
        key = self._s.gemini_api_key.get_secret_value()
        url = f"{_api_root(self._s)}/models/{model}:generateContent"
        try:
            resp = await self._client().post(url, json=body, headers={"x-goog-api-key": key})
        except httpx.TimeoutException as exc:
            raise GeminiNativeError(None, "timeout") from exc
        except httpx.HTTPError as exc:
            raise GeminiNativeError(None, type(exc).__name__) from exc
        if resp.status_code != 200:
            detail = ""
            try:
                detail = str(resp.json().get("error", {}).get("message", ""))
            except ValueError:
                pass
            raise GeminiNativeError(resp.status_code, detail)
        data = resp.json()
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
