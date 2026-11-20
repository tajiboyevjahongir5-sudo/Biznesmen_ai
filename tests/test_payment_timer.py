"""To'lov taymeri: har soniyada yangilanish, limitga moslashish, vaqt tugashi."""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from aiogram.exceptions import TelegramRetryAfter

from app.bot.handlers import payment as pay
from app.database import repository as repo
from app.database.models import PaymentStatus
from app.services.payment_service import PaymentService


def test_tick_interval_scales_with_load():
    assert pay.tick_interval(1) == 1.0
    assert pay.tick_interval(20) == 1.0
    assert pay.tick_interval(60) == 3.0  # 60 ta taymer → har biri 3 soniyada (20 tahrir/soniya)
    assert pay.tick_interval(99, slowdown=8) == pay.TIMER_MAX_TICK


def test_progress_bar_colors():
    assert pay.progress_bar(300, 300) == "🟩" * 10
    assert pay.progress_bar(120, 300) == "🟨" * 4 + "⬜" * 6
    assert pay.progress_bar(30, 300) == "🟥" + "⬜" * 9
    assert pay.progress_bar(0, 300) == "⬜" * 10


def _ctx(payments):
    settings = SimpleNamespace(
        payment_card_number="8600000000000007", payment_card_holder="", subscription_days=0
    )
    return SimpleNamespace(settings=settings, payments=payments)


async def _reserve(session_factory, timeout: int):
    payments = PaymentService(session_factory, 50_000, timeout)
    async with session_factory() as s:
        user = await repo.get_or_create_user(s, 901, None, None)
        await s.commit()
    r, _ = await payments.reserve(user.id, chat_id=901)
    return payments, r


async def test_timer_updates_every_second_and_expires(session_factory):
    payments, r = await _reserve(session_factory, timeout=3)
    bot = MagicMock()
    bot.edit_message_text = AsyncMock()
    await asyncio.wait_for(pay._timer_loop(bot, _ctx(payments), r.id, 901, 10), timeout=8)

    texts = [c.args[0] for c in bot.edit_message_text.call_args_list]
    countdown = [t for t in texts if "Qolgan vaqt" in t]
    assert len(countdown) >= 2  # 3 soniyalik to'lovda har soniyada yangilandi
    assert "0:02" in countdown[0] or "0:01" in countdown[0]
    assert "tugadi" in texts[-1]
    assert (await payments.get(r.id)).status == PaymentStatus.EXPIRED


async def test_timer_backs_off_on_retry_after(session_factory, monkeypatch):
    payments, r = await _reserve(session_factory, timeout=2)
    sleeps: list[float] = []
    real_sleep = asyncio.sleep

    async def fake_sleep(sec):
        sleeps.append(sec)
        await real_sleep(min(sec, 0.01))

    monkeypatch.setattr(pay.asyncio, "sleep", fake_sleep)
    bot = MagicMock()
    flood = TelegramRetryAfter(method=MagicMock(), message="flood", retry_after=7)
    bot.edit_message_text = AsyncMock(side_effect=[flood] + [None] * 50)
    # Vaqt tugashini tezlashtiramiz: rezervatsiyani darhol tugaydigan qilib qo'yamiz
    task = asyncio.create_task(pay._timer_loop(bot, _ctx(payments), r.id, 901, 10))
    await real_sleep(0.1)
    async with session_factory() as s:
        row = await s.get(type(r), r.id)
        row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await s.commit()
    task.cancel()
    assert 7.0 in sleeps  # Telegram so'ragan vaqt kutildi


async def test_timer_stops_when_paid(session_factory):
    payments, r = await _reserve(session_factory, timeout=60)
    bot = MagicMock()
    bot.edit_message_text = AsyncMock()
    task = asyncio.create_task(pay._timer_loop(bot, _ctx(payments), r.id, 901, 10))
    await asyncio.sleep(0.05)
    await payments.admin_confirm(r.id, admin_id=1)
    pay.STATUS_CHECK_SECONDS, old = 0.0, pay.STATUS_CHECK_SECONDS
    try:
        await asyncio.wait_for(task, timeout=4)
    finally:
        pay.STATUS_CHECK_SECONDS = old
    assert task.done()
