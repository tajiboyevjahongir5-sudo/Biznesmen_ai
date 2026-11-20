"""Admin panel orqali o'zgartiriladigan sozlamalar (DB da saqlanadi).

Kanal, karta, narx kabi qiymatlarni Railway Variables o'rniga bot ichidan kiritish
mumkin. Qiymatlar `app_settings` jadvalida saqlanadi, ishga tushishda yuklanadi va
`.env` dagi qiymatlar ustidan yoziladi. Har bir qiymat `Settings` validatorlaridan o'tadi.

Kanallar ID si qo'lda yozilmaydi: admin bir martalik kod oladi va uni kanalga
joylaydi. Shunda begona odam botni o'z kanaliga qo'shib, uni "ulab" ololmaydi.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.database.models import AppSetting

# Admin o'zgartira oladigan kalitlar (boshqalari faqat .env orqali).
EDITABLE_KEYS: frozenset[str] = frozenset(
    {
        "ideas_channel_id",
        "ideas_channel_title",
        "payment_channel_id",
        "payment_card_number",
        "payment_card_holder",
        "channel_price_uzs",
        "payment_timeout_seconds",
        "daily_ideas_count",
        "daily_research_time",
        "ideas_auto_publish",
        "payment_amount_regex",
        "subscription_days",
        "research_provider",
        "followup_days",
        "bank_bot_username",
    }
)

LINK_CODE_TTL = 600  # 10 daqiqa
CHANNEL_KINDS = {"ideas": "ideas_channel_id", "payment": "payment_channel_id"}


class SettingError(ValueError):
    """Foydalanuvchiga ko'rsatiladigan xato."""


@dataclass
class _PendingLink:
    kind: str
    admin_id: int
    expires: float


class RuntimeSettings:
    def __init__(self, settings: Settings, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.settings = settings
        self._sf = session_factory
        self._links: dict[str, _PendingLink] = {}

    async def load(self) -> int:
        """DB dagi qiymatlarni `settings` ga qo'llaydi. Noto'g'ri qiymatlar o'tkazib yuboriladi."""
        async with self._sf() as session:
            rows = (await session.scalars(select(AppSetting))).all()
        applied = 0
        for row in rows:
            if row.key in EDITABLE_KEYS:
                try:
                    self._apply(row.key, row.value)
                    applied += 1
                except SettingError:
                    continue
        return applied

    def _apply(self, key: str, value: str) -> None:
        try:
            setattr(self.settings, key, value)
        except ValidationError as exc:
            msg = exc.errors()[0].get("msg", "noto'g'ri qiymat")
            raise SettingError(msg.removeprefix("Value error, ")) from exc

    async def set(self, key: str, value: str) -> None:
        """Qiymatni tekshiradi, qo'llaydi va DB ga saqlaydi."""
        if key not in EDITABLE_KEYS:
            raise SettingError("Bu sozlamani admin paneldan o'zgartirib bo'lmaydi")
        old = getattr(self.settings, key)
        self._apply(key, value)
        try:
            async with self._sf() as session:
                row = await session.get(AppSetting, key)
                if row is None:
                    session.add(AppSetting(key=key, value=value))
                else:
                    row.value = value
                await session.commit()
        except Exception:
            setattr(self.settings, key, old)
            raise

    # ----------------------------------------------------- kanalni kod bilan ulash

    def create_link_code(self, kind: str, admin_id: int) -> str:
        if kind not in CHANNEL_KINDS:
            raise SettingError("Noma'lum kanal turi")
        self._cleanup()
        code = "BMA-" + secrets.token_hex(4).upper()
        self._links[code] = _PendingLink(kind, admin_id, time.monotonic() + LINK_CODE_TTL)
        return code

    def consume_link_code(self, text: str) -> _PendingLink | None:
        """Kanal xabarida faol kod bo'lsa, uni (bir marta) qaytaradi."""
        self._cleanup()
        code = text.strip().upper()
        return self._links.pop(code, None)

    def _cleanup(self) -> None:
        now = time.monotonic()
        for code in [c for c, p in self._links.items() if p.expires < now]:
            del self._links[code]


def mask_card(number: str) -> str:
    if not number:
        return "kiritilmagan"
    return f"{number[:4]} **** **** {number[-4:]}"
