"""Global xato handleri: foydalanuvchiga xavfsiz xabar, texnik tafsilot faqat logda."""

from __future__ import annotations

import logging

from aiogram import Router
from aiogram.exceptions import TelegramAPIError
from aiogram.types import ErrorEvent

from app.services.ai_service import AIServiceError

logger = logging.getLogger(__name__)
router = Router(name="errors")

GENERIC_ERROR = "Kutilmagan xatolik yuz berdi. Iltimos, birozdan keyin qayta urinib ko'ring."


@router.errors()
async def on_error(event: ErrorEvent) -> bool:
    exc = event.exception
    if isinstance(exc, AIServiceError):
        text = exc.user_message
        logger.warning("ai_error_unhandled type=%s", type(exc).__name__)
    else:
        text = GENERIC_ERROR
        logger.error("unhandled_error type=%s", type(exc).__name__, exc_info=exc)

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
