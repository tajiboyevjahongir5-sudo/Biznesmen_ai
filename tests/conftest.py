from __future__ import annotations

import pytest

from app.config import Settings
from app.database.session import create_engine, create_session_factory, init_db

FAKE_BOT_TOKEN = "123456789:AAFakeTokenForTestsOnly_abcdefghijklmn"
FAKE_OPENAI_KEY = "sk-test-fake-key-0123456789abcdefghij"


@pytest.fixture
def settings(monkeypatch) -> Settings:
    for key in (
        "BOT_TOKEN",
        "OPENAI_API_KEY",
        "OPENAI_MODEL",
        "ADMIN_IDS",
        "IDEAS_CHANNEL_ID",
        "PAYMENT_CHANNEL_ID",
        "PAYMENT_CARD_NUMBER",
        "RESEARCH_PROVIDER",
    ):
        monkeypatch.delenv(key, raising=False)
    return Settings(
        _env_file=None,
        BOT_TOKEN=FAKE_BOT_TOKEN,
        OPENAI_API_KEY=FAKE_OPENAI_KEY,
        OPENAI_MODEL="test-model",
        ADMIN_IDS="1001",
        LOG_FILE="",
    )


@pytest.fixture
async def session_factory(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    await init_db(engine)
    yield create_session_factory(engine)
    await engine.dispose()
