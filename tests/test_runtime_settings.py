import pytest

from app.config import Settings
from app.services.runtime_settings import RuntimeSettings, SettingError, mask_card


async def test_set_validates_saves_and_reloads(settings, session_factory):
    rt = RuntimeSettings(settings, session_factory)
    await rt.set("payment_card_number", "8600 1234 5678 9012")
    await rt.set("channel_price_uzs", "75000")
    await rt.set("ideas_auto_publish", "true")
    assert settings.payment_card_number == "8600123456789012"
    assert settings.channel_price_uzs == 75000
    assert settings.ideas_auto_publish is True

    # Bot qayta ishga tushganda DB dan yuklanadi
    fresh = Settings(_env_file=None)
    assert await RuntimeSettings(fresh, session_factory).load() == 3
    assert fresh.payment_card_number == "8600123456789012"
    assert fresh.channel_price_uzs == 75000


@pytest.mark.parametrize(
    "key,value",
    [
        ("payment_card_number", "1234"),
        ("channel_price_uzs", "10"),
        ("daily_research_time", "25:99"),
        ("channel_price_uzs", "abc"),
    ],
)
async def test_invalid_values_rejected_and_not_saved(settings, session_factory, key, value):
    rt = RuntimeSettings(settings, session_factory)
    before = getattr(settings, key)
    with pytest.raises(SettingError):
        await rt.set(key, value)
    assert getattr(settings, key) == before
    fresh = Settings(_env_file=None)
    assert await RuntimeSettings(fresh, session_factory).load() == 0


async def test_secrets_cannot_be_changed_from_admin_panel(settings, session_factory):
    rt = RuntimeSettings(settings, session_factory)
    for key in ("bot_token", "gemini_api_key", "openai_api_key", "admin_ids_raw", "database_url"):
        with pytest.raises(SettingError):
            await rt.set(key, "x" * 20)


async def test_link_code_is_single_use(settings, session_factory):
    rt = RuntimeSettings(settings, session_factory)
    code = rt.create_link_code("ideas", admin_id=1001)
    assert code.startswith("BMA-")
    assert rt.consume_link_code("bma-wrong") is None
    link = rt.consume_link_code(code.lower())
    assert link and link.kind == "ideas" and link.admin_id == 1001
    assert rt.consume_link_code(code) is None  # ikkinchi marta ishlamaydi


async def test_link_code_expires(settings, session_factory, monkeypatch):
    import app.services.runtime_settings as mod

    now = [1000.0]
    monkeypatch.setattr(mod.time, "monotonic", lambda: now[0])
    rt = RuntimeSettings(settings, session_factory)
    code = rt.create_link_code("payment", admin_id=1001)
    now[0] += mod.LINK_CODE_TTL + 1
    assert rt.consume_link_code(code) is None


async def test_channel_ids_make_payments_configured(settings, session_factory):
    rt = RuntimeSettings(settings, session_factory)
    assert not settings.payments_configured
    await rt.set("ideas_channel_id", "-1001111111111")
    await rt.set("payment_channel_id", "-1002222222222")
    await rt.set("payment_card_number", "8600123456789012")
    assert settings.ideas_channel_id == -1001111111111
    assert settings.payments_configured


def test_mask_card():
    assert mask_card("8600123456789012") == "8600 **** **** 9012"
    assert mask_card("") == "kiritilmagan"
