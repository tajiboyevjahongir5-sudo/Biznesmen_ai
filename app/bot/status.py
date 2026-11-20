"""Jonli holat xabarlari: «O'ylayapman...», «Tinglayapman...» va tahlil bosqichlari.

Telegram'dagi "typing" yozuvini bot o'zgartira olmaydi (uni ilovaning o'zi foydalanuvchi
tilida chiqaradi). Shuning uchun bot o'z holat xabarini yuboradi va javob tayyor bo'lgach
uni javobga aylantiradi yoki o'chiradi. Holat xabari — bezak: Telegram xatolari jim
o'tadi va asosiy javobga ta'sir qilmaydi.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter

from app.bot.helpers import NO_PREVIEW, send_parts
from app.core.text import SEP

logger = logging.getLogger(__name__)

THINKING_TEXT = "💭 <i>O'ylayapman...</i>"
LISTENING_TEXT = "🎙 <i>Tinglayapman...</i>"

ANALYSIS_STAGES: tuple[tuple[str, str], ...] = (
    ("market", "Bozor, talab va raqobat"),
    ("finance", "Moliyaviy model"),
    ("prompt", "SUPER PROMPT"),
    ("report", "Hisobot"),
)
TICK_SECONDS = 5.0  # tahlil xabaridagi vaqt shu oraliqda yangilanadi
MAX_TICK_SECONDS = 30.0


async def send_status(bot: Bot, chat_id: int, text: str) -> int | None:
    try:
        msg = await bot.send_message(chat_id, text)
    except TelegramAPIError:
        return None
    return msg.message_id


async def drop_status(bot: Bot, chat_id: int, message_id: int | None) -> None:
    if message_id is None:
        return
    with contextlib.suppress(TelegramAPIError):
        await bot.delete_message(chat_id, message_id)


async def replace_status(bot: Bot, chat_id: int, message_id: int | None, parts: list[str]) -> None:
    """Holat xabarini javobning birinchi qismiga aylantiradi, qolganlarini alohida yuboradi."""
    if not parts:
        await drop_status(bot, chat_id, message_id)
        return
    if message_id is not None:
        try:
            await bot.edit_message_text(
                parts[0], chat_id=chat_id, message_id=message_id, link_preview_options=NO_PREVIEW
            )
            parts = parts[1:]
        except TelegramAPIError:
            await drop_status(bot, chat_id, message_id)  # tahrir bo'lmasa — oddiy xabar qilib yuboramiz
    if parts:
        await send_parts(bot, chat_id, parts)


class AnalysisProgress:
    """Tahlil paytida bitta xabar: bosqichlar (✅ / ⏳ / ▫️) va o'tgan vaqt."""

    def __init__(self, bot: Bot, chat_id: int, *, web: bool = False, tick: float = TICK_SECONDS) -> None:
        self.bot = bot
        self.chat_id = chat_id
        self.tick = tick
        self.labels = [label for _, label in ANALYSIS_STAGES]
        if web:
            self.labels[0] += " 🌐"
        self.keys = [key for key, _ in ANALYSIS_STAGES]
        self.current = 0
        self.message_id: int | None = None
        self._started = time.monotonic()
        self._ticks = 0
        self._last = ""
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None

    def _clock(self) -> str:
        mm, ss = divmod(int(time.monotonic() - self._started), 60)
        return f"{mm}:{ss:02d}"

    def render(self, done: bool = False) -> str:
        head = f"✅ <b>Tahlil tayyor!</b> · ⏱ {self._clock()}" if done else "🧠 <b>Tahlil qilinyapti...</b>"
        lines = [head, SEP]
        running = "⌛" if self._ticks % 2 else "⏳"
        for i, label in enumerate(self.labels):
            icon = "✅" if done or i < self.current else (running if i == self.current else "▫️")
            lines.append(f"{icon} {label}")
        if not done:
            lines += ["", f"⏱ {self._clock()} · <i>odatda 1–3 daqiqa</i>"]
        return "\n".join(lines)

    async def start(self) -> None:
        self._last = self.render()
        self.message_id = await send_status(self.bot, self.chat_id, self._last)
        if self.message_id is not None:
            self._task = asyncio.create_task(self._ticker())

    async def stage(self, key: str) -> None:
        """Orkestrator chaqiradi: `key` bosqichi boshlandi (oldingilari tugadi)."""
        if key in self.keys and self.keys.index(key) > self.current:
            self.current = self.keys.index(key)
            await self._edit()

    async def finish(self, ok: bool) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        if ok:
            self.current = len(self.keys)
            await self._edit(done=True)
        else:
            await drop_status(self.bot, self.chat_id, self.message_id)  # xato haqida chaqiruvchi yozadi

    async def _ticker(self) -> None:
        while True:
            await asyncio.sleep(self.tick)
            self._ticks += 1
            await self._edit()

    async def _edit(self, done: bool = False) -> None:
        if self.message_id is None:
            return
        async with self._lock:
            text = self.render(done)
            if text == self._last:
                return
            try:
                await self.bot.edit_message_text(text, chat_id=self.chat_id, message_id=self.message_id)
                self._last = text
            except TelegramRetryAfter:
                self.tick = min(self.tick * 2, MAX_TICK_SECONDS)  # Telegram sekinlashtirishni so'radi
            except TelegramAPIError:
                logger.debug("progress_edit_failed", exc_info=True)
