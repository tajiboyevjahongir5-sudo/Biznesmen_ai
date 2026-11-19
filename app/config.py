"""Ilova konfiguratsiyasi.

Barcha sozlamalar `.env` fayli yoki muhit o'zgaruvchilaridan o'qiladi.
Maxfiy qiymatlar `SecretStr` turida saqlanadi: ular `repr()`, `str()` va
loglarda `**********` ko'rinishida chiqadi.
"""

from __future__ import annotations

import re
from functools import lru_cache

from pydantic import AliasChoices, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PLACEHOLDER_MARKERS = ("change_me", "your_", "xxxx", "placeholder")


def _parse_ids(raw: str) -> frozenset[int]:
    ids: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if re.fullmatch(r"-?\d+", part):
            ids.add(int(part))
    return frozenset(ids)


class Settings(BaseSettings):
    """Ilova sozlamalari (`.env` orqali boshqariladi)."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        populate_by_name=True,
        # Admin paneldan o'zgartirilgan qiymatlar ham validatorlardan o'tadi.
        validate_assignment=True,
    )

    # --- Maxfiy kalitlar (faqat .env ichida) ---
    bot_token: SecretStr = Field(default=SecretStr(""), alias="BOT_TOKEN")
    gemini_api_key: SecretStr = Field(default=SecretStr(""), alias="GEMINI_API_KEY")
    openai_api_key: SecretStr = Field(default=SecretStr(""), alias="OPENAI_API_KEY")

    # --- AI provayder: "gemini" (standart) yoki "openai" ---
    ai_provider: str = Field(default="gemini", alias="AI_PROVIDER")

    # --- Gemini (Google AI Studio, OpenAI-mos endpoint orqali) ---
    gemini_model: str = Field(default="", alias="GEMINI_MODEL")
    gemini_base_url: str = Field(
        default="https://generativelanguage.googleapis.com/v1beta/openai/", alias="GEMINI_BASE_URL"
    )

    # --- OpenAI (ixtiyoriy) ---
    openai_model: str = Field(default="", alias="OPENAI_MODEL")
    openai_base_url: str | None = Field(default=None, alias="OPENAI_BASE_URL")

    # --- Umumiy AI cheklovlari (ikkala provayder uchun) ---
    openai_timeout_seconds: float = Field(
        default=90.0,
        ge=5,
        le=600,
        validation_alias=AliasChoices("AI_TIMEOUT_SECONDS", "OPENAI_TIMEOUT_SECONDS"),
    )
    openai_max_retries: int = Field(
        default=2, ge=0, le=5, validation_alias=AliasChoices("AI_MAX_RETRIES", "OPENAI_MAX_RETRIES")
    )
    openai_max_output_tokens: int = Field(
        default=8000,
        ge=200,
        le=65000,
        validation_alias=AliasChoices("AI_MAX_OUTPUT_TOKENS", "OPENAI_MAX_OUTPUT_TOKENS"),
    )
    # Xarajatni taxminiy hisoblash uchun (USD / 1M token). 0 bo'lsa hisoblanmaydi.
    openai_input_price_per_1m: float = Field(
        default=0.0, ge=0, validation_alias=AliasChoices("AI_INPUT_PRICE_PER_1M", "OPENAI_INPUT_PRICE_PER_1M")
    )
    openai_output_price_per_1m: float = Field(
        default=0.0,
        ge=0,
        validation_alias=AliasChoices("AI_OUTPUT_PRICE_PER_1M", "OPENAI_OUTPUT_PRICE_PER_1M"),
    )
    # Internet qidiruvi: "none" yoki "openai_web" (faqat AI_PROVIDER=openai bilan, alohida pullik).
    research_provider: str = Field(default="none", alias="RESEARCH_PROVIDER")

    # --- Xarajat va suiiste'mol nazorati ---
    ai_daily_requests_per_user: int = Field(default=40, ge=1, le=10000, alias="AI_DAILY_REQUESTS_PER_USER")
    ai_daily_requests_global: int = Field(default=800, ge=1, le=1_000_000, alias="AI_DAILY_REQUESTS_GLOBAL")
    ai_daily_budget_usd: float = Field(default=0.0, ge=0, alias="AI_DAILY_BUDGET_USD")
    rate_limit_messages: int = Field(default=10, ge=1, le=1000, alias="RATE_LIMIT_MESSAGES")
    rate_limit_window_seconds: int = Field(default=60, ge=1, le=3600, alias="RATE_LIMIT_WINDOW_SECONDS")
    max_input_chars: int = Field(default=3000, ge=100, le=20000, alias="MAX_INPUT_CHARS")
    min_idea_chars: int = Field(default=10, ge=1, le=500, alias="MIN_IDEA_CHARS")
    min_interview_questions: int = Field(default=6, ge=0, le=30, alias="MIN_INTERVIEW_QUESTIONS")
    max_interview_questions: int = Field(default=12, ge=1, le=40, alias="MAX_INTERVIEW_QUESTIONS")

    # --- Database ---
    database_url: str = Field(default="sqlite+aiosqlite:///./data/biznes_mind.db", alias="DATABASE_URL")

    # --- Adminlar ---
    admin_ids_raw: str = Field(default="", alias="ADMIN_IDS")

    # --- Pullik g'oyalar kanali ---
    ideas_channel_id: int | None = Field(default=None, alias="IDEAS_CHANNEL_ID")
    ideas_channel_title: str = Field(default="BIZNES MIND g'oyalar kanali", alias="IDEAS_CHANNEL_TITLE")
    daily_ideas_count: int = Field(default=2, ge=0, le=10, alias="DAILY_IDEAS_COUNT")
    daily_research_time: str = Field(default="09:00", alias="DAILY_RESEARCH_TIME")
    timezone: str = Field(default="Asia/Tashkent", alias="TIMEZONE")
    ideas_auto_publish: bool = Field(default=False, alias="IDEAS_AUTO_PUBLISH")

    # --- To'lov ---
    payment_channel_id: int | None = Field(default=None, alias="PAYMENT_CHANNEL_ID")
    channel_price_uzs: int = Field(default=50_000, ge=1000, le=100_000_000, alias="CHANNEL_PRICE_UZS")
    payment_card_number: str = Field(default="", alias="PAYMENT_CARD_NUMBER")
    payment_card_holder: str = Field(default="", alias="PAYMENT_CARD_HOLDER")
    payment_timeout_seconds: int = Field(default=300, ge=60, le=3600, alias="PAYMENT_TIMEOUT_SECONDS")
    invite_link_ttl_hours: int = Field(default=24, ge=1, le=720, alias="INVITE_LINK_TTL_HOURS")
    # Ixtiyoriy: bank xabaridan summani ajratib oluvchi o'z regex ifodangiz (bitta guruh).
    payment_amount_regex: str = Field(default="", alias="PAYMENT_AMOUNT_REGEX")

    # --- Umumiy ---
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    log_file: str | None = Field(default="logs/bot.log", alias="LOG_FILE")
    environment: str = Field(default="development", alias="ENVIRONMENT")

    @field_validator("log_level")
    @classmethod
    def _validate_log_level(cls, value: str) -> str:
        value = value.upper().strip()
        if value not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError("LOG_LEVEL noto'g'ri")
        return value

    @field_validator("research_provider")
    @classmethod
    def _validate_provider(cls, value: str) -> str:
        value = value.strip().lower()
        if value not in {"none", "openai_web"}:
            raise ValueError("RESEARCH_PROVIDER faqat 'none' yoki 'openai_web' bo'lishi mumkin")
        return value

    @field_validator("ai_provider")
    @classmethod
    def _validate_ai_provider(cls, value: str) -> str:
        value = value.strip().lower()
        if value not in {"gemini", "openai"}:
            raise ValueError("AI_PROVIDER faqat 'gemini' yoki 'openai' bo'lishi mumkin")
        return value

    @field_validator("daily_research_time")
    @classmethod
    def _validate_time(cls, value: str) -> str:
        if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", value.strip()):
            raise ValueError("DAILY_RESEARCH_TIME HH:MM formatida bo'lishi kerak")
        return value.strip()

    @field_validator("payment_card_number")
    @classmethod
    def _validate_card(cls, value: str) -> str:
        digits = re.sub(r"\D", "", value)
        if value and not 16 <= len(digits) <= 19:
            raise ValueError("PAYMENT_CARD_NUMBER 16-19 raqamdan iborat bo'lishi kerak")
        return digits

    @field_validator("openai_model", "gemini_model", "payment_amount_regex")
    @classmethod
    def _strip(cls, value: str) -> str:
        return value.strip()

    @field_validator("database_url")
    @classmethod
    def _normalize_db_url(cls, value: str) -> str:
        """Railway/Heroku `postgresql://` URL ini async drayverga moslaydi."""
        # Variables ga qo'shtirnoq bilan yozilgan bo'lsa, ularni olib tashlaymiz.
        value = value.strip().strip("\"'").strip()
        for prefix in ("postgres://", "postgresql://"):
            if value.startswith(prefix):
                return "postgresql+asyncpg://" + value[len(prefix) :]
        return value

    @field_validator("ideas_channel_id", "payment_channel_id", "openai_base_url", mode="before")
    @classmethod
    def _empty_to_none(cls, value: object) -> object:
        return None if value == "" else value

    @property
    def admin_ids(self) -> frozenset[int]:
        return _parse_ids(self.admin_ids_raw)

    @property
    def web_search_enabled(self) -> bool:
        # OpenAI web_search vositasi faqat OpenAI provayderida mavjud.
        return self.research_provider == "openai_web" and self.ai_provider == "openai"

    @property
    def ai_api_key(self) -> str:
        key = self.gemini_api_key if self.ai_provider == "gemini" else self.openai_api_key
        return key.get_secret_value()

    @property
    def ai_model(self) -> str:
        return self.gemini_model if self.ai_provider == "gemini" else self.openai_model

    @property
    def ai_base_url(self) -> str | None:
        return self.gemini_base_url if self.ai_provider == "gemini" else self.openai_base_url

    @property
    def payments_configured(self) -> bool:
        return bool(self.payment_card_number and self.payment_channel_id and self.ideas_channel_id)

    def secret_values(self) -> list[str]:
        """Loglardan yashirilishi kerak bo'lgan qiymatlar ro'yxati."""
        values = [
            self.bot_token.get_secret_value(),
            self.openai_api_key.get_secret_value(),
            self.gemini_api_key.get_secret_value(),
        ]
        return [v for v in values if v and len(v) >= 8]

    def missing_required(self) -> list[str]:
        """Bot ishga tushishi uchun yetishmayotgan sozlamalar nomlari."""
        missing: list[str] = []
        if _is_placeholder(self.bot_token.get_secret_value()):
            missing.append("BOT_TOKEN")
        prefix = "GEMINI" if self.ai_provider == "gemini" else "OPENAI"
        if _is_placeholder(self.ai_api_key):
            missing.append(f"{prefix}_API_KEY")
        if not self.ai_model:
            missing.append(f"{prefix}_MODEL")
        if not _valid_db_url(self.database_url):
            missing.append("DATABASE_URL (bo'sh yoki noto'g'ri formatda)")
        return missing


def _valid_db_url(url: str) -> bool:
    """URL ni SQLAlchemy o'qiy oladimi (parol loglarga chiqmasligi uchun qiymat ko'rsatilmaydi)."""
    from sqlalchemy.engine import make_url
    from sqlalchemy.exc import ArgumentError

    if not url or "${{" in url:
        return False
    try:
        parsed = make_url(url)
    except (ArgumentError, ValueError):
        return False
    return parsed.drivername in {"sqlite+aiosqlite", "postgresql+asyncpg"}


def _is_placeholder(value: str) -> bool:
    if not value or len(value) < 10:
        return True
    lowered = value.lower()
    return any(marker in lowered for marker in PLACEHOLDER_MARKERS)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Sozlamalarni bir marta yuklab, keshlab qaytaradi."""
    return Settings()
