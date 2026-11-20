"""Middlewarelar: foydalanuvchini aniqlash, bloklash va rate limiting."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

from app.bot.context import AppContext
from app.core.logging import user_ref
from app.database import repository as repo

logger = logging.getLogger(__name__)


class UserContextMiddleware(BaseMiddleware):
    """Faqat shaxsiy chatlar. Foydalanuvchini DB da topadi/yaratadi va `db_user_id` beradi."""

    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if isinstance(event, Message):
            chat, tg_user = event.chat, event.from_user
        elif isinstance(event, CallbackQuery):
            chat = event.message.chat if event.message else None
            tg_user = event.from_user
        else:
            return await handler(event, data)

        if tg_user is None or tg_user.is_bot or chat is None or chat.type != "private":
            return None  # guruh/kanal xabarlari bu routerda qayta ishlanmaydi

        async with self.ctx.session_factory() as session:
            user = await repo.get_or_create_user(
                session,
                tg_user.id,
                (tg_user.username or "")[:64] or None,
                (tg_user.first_name or "")[:128] or None,
            )
            await repo.touch_user(session, user, datetime.now(UTC))
            await session.commit()
            blocked, db_id = user.is_blocked, user.id
        if blocked and tg_user.id not in self.ctx.settings.admin_ids:
            logger.info("blocked_user_ignored user=%s", user_ref(tg_user.id))
            return None
        data["db_user_id"] = db_id
        data["is_admin"] = tg_user.id in self.ctx.settings.admin_ids
        return await handler(event, data)


class ThrottlingMiddleware(BaseMiddleware):
    """Spam va haddan tashqari so'rovlardan himoya."""

    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx
        self._warned: set[int] = set()

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = getattr(event, "from_user", None)
        if user is None or user.id in self.ctx.settings.admin_ids:
            return await handler(event, data)
        limiter = self.ctx.rate_limiter
        if limiter.hit(user.id):
            self._warned.discard(user.id)
            return await handler(event, data)

        logger.info("rate_limited user=%s", user_ref(user.id))
        if user.id not in self._warned:  # har safar emas, bir marta ogohlantiramiz
            self._warned.add(user.id)
            wait = limiter.retry_after(user.id)
            text = f"Juda ko'p xabar yuborildi. Iltimos, {wait} soniyadan keyin davom eting."
            if isinstance(event, Message):
                await event.answer(text)
            elif isinstance(event, CallbackQuery):
                await event.answer(text, show_alert=True)
        elif isinstance(event, CallbackQuery):
            await event.answer()
        return None
