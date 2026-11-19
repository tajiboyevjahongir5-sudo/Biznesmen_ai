import logging

import pytest

from app.config import Settings
from app.core.logging import REDACTED, SecretRedactingFilter, redact, user_ref
from tests.conftest import FAKE_BOT_TOKEN, FAKE_OPENAI_KEY


def test_config_loads_and_hides_secrets(settings):
    assert settings.openai_model == "test-model"
    assert settings.admin_ids == frozenset({1001})
    assert settings.missing_required() == []
    dumped = repr(settings) + str(settings.model_dump())
    assert FAKE_BOT_TOKEN not in dumped
    assert FAKE_OPENAI_KEY not in dumped


def test_config_reports_missing_and_placeholders(monkeypatch):
    for key in ("BOT_TOKEN", "OPENAI_API_KEY", "OPENAI_MODEL"):
        monkeypatch.delenv(key, raising=False)
    s = Settings(
        _env_file=None, AI_PROVIDER="openai", BOT_TOKEN="CHANGE_ME_telegram_bot_token", OPENAI_MODEL=""
    )
    assert set(s.missing_required()) == {"BOT_TOKEN", "OPENAI_API_KEY", "OPENAI_MODEL"}


def test_gemini_is_default_provider(monkeypatch):
    for key in ("AI_PROVIDER", "GEMINI_API_KEY", "GEMINI_MODEL", "OPENAI_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    s = Settings(_env_file=None, BOT_TOKEN=FAKE_BOT_TOKEN)
    assert s.ai_provider == "gemini"
    assert set(s.missing_required()) == {"GEMINI_API_KEY", "GEMINI_MODEL"}
    s2 = Settings(
        _env_file=None, BOT_TOKEN=FAKE_BOT_TOKEN, GEMINI_API_KEY="AIzaFakeGeminiKey_123456", GEMINI_MODEL="g"
    )
    assert s2.missing_required() == []
    assert s2.ai_base_url.startswith("https://generativelanguage.googleapis.com/")
    assert "AIzaFakeGeminiKey_123456" in s2.secret_values()
    # Internet qidiruvi faqat OpenAI bilan ishlaydi
    s3 = Settings(_env_file=None, RESEARCH_PROVIDER="openai_web")
    assert not s3.web_search_enabled


def test_config_empty_channel_ids_are_none(monkeypatch):
    s = Settings(_env_file=None, IDEAS_CHANNEL_ID="", PAYMENT_CHANNEL_ID="", PAYMENT_CARD_NUMBER="")
    assert s.ideas_channel_id is None
    assert not s.payments_configured


def test_card_number_normalized():
    s = Settings(_env_file=None, PAYMENT_CARD_NUMBER="8600 1234 5678 9012")
    assert s.payment_card_number == "8600123456789012"


def test_redact_known_and_pattern_secrets():
    text = f"token={FAKE_BOT_TOKEN} key={FAKE_OPENAI_KEY} Authorization: Bearer abcdefghijklmnop"
    out = redact(text)
    assert FAKE_BOT_TOKEN not in out
    assert FAKE_OPENAI_KEY not in out
    assert "abcdefghijklmnop" not in out
    assert REDACTED in out


def test_logs_never_contain_secrets(caplog, settings):
    logger = logging.getLogger("test.secrets")
    logger.addFilter(SecretRedactingFilter(settings.secret_values()))
    with caplog.at_level(logging.INFO, logger="test.secrets"):
        logger.info("bot token %s", FAKE_BOT_TOKEN)
        try:
            raise RuntimeError(f"failed with key {FAKE_OPENAI_KEY}")
        except RuntimeError:
            logger.exception("error")
    full = caplog.text + "".join(r.exc_text or "" for r in caplog.records)
    assert FAKE_BOT_TOKEN not in full
    assert FAKE_OPENAI_KEY not in full


def test_user_ref_is_anonymous():
    ref = user_ref(123456789)
    assert "123456789" not in ref
    assert ref == user_ref(123456789)


def test_railway_postgres_url_normalized():
    s = Settings(_env_file=None, DATABASE_URL="postgresql://u:p@host:5432/railway")
    assert s.database_url == "postgresql+asyncpg://u:p@host:5432/railway"
    s2 = Settings(_env_file=None, DATABASE_URL="postgres://u:p@h/db")
    assert s2.database_url.startswith("postgresql+asyncpg://")


@pytest.mark.parametrize("url", ["", "   ", "${{Postgres.DATABASE_URL}}", "not a url", "mysql://u:p@h/db"])
def test_bad_database_url_reported_clearly(url):
    s = Settings(_env_file=None, DATABASE_URL=url)
    assert any(m.startswith("DATABASE_URL") for m in s.missing_required())


def test_quoted_database_url_accepted():
    s = Settings(_env_file=None, DATABASE_URL='"postgresql://u:p@host:5432/railway"')
    assert s.database_url == "postgresql+asyncpg://u:p@host:5432/railway"
    assert not any(m.startswith("DATABASE_URL") for m in s.missing_required())
