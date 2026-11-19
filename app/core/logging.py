"""Xavfsiz logging.

- Tokenlar va API kalitlari har qanday log yozuvidan avtomatik yashiriladi.
- Foydalanuvchi matnlari loglanmaydi: handlerlar faqat uzunlik va
  anonimlashtirilgan foydalanuvchi identifikatorini yozadi.
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Iterable
from logging.handlers import RotatingFileHandler
from pathlib import Path

REDACTED = "[YASHIRILDI]"

# Telegram bot tokeni: 123456789:AA... (35 belgi)
_TELEGRAM_TOKEN_RE = re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{30,}\b")
# OpenAI kalitlari: sk-..., sk-proj-...
_OPENAI_KEY_RE = re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b")
# Authorization: Bearer xxx
_BEARER_RE = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{12,}")

_PATTERNS: tuple[re.Pattern[str], ...] = (_TELEGRAM_TOKEN_RE, _OPENAI_KEY_RE)


def redact(text: str, secrets: Iterable[str] = ()) -> str:
    """Matndan ma'lum maxfiy qiymatlar va token ko'rinishidagi satrlarni olib tashlaydi."""
    for secret in secrets:
        if secret:
            text = text.replace(secret, REDACTED)
    for pattern in _PATTERNS:
        text = pattern.sub(REDACTED, text)
    text = _BEARER_RE.sub(lambda m: m.group(1) + REDACTED, text)
    return text


class SecretRedactingFilter(logging.Filter):
    """Har bir log yozuvini (xabar, argumentlar, traceback) tozalaydigan filter."""

    def __init__(self, secrets: Iterable[str] = ()) -> None:
        super().__init__()
        self._secrets = tuple(s for s in secrets if s)

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - noto'g'ri formatlangan log ham yozilsin
            message = str(record.msg)
        record.msg = redact(message, self._secrets)
        record.args = None
        if record.exc_info:
            # Tracebackni oldindan matnga aylantirib, uni ham tozalaymiz.
            formatter = logging.Formatter()
            record.exc_text = redact(formatter.formatException(record.exc_info), self._secrets)
            record.exc_info = None
        elif record.exc_text:
            record.exc_text = redact(record.exc_text, self._secrets)
        return True


def user_ref(telegram_id: int | None) -> str:
    """Loglar uchun Telegram ID ning qisqa anonim xeshi."""
    if telegram_id is None:
        return "u:unknown"
    digest = hashlib.sha256(str(telegram_id).encode()).hexdigest()[:10]
    return f"u:{digest}"


def setup_logging(level: str, secrets: Iterable[str], log_file: str | None = None) -> None:
    """Root loggerni xavfsiz filter bilan sozlaydi."""
    secret_filter = SecretRedactingFilter(secrets)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")

    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if log_file:
        path = Path(log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(RotatingFileHandler(path, maxBytes=2_000_000, backupCount=3, encoding="utf-8"))

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    for handler in handlers:
        handler.setFormatter(fmt)
        handler.addFilter(secret_filter)
        root.addHandler(handler)
    root.setLevel(level)

    # Tashqi kutubxonalar so'rov URL/headerlarini DEBUG darajasida yozishi mumkin.
    for noisy in ("httpx", "httpcore", "openai", "aiogram.event"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
