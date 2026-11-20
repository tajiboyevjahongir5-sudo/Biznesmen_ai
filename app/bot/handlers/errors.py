"""Global xato handleri: foydalanuvchiga xavfsiz xabar, texnik tafsilot faqat logda."""

from __future__ import annotations

import logging

from aiogram import Bot, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.types import ErrorEvent

from app.bot.context import AppContext
from app.bot.helpers import alert_admins
from app.core.text import e
from app.services.ai_service import AIServiceError

logger = logging.getLogger(__name__)
router = Router(name="errors")

GENERIC_ERROR = "😔 Kutilmagan xatolik yuz berdi. Iltimos, birozdan keyin qayta urinib ko'ring."


@router.errors()
async def on_error(event: ErrorEvent, bot: Bot | None = None, ctx: AppContext | None = None) -> bool:
    exc = event.exception
    if isinstance(exc, AIServiceError):
        text = exc.user_message
        logger.warning("ai_error_unhandled type=%s", type(exc).__name__)
    else:
        text = GENERIC_ERROR
        logger.error("unhandled_error type=%s", type(exc).__name__, exc_info=exc)
        if bot is not None and ctx is not None:
            await _alert(bot, ctx, exc)

    update = event.update
    target = update.message or (update.callback_query.message if update.callback_query else None)
    if target is not None and target.chat.type == "private":
        try:
            if update.callback_query:
                await update.callback_query.answer()
            await target.answer(text)
        except TelegramAPIError:
            pass
    return True


async def _alert(bot: Bot, ctx: AppContext, exc: BaseException) -> None:
    """Adminga qisqa xabar: faqat xato turi va kod joyi (foydalanuvchi ma'lumoti yo'q)."""
    tb = exc.__traceback__
    while tb and tb.tb_next:
        tb = tb.tb_next
    where = ""
    if tb:
        where = (
            f"{tb.tb_frame.f_code.co_filename.replace(chr(92), '/').rsplit('/app/', 1)[-1]}:{tb.tb_lineno}"
        )
    name = type(exc).__name__
    try:
        await alert_admins(
            bot,
            ctx,
            f"err:{name}:{where}",
            f"🚨 Botda xatolik: <code>{e(name)}</code> ({e(where)}). Batafsil — Railway loglarida.",
        )
    except Exception:  # noqa: BLE001 — ogohlantirish xatosi asosiy xatoni yashirmasin
        logger.warning("error_alert_failed")
