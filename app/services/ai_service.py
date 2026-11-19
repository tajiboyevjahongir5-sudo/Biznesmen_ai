"""OpenAI bilan ishlash uchun yagona, xavfsiz qatlam.

Vazifalari:
- timeout va retry (OpenAI SDK orqali);
- har bir so'rovda maksimal chiqish tokeni;
- foydalanuvchi va global kunlik limitlar, taxminiy xarajat byudjeti;
- structured output (Pydantic) va natijani tekshirish;
- barcha SDK xatolarini ichki, xavfsiz xato turlariga aylantirish.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import TypeVar

import openai
from pydantic import BaseModel, ValidationError

from app.config import Settings
from app.core.logging import user_ref

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


# ---------------------------------------------------------------------------
# Xato turlari — har biri foydalanuvchiga tushunarli o'zbekcha xabarga ega
# ---------------------------------------------------------------------------


class AIServiceError(Exception):
    user_message = "AI xizmatida kutilmagan xatolik yuz berdi. Birozdan keyin qayta urinib ko'ring."


class AITimeoutError(AIServiceError):
    user_message = "AI javob berishga ulgurmadi. Iltimos, birozdan keyin qayta urinib ko'ring."


class AIUnavailableError(AIServiceError):
    user_message = (
        "AI xizmatiga ulanib bo'lmadi (internet yoki server muammosi). Birozdan keyin qayta urinib ko'ring."
    )


class AIRateLimitedError(AIServiceError):
    user_message = "AI xizmati hozir band. Bir daqiqadan so'ng qayta urinib ko'ring."


class AIQuotaExceededError(AIServiceError):
    user_message = "AI xizmatining limiti tugagan. Administrator API balansini tekshirishi kerak."


class AIConfigError(AIServiceError):
    user_message = "AI xizmati sozlanmagan. Administratorga murojaat qiling."


class AIBadResponseError(AIServiceError):
    user_message = "AI noto'g'ri formatdagi javob qaytardi. Iltimos, qayta urinib ko'ring."


class UsageLimitError(AIServiceError):
    user_message = "Bugungi AI so'rovlar limitingiz tugadi. Ertaga qayta urinib ko'ring."


class GlobalLimitError(AIServiceError):
    user_message = (
        "Bot bugungi umumiy AI limitiga yetdi. Xarajatlarni nazorat qilish uchun "
        "yangi so'rovlar ertaga qabul qilinadi."
    )


# ---------------------------------------------------------------------------
# Foydalanish va xarajat hisobi
# ---------------------------------------------------------------------------


@dataclass
class UsageTracker:
    """Kunlik so'rovlar soni, tokenlar va taxminiy xarajat (xotirada)."""

    per_user_limit: int
    global_limit: int
    daily_budget_usd: float = 0.0
    input_price_per_1m: float = 0.0
    output_price_per_1m: float = 0.0
    _day: date = field(default_factory=lambda: datetime.now(UTC).date())
    _user_requests: dict[int, int] = field(default_factory=dict)
    global_requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0

    def _roll_day(self) -> None:
        today = datetime.now(UTC).date()
        if today != self._day:
            self._day = today
            self._user_requests.clear()
            self.global_requests = 0
            self.input_tokens = 0
            self.output_tokens = 0
            self.cost_usd = 0.0

    def check_and_reserve(self, user_id: int) -> None:
        """So'rov oldidan limitlarni tekshiradi va so'rovni band qiladi."""
        self._roll_day()
        if self.global_requests >= self.global_limit:
            raise GlobalLimitError
        if self.daily_budget_usd > 0 and self.cost_usd >= self.daily_budget_usd:
            raise GlobalLimitError
        used = self._user_requests.get(user_id, 0)
        if used >= self.per_user_limit:
            raise UsageLimitError
        self._user_requests[user_id] = used + 1
        self.global_requests += 1

    def record_tokens(self, input_tokens: int, output_tokens: int) -> float:
        """Token sarfini qayd qiladi va shu so'rovning taxminiy narxini qaytaradi."""
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        cost = (input_tokens * self.input_price_per_1m + output_tokens * self.output_price_per_1m) / 1_000_000
        self.cost_usd += cost
        return cost

    def remaining_for(self, user_id: int) -> int:
        self._roll_day()
        return max(0, self.per_user_limit - self._user_requests.get(user_id, 0))


# ---------------------------------------------------------------------------
# AI servis
# ---------------------------------------------------------------------------


class AIService:
    """OpenAI Responses API ustidagi structured-output qatlam."""

    def __init__(
        self, settings: Settings, client: openai.AsyncOpenAI | None = None, usage: UsageTracker | None = None
    ) -> None:
        self._settings = settings
        self._client = client
        self.usage = usage or UsageTracker(
            per_user_limit=settings.ai_daily_requests_per_user,
            global_limit=settings.ai_daily_requests_global,
            daily_budget_usd=settings.ai_daily_budget_usd,
            input_price_per_1m=settings.openai_input_price_per_1m,
            output_price_per_1m=settings.openai_output_price_per_1m,
        )

    def _get_client(self) -> openai.AsyncOpenAI:
        if self._client is None:
            key = self._settings.openai_api_key.get_secret_value()
            if not key or not self._settings.openai_model:
                raise AIConfigError
            self._client = openai.AsyncOpenAI(
                api_key=key,
                base_url=self._settings.openai_base_url or None,
                timeout=self._settings.openai_timeout_seconds,
                max_retries=self._settings.openai_max_retries,
            )
        return self._client

    @property
    def web_search_available(self) -> bool:
        return self._settings.web_search_enabled

    async def structured(
        self,
        *,
        user_id: int,
        agent: str,
        instructions: str,
        user_input: str,
        schema: type[T],
        max_output_tokens: int | None = None,
    ) -> T:
        """Bitta AI chaqiruvi: natija `schema` bo'yicha tekshirilgan obyekt.

        `instructions` — tizim ko'rsatmasi (developer roli), `user_input` —
        allaqachon `wrap_untrusted` bilan o'ralgan foydalanuvchi ma'lumoti.
        """
        result = await self.structured_with_sources(
            user_id=user_id,
            agent=agent,
            instructions=instructions,
            user_input=user_input,
            schema=schema,
            max_output_tokens=max_output_tokens,
            web_search=False,
        )
        return result.data

    async def structured_with_sources(
        self,
        *,
        user_id: int,
        agent: str,
        instructions: str,
        user_input: str,
        schema: type[T],
        max_output_tokens: int | None = None,
        web_search: bool = False,
    ) -> AIResult[T]:
        """`structured` bilan bir xil, lekin internet qidiruvi va manbalarni qaytaradi.

        Internet qidiruvi faqat `RESEARCH_PROVIDER=openai_web` bo'lsa yoqiladi.
        Qaytgan manbalar — haqiqatda API qaytargan URL lar (AI o'ylab topmagan).
        """
        client = self._get_client()
        self.usage.check_and_reserve(user_id)
        limit = min(
            max_output_tokens or self._settings.openai_max_output_tokens,
            self._settings.openai_max_output_tokens,
        )
        use_web = web_search and self.web_search_available
        extra: dict = {"tools": [{"type": "web_search"}]} if use_web else {}
        started = time.perf_counter()
        try:
            response = await client.responses.parse(
                model=self._settings.openai_model,
                instructions=instructions,
                input=[{"role": "user", "content": user_input}],
                text_format=schema,
                max_output_tokens=limit,
                store=False,
                **extra,
            )
        except openai.APITimeoutError as exc:
            self._log_failure(agent, user_id, exc)
            raise AITimeoutError from exc
        except openai.APIConnectionError as exc:
            self._log_failure(agent, user_id, exc)
            raise AIUnavailableError from exc
        except openai.RateLimitError as exc:
            self._log_failure(agent, user_id, exc)
            if _error_code(exc) == "insufficient_quota":
                raise AIQuotaExceededError from exc
            raise AIRateLimitedError from exc
        except (openai.AuthenticationError, openai.PermissionDeniedError, openai.NotFoundError) as exc:
            # Noto'g'ri kalit yoki mavjud bo'lmagan model nomi
            self._log_failure(agent, user_id, exc)
            raise AIConfigError from exc
        except openai.APIStatusError as exc:
            self._log_failure(agent, user_id, exc)
            if exc.status_code >= 500:
                raise AIUnavailableError from exc
            raise AIServiceError from exc
        except (ValidationError, ValueError) as exc:
            self._log_failure(agent, user_id, exc)
            raise AIBadResponseError from exc
        except openai.OpenAIError as exc:
            self._log_failure(agent, user_id, exc)
            raise AIServiceError from exc

        usage = getattr(response, "usage", None)
        in_tok = int(getattr(usage, "input_tokens", 0) or 0)
        out_tok = int(getattr(usage, "output_tokens", 0) or 0)
        cost = self.usage.record_tokens(in_tok, out_tok)
        logger.info(
            "ai_call agent=%s user=%s in_tokens=%d out_tokens=%d cost_usd=%.5f ms=%d",
            agent,
            user_ref(user_id),
            in_tok,
            out_tok,
            cost,
            int((time.perf_counter() - started) * 1000),
        )

        parsed = getattr(response, "output_parsed", None)
        if parsed is None:
            status = getattr(response, "status", None)
            logger.warning("ai_empty_output agent=%s status=%s", agent, status)
            raise AIBadResponseError
        if not isinstance(parsed, schema):
            try:
                parsed = schema.model_validate(parsed)
            except ValidationError as exc:
                raise AIBadResponseError from exc
        sources = extract_citations(response) if use_web else []
        return AIResult(data=parsed, sources=sources, web_used=use_web)

    @staticmethod
    def _log_failure(agent: str, user_id: int, exc: BaseException) -> None:
        # Faqat xato turi va status — so'rov matni yoki kalit loglanmaydi.
        status = getattr(exc, "status_code", None)
        logger.warning(
            "ai_call_failed agent=%s user=%s error=%s status=%s",
            agent,
            user_ref(user_id),
            type(exc).__name__,
            status,
        )


@dataclass(frozen=True)
class Source:
    url: str
    title: str


@dataclass(frozen=True)
class AIResult[T: BaseModel]:
    data: T
    sources: list[Source]
    web_used: bool


def extract_citations(response: object, limit: int = 12) -> list[Source]:
    """Responses API javobidagi `url_citation` izohlaridan manbalarni ajratadi."""
    seen: dict[str, Source] = {}
    for item in getattr(response, "output", None) or []:
        for content in getattr(item, "content", None) or []:
            for ann in getattr(content, "annotations", None) or []:
                if getattr(ann, "type", None) != "url_citation":
                    continue
                url = str(getattr(ann, "url", "") or "")
                if not url.startswith(("https://", "http://")) or url in seen:
                    continue
                title = str(getattr(ann, "title", "") or url)[:200]
                seen[url] = Source(url=url[:500], title=title)
                if len(seen) >= limit:
                    return list(seen.values())
    return list(seen.values())


def _error_code(exc: openai.APIStatusError) -> str | None:
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        code = body.get("code")
        if code is None and isinstance(body.get("error"), dict):
            code = body["error"].get("code")
        return code
    return getattr(exc, "code", None)
