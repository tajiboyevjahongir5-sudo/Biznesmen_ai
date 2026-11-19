"""OpenAI bilan ishlash uchun yagona, xavfsiz qatlam.

Vazifalari:
- timeout va retry (OpenAI SDK orqali);
- har bir so'rovda maksimal chiqish tokeni;
- foydalanuvchi va global kunlik limitlar, taxminiy xarajat byudjeti;
- structured output (Pydantic) va natijani tekshirish;
- barcha SDK xatolarini ichki, xavfsiz xato turlariga aylantirish.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from types import SimpleNamespace
from typing import TypeVar

import openai
from pydantic import BaseModel, ValidationError

from app.config import Settings
from app.core.logging import redact, user_ref

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
            key = self._settings.ai_api_key
            if not key or not self._settings.ai_model:
                raise AIConfigError
            # Gemini ham OpenAI-mos endpoint orqali shu SDK bilan ishlaydi.
            self._client = openai.AsyncOpenAI(
                api_key=key,
                base_url=self._settings.ai_base_url or None,
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
            if self._settings.ai_provider == "gemini":
                response = await self._call_gemini(client, instructions, user_input, schema, limit)
            else:
                response = await client.responses.parse(
                    model=self._settings.ai_model,
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

    async def diagnose(self) -> list[str]:
        """Admin uchun: kalit/model holatini tekshiradi. Kalit qiymati hech qachon chiqmaydi."""
        s = self._settings
        lines = [f"Provayder: {s.ai_provider}", f"Model: {s.ai_model or '(kiritilmagan)'}"]
        try:
            client = self._get_client()
        except AIConfigError:
            return [*lines, "Kalit yoki model nomi kiritilmagan."]

        # 1) Kalit ishlaydimi va qaysi modellar mavjud
        try:
            page = await client.models.list()
            ids = sorted({str(m.id).removeprefix("models/") for m in page.data})
            gem = [i for i in ids if "gemini" in i or s.ai_provider == "openai"]
            lines.append(f"Kalit: ishlaydi. Mavjud modellar soni: {len(ids)}")
            if s.ai_model.removeprefix("models/") in ids:
                lines.append("Model nomi: ro'yxatda bor.")
            else:
                lines.append("Model nomi: RO'YXATDA YO'Q. Quyidagilardan birini GEMINI_MODEL ga yozing:")
            flash = [i for i in gem if "flash" in i and "audio" not in i and "tts" not in i][:40] or gem[:40]
            lines += [f"  • {i}" for i in flash]
        except openai.OpenAIError as exc:
            lines.append(f"Kalit tekshiruvi xatosi: {self._describe(exc)}")

        # 2) Haqiqiy kichik so'rov
        try:
            await client.chat.completions.create(
                model=s.ai_model,
                messages=[{"role": "user", "content": "Faqat OK deb javob ber."}],
                max_tokens=20,
            )
            lines.append("Sinov so'rovi: MUVAFFAQIYATLI.")
        except openai.OpenAIError as exc:
            lines.append(f"Sinov so'rovi xatosi: {self._describe(exc)}")

        # 3) Bot ishlatadigan JSON rejimi (vaqti bilan)
        started = time.perf_counter()
        try:
            resp = await client.with_options(max_retries=0).chat.completions.create(
                model=s.ai_model,
                messages=[
                    {"role": "system", "content": 'Faqat JSON qaytar: {"ok": true}'},
                    {"role": "user", "content": "Sinov"},
                ],
                response_format={"type": "json_object"},
                max_tokens=s.openai_max_output_tokens,
            )
            secs = time.perf_counter() - started
            choice = resp.choices[0] if resp.choices else None
            content = (choice.message.content or "").strip() if choice else ""
            reason = getattr(choice, "finish_reason", None)
            lines.append(
                f"JSON rejimi: javob {secs:.1f} s da keldi, finish_reason={reason}, "
                f"matn={'bor' if content else 'BO`SH'}."
            )
        except openai.OpenAIError as exc:
            secs = time.perf_counter() - started
            lines.append(f"JSON rejimi xatosi ({secs:.1f} s): {self._describe(exc)}")
        return lines

    def _describe(self, exc: BaseException) -> str:
        """Xato turi, HTTP status va API xabarining qisqa, tozalangan matni."""
        status = getattr(exc, "status_code", None)
        message = str(getattr(exc, "message", "") or exc)
        message = redact(message, self._settings.secret_values())[:300]
        return f"{type(exc).__name__} status={status}: {message}"

    async def _call_gemini(
        self, client: openai.AsyncOpenAI, instructions: str, user_input: str, schema: type[T], limit: int
    ) -> SimpleNamespace:
        """Gemini (OpenAI-mos Chat Completions): JSON rejimi + Pydantic tekshiruvi.

        Natija `responses.parse` javobi bilan bir xil shaklga keltiriladi
        (`output_parsed`, `usage.input_tokens/output_tokens`).
        """
        schema_json = json.dumps(schema.model_json_schema(), ensure_ascii=False)
        system = (
            f"{instructions}\n\nJAVOB FORMATI: faqat quyidagi JSON Schema ga mos bitta JSON obyekt "
            f"qaytar. Markdown, izoh yoki ``` belgisi yozma. Matn chegaralariga (maxLength) rioya qil.\n"
            f"{schema_json}"
        )
        completion = await client.chat.completions.create(
            model=self._settings.ai_model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user_input},
            ],
            response_format={"type": "json_object"},
            max_tokens=limit,
        )
        choice = completion.choices[0] if completion.choices else None
        content = (choice.message.content or "") if choice else ""
        usage = getattr(completion, "usage", None)
        parsed = schema.model_validate_json(_strip_json_fence(content)) if content.strip() else None
        return SimpleNamespace(
            output_parsed=parsed,
            output=[],
            status=getattr(choice, "finish_reason", None),
            usage=SimpleNamespace(
                input_tokens=getattr(usage, "prompt_tokens", 0) or 0,
                output_tokens=getattr(usage, "completion_tokens", 0) or 0,
            ),
        )

    @staticmethod
    def _log_failure(agent: str, user_id: int, exc: BaseException) -> None:
        # Xato turi, status va API xabari (provayder matni) — foydalanuvchi matni loglanmaydi.
        # Kalitlar root logger filtri orqali baribir yashiriladi.
        status = getattr(exc, "status_code", None)
        detail = str(getattr(exc, "message", "") or "")[:200] if status else ""
        logger.warning(
            "ai_call_failed agent=%s user=%s error=%s status=%s detail=%s",
            agent,
            user_ref(user_id),
            type(exc).__name__,
            status,
            detail,
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


_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)


def _strip_json_fence(text: str) -> str:
    """Model ba'zan JSON ni ```json ... ``` ichida qaytaradi — uni olib tashlaymiz."""
    return _FENCE_RE.sub("", text.strip())


def _error_code(exc: openai.APIStatusError) -> str | None:
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        code = body.get("code")
        if code is None and isinstance(body.get("error"), dict):
            code = body["error"].get("code")
        return code
    return getattr(exc, "code", None)
