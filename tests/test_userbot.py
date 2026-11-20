"""Userbot sozlamalari va bank xabarini qayta ishlash (jonli Telegramsiz)."""

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.bot.handlers import payment as payment_handlers
from app.config import Settings
from app.core.logging import SecretRedactingFilter
from app.services import userbot
from app.services.payment_service import PaymentService

SESSION = "1BVtsOKABu0FakeSessionString_abcdefghijklmnopqrstuvwxyz0123456789"
HASH = "0123456789abcdef0123456789abcdef"


def _settings(**kw) -> Settings:
    data = dict(
        _env_file=None,
        TG_API_ID="12345",
        TG_API_HASH=HASH,
        TG_SESSION=SESSION,
        BANK_BOT_USERNAME="@HUMOcardbot",
        PAYMENT_CARD_NUMBER="8600000000000007",
        IDEAS_CHANNEL_ID="-1001",
    )
    data.update(kw)
    return Settings(**data)


def test_userbot_settings_and_payments_without_channel():
    s = _settings()
    assert s.userbot_enabled and s.bank_bot_username == "HUMOcardbot"
    assert s.payment_channel_id is None and s.payments_configured
    assert not _settings(TG_SESSION="").userbot_enabled
    assert not _settings(TG_API_ID="").userbot_enabled
    with pytest.raises(ValueError):
        _settings(BANK_BOT_USERNAME="bad name!")


def test_session_and_hash_never_logged(caplog):
    s = _settings()
    log = logging.getLogger("test.userbot")
    log.addFilter(SecretRedactingFilter(s.secret_values()))
    with caplog.at_level(logging.INFO, logger="test.userbot"):
        log.info("session=%s hash=%s", SESSION, HASH)
    assert SESSION not in caplog.text and HASH not in caplog.text
    assert SESSION not in repr(s)


async def test_disabled_userbot_does_not_start():
    ctx = SimpleNamespace(settings=_settings(TG_SESSION=""))
    assert await userbot.start_userbot(MagicMock(), ctx) is None
    assert not userbot.STATUS.enabled


async def test_bank_event_matches_reservation_and_ignores_duplicate(session_factory, monkeypatch):
    from app.database import repository as repo

    payments = PaymentService(session_factory, 50_000, 300)
    async with session_factory() as s:
        user = await repo.get_or_create_user(s, 555, None, None)
        await s.commit()
    reservation, _ = await payments.reserve(user.id, chat_id=555)

    deliver = AsyncMock(return_value=True)
    monkeypatch.setattr(payment_handlers, "deliver_access", deliver)
    ctx = SimpleNamespace(settings=_settings(ADMIN_IDS=""), payments=payments)
    bot = MagicMock()
    bot.send_message = AsyncMock()

    amount = f"{reservation.amount:,}".replace(",", ".")
    text = f"🎉 To'ldirish\n➕ {amount},00 UZS\n📍 PAYNET P2P\n💳 HUMOCARD *7741\n💰 102.592,77 UZS"
    assert await userbot.handle_bank_event(bot, ctx, 777, 1, text) == "matched"
    deliver.assert_awaited_once()
    assert deliver.call_args.args[2].id == reservation.id
    # Xuddi shu xabar ikkinchi marta kelsa (catch_up), qayta hisoblanmaydi
    assert await userbot.handle_bank_event(bot, ctx, 777, 1, text) == "duplicate"
    # Chiqim xabari e'tiborsiz
    assert await userbot.handle_bank_event(bot, ctx, 777, 2, "💸 To'lov\n➖ 23.000,00 UZS") == "ignored"
