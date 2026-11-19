"""Handlerlar uchun umumiy yordamchilar."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions

from app.bot.context import AppContext
from app.core.logging import user_ref
from app.core.text import e, format_money
from app.database import repository as repo

logger = logging.getLogger(__name__)

CB_PAY_START = "pay:start"
CB_PAY_CANCEL = "pay:cancel:"
MEMBER_STATUSES = {"member", "administrator", "creator", "restricted"}
PROMO_COOLDOWN = timedelta(hours=24)
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)


async def send_parts(bot: Bot, chat_id: int, parts: list[str], **kwargs) -> None:
    """Xabarlarni ketma-ket yuboradi; Telegram flood-limitida kutib qayta urinadi."""
    for i, part in enumerate(parts):
        extra = kwargs if i == len(parts) - 1 else {}
        for _ in range(3):
            try:
                await bot.send_message(chat_id, part, link_preview_options=NO_PREVIEW, **extra)
                break
            except TelegramRetryAfter as exc:
                await asyncio.sleep(min(exc.retry_after, 30))
        await asyncio.sleep(0.3)


async def is_channel_member(bot: Bot, ctx: AppContext, telegram_id: int) -> bool:
    channel = ctx.settings.ideas_channel_id
    if not channel:
        return False
    try:
        member = await bot.get_chat_member(channel, telegram_id)
    except TelegramAPIError:
        logger.warning("membership_check_failed user=%s", user_ref(telegram_id))
        return False
    return member.status in MEMBER_STATUSES


def promo_text(ctx: AppContext) -> str:
    s = ctx.settings
    return (
        f"<b>{e(s.ideas_channel_title)}</b>\n\n"
        "Har kuni bozorni o'rganib, <b>talabi yuqori va raqobati past</b> biznes g'oyalarni "
        "joylab boraman. Har bir g'oya ostidagi tugma orqali o'sha g'oya uchun to'liq "
        "SUPER PROMPT olasiz: bozor, xavflar, moliyaviy reja va harakat rejasi bilan.\n\n"
        f"Kanalga kirish narxi: <b>{format_money(s.channel_price_uzs)} so'm</b> (bir martalik).\n\n"
        "<i>Eslatma: g'oyalar tahlil va maslahat xarakterida, daromad kafolatlanmaydi.</i>"
    )


def promo_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="To'lov qilish", callback_data=CB_PAY_START)]]
    )


async def maybe_send_promo(
    bot: Bot, ctx: AppContext, telegram_id: int, chat_id: int, force: bool = False
) -> bool:
    """Kanal reklamasini yuboradi (sozlangan bo'lsa, a'zo bo'lmasa va 24 soatda 1 marta)."""
    if not ctx.settings.payments_configured:
        return False
    if await is_channel_member(bot, ctx, telegram_id):
        return False
    now = datetime.now(UTC)
    async with ctx.session_factory() as session:
        user = await repo.get_user_by_tg(session, telegram_id)
        if user is None:
            return False
        last = user.promo_sent_at
        if last and last.tzinfo is None:
            last = last.replace(tzinfo=UTC)
        if not force and last and now - last < PROMO_COOLDOWN:
            return False
        user.promo_sent_at = now
        await session.commit()
    await bot.send_message(chat_id, promo_text(ctx), reply_markup=promo_kb())
    return True
