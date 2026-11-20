"""Fon vazifalari: kunlik izlanish va muddati o'tgan to'lovlarni tozalash."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import Bot

from app.bot.context import AppContext

logger = logging.getLogger(__name__)


def seconds_until(hhmm: str, tz: str, now: datetime | None = None) -> float:
    """Keyingi `HH:MM` (berilgan vaqt zonasida) gacha qolgan soniyalar."""
    zone = ZoneInfo(tz)
    now = now.astimezone(zone) if now else datetime.now(zone)
    hour, minute = map(int, hhmm.split(":"))
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


async def daily_research_loop(bot: Bot, ctx: AppContext) -> None:
    from app.bot.handlers.admin import send_idea_preview  # aylanma importdan qochish

    s = ctx.settings
    while True:
        await asyncio.sleep(seconds_until(s.daily_research_time, s.timezone))
        try:
            created = await ctx.channel.research_and_create_drafts()
            for idea in created:
                if s.ideas_auto_publish and s.ideas_channel_id:
                    await ctx.channel.publish(bot, idea.id, ctx.bot_username)
                    await asyncio.sleep(3)
                else:
                    for admin_id in s.admin_ids:
                        await send_idea_preview(bot, admin_id, idea)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - kunlik vazifa xatosi botni to'xtatmasin
            logger.exception("daily_research_failed")
        await asyncio.sleep(60)  # bir daqiqa ichida qayta ishga tushmasligi uchun


async def payment_sweeper_loop(ctx: AppContext, interval: float = 20.0) -> None:
    while True:
        try:
            expired = await ctx.payments.sweep_expired()
            if expired:
                logger.info("payments_expired count=%d", len(expired))
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("payment_sweeper_failed")
        await asyncio.sleep(interval)


async def subscription_loop(bot: Bot, ctx: AppContext, interval: float = 1800.0) -> None:
    """Obuna muddati: 3 kun qolganda eslatma, tugaganda kanaldan chiqarish."""
    from aiogram.exceptions import TelegramAPIError

    from app.bot.helpers import promo_kb
    from app.database.models import SubscriptionStatus

    while True:
        try:
            for sub in await ctx.subscriptions.due_reminders():
                try:
                    await bot.send_message(
                        sub.telegram_id,
                        "⏰ Kanal obunangiz tugashiga oz qoldi "
                        f"(<b>{sub.expires_at:%d.%m.%Y}</b>). Uzaytirish uchun pastdagi tugmani bosing.",
                        reply_markup=promo_kb(),
                    )
                except TelegramAPIError:
                    logger.info("subscription_reminder_failed sub=%d", sub.id)
                await ctx.subscriptions.mark(sub.id, reminded=True)
            for sub in await ctx.subscriptions.due_expired():
                channel = ctx.settings.ideas_channel_id
                if channel and sub.telegram_id not in ctx.settings.admin_ids:
                    try:
                        # ban + unban = kanaldan chiqarish (keyin to'lab qayta kira oladi)
                        await bot.ban_chat_member(channel, sub.telegram_id)
                        await bot.unban_chat_member(channel, sub.telegram_id, only_if_banned=True)
                    except TelegramAPIError:
                        logger.warning("subscription_kick_failed sub=%d", sub.id)
                await ctx.subscriptions.mark(sub.id, status=SubscriptionStatus.EXPIRED)
                try:
                    await bot.send_message(
                        sub.telegram_id,
                        "📭 Kanal obunangiz muddati tugadi. Qayta qo'shilish uchun pastdagi tugmani bosing.",
                        reply_markup=promo_kb(),
                    )
                except TelegramAPIError:
                    pass
                logger.info("subscription_expired sub=%d", sub.id)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("subscription_loop_failed")
        await asyncio.sleep(interval)
