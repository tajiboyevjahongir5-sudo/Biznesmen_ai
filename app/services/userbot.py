"""Userbot: bank botidan (masalan, HUMO Card) kelgan bildirishnomalarni o'qib, to'lovni tasdiqlaydi.

Telegram botlari boshqa chatlarni o'qiy olmaydi, shuning uchun karta egasining akkaunti
nomidan ishlaydigan kichik mijoz (Telethon) ishlatiladi. Xavfsizlik:
- faqat `BANK_BOT_USERNAME` dagi BITTA botdan kelgan KIRUVCHI xabarlar o'qiladi;
- o'sha bot haqiqatan bot ekani va ID si tekshiriladi (soxta akkaunt ishlamaydi);
- boshqa chatlar, kontaktlar, xabarlar o'qilmaydi va saqlanmaydi;
- hech qanday xabar yuborilmaydi; sessiya kaliti faqat Railway Variables da turadi.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from aiogram import Bot

    from app.bot.context import AppContext

logger = logging.getLogger(__name__)


@dataclass
class UserbotStatus:
    enabled: bool = False
    connected: bool = False
    source: str = ""
    error: str = ""
    started_at: datetime | None = None
    last_message_at: datetime | None = None
    processed: int = 0
    matched: int = 0
    _client: object | None = field(default=None, repr=False)


STATUS = UserbotStatus()


async def start_userbot(bot: Bot, ctx: AppContext) -> asyncio.Task | None:
    """Userbotni ishga tushiradi. Sozlanmagan yoki xato bo'lsa — None (bot ishlashda davom etadi)."""
    s = ctx.settings
    STATUS.enabled = s.userbot_enabled
    STATUS.source = s.bank_bot_username
    if not s.userbot_enabled:
        return None
    try:
        from telethon import TelegramClient, events
        from telethon.sessions import StringSession
    except ImportError:
        STATUS.error = "telethon o'rnatilmagan"
        logger.error("userbot: telethon paketi yo'q")
        return None

    client = TelegramClient(
        StringSession(s.tg_session.get_secret_value()),
        s.tg_api_id,
        s.tg_api_hash.get_secret_value(),
        device_model="BIZNES MIND payments",
        receive_updates=True,
        catch_up=True,  # bot qayta ishga tushganda o'tkazib yuborilgan xabarlarni ham oladi
    )
    try:
        await client.connect()
        if not await client.is_user_authorized():
            STATUS.error = "sessiya yaroqsiz — TG_SESSION ni qayta yarating"
            logger.error("userbot: sessiya avtorizatsiyadan o'tmagan")
            await client.disconnect()
            return None
        source = await client.get_entity(s.bank_bot_username)
    except Exception as exc:  # noqa: BLE001 — userbot xatosi asosiy botni to'xtatmasin
        STATUS.error = f"ulanib bo'lmadi: {type(exc).__name__}"
        logger.error("userbot: ulanish xatosi %s", type(exc).__name__)
        await client.disconnect()
        return None
    if not getattr(source, "bot", False):
        STATUS.error = f"@{s.bank_bot_username} bot emas"
        logger.error("userbot: manba bot emas")
        await client.disconnect()
        return None
    source_id = source.id

    @client.on(events.NewMessage(chats=[source_id], incoming=True))
    async def on_bank_message(event) -> None:  # pragma: no cover - jonli Telegram kerak
        if event.sender_id != source_id:
            return
        await handle_bank_event(bot, ctx, source_id, event.id, event.raw_text or "")

    STATUS.connected = True
    STATUS.error = ""
    STATUS.started_at = datetime.now(UTC)
    STATUS._client = client
    logger.info("userbot: ulandi, manba=@%s", s.bank_bot_username)
    return asyncio.create_task(_run(client))


async def handle_bank_event(bot: Bot, ctx: AppContext, source_id: int, message_id: int, text: str) -> str:
    """Bank botidan kelgan bitta xabarni qayta ishlaydi (testlarda ham shu chaqiriladi)."""
    from app.bot.handlers.payment import process_bank_message

    STATUS.last_message_at = datetime.now(UTC)
    STATUS.processed += 1
    try:
        status = await process_bank_message(bot, ctx, source_id, message_id, text, "userbot")
    except Exception:  # noqa: BLE001
        logger.exception("userbot: xabarni qayta ishlashda xato")
        return "error"
    if status == "matched":
        STATUS.matched += 1
    return status


async def _run(client) -> None:  # pragma: no cover - jonli Telegram kerak
    try:
        await client.run_until_disconnected()
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        STATUS.error = f"uzildi: {type(exc).__name__}"
        logger.exception("userbot: uzildi")
    finally:
        STATUS.connected = False


async def stop_userbot() -> None:
    client = STATUS._client
    if client is not None:
        try:
            await client.disconnect()
        except Exception:  # noqa: BLE001
            logger.debug("userbot disconnect failed", exc_info=True)
    STATUS.connected = False
