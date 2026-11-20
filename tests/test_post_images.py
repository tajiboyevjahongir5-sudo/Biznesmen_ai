"""Kanal postlari uchun AI rasm: model tanlash, rasmni o'qish, rasmli/matnli joylash."""

import base64
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from app.agents.analyst import Claim
from app.agents.idea_scout import ScoutIdea
from app.config import Settings
from app.core.text import CAPTION_LIMIT, render_channel_caption, render_channel_post
from app.database.models import ChannelIdea, IdeaStatus
from app.services.ai_service import AIServiceError
from app.services.channel_service import ChannelService, image_prompt
from app.services.gemini_native import GeminiNative, GeminiNativeError, GeneratedImage

KEY = "AIzaFakeGeminiKeyForTests_0123456789abcd"
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64


def _settings(**kw) -> Settings:
    data = dict(
        _env_file=None,
        AI_PROVIDER="gemini",
        GEMINI_API_KEY=KEY,
        GEMINI_MODEL="g-main",
        IDEAS_CHANNEL_ID="-1001",
    )
    data.update(kw)
    return Settings(**data)


def _native(handler, **kw) -> tuple[GeminiNative, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    return GeminiNative(_settings(**kw), http=httpx.AsyncClient(transport=httpx.MockTransport(wrapped))), seen


MODELS = {
    "models": [
        {"name": "models/gemini-2.5-flash", "supportedGenerationMethods": ["generateContent"]},
        {"name": "models/imagen-4.0-generate-001", "supportedGenerationMethods": ["predict"]},
        {"name": "models/gemini-3-pro-image-preview", "supportedGenerationMethods": ["generateContent"]},
        {"name": "models/gemini-2.5-flash-image", "supportedGenerationMethods": ["generateContent"]},
        {"name": "models/gemini-2.5-flash-image-preview", "supportedGenerationMethods": ["generateContent"]},
    ]
}


def _image_response(data: bytes = PNG, mime: str = "image/png") -> dict:
    return {
        "candidates": [
            {
                "content": {
                    "parts": [
                        {"text": "Mana rasm"},
                        {"inlineData": {"mimeType": mime, "data": base64.b64encode(data).decode()}},
                    ]
                }
            }
        ]
    }


async def test_image_model_autodetect_prefers_stable_flash():
    native, seen = _native(lambda r: httpx.Response(200, json=MODELS))
    models = await native.image_models()
    assert models[0] == "gemini-2.5-flash-image"
    assert "imagen-4.0-generate-001" not in models and "gemini-2.5-flash" not in models
    assert KEY not in str(seen[0].url) and seen[0].headers["x-goog-api-key"] == KEY


async def test_generate_image_parses_inline_data_and_caches_model():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=MODELS)
        return httpx.Response(200, json=_image_response())

    native, seen = _native(handler)
    img = await native.generate_image("kafe")
    assert img.data == PNG and img.mime == "image/png" and img.filename == "idea.png"
    assert img.model == "gemini-2.5-flash-image"
    body = json.loads(seen[1].content)
    assert body["generationConfig"]["responseModalities"] == ["TEXT", "IMAGE"]
    assert str(seen[1].url).endswith("/models/gemini-2.5-flash-image:generateContent")
    await native.generate_image("kafe 2")
    assert [r.method for r in seen].count("GET") == 1  # model bir marta aniqlanadi


async def test_generate_image_retries_without_image_config_on_400():
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        bodies.append(body)
        if "imageConfig" in body["generationConfig"]:
            return httpx.Response(400, json={"error": {"message": "imageConfig not supported"}})
        return httpx.Response(200, json=_image_response(mime="image/jpeg"))

    native, _ = _native(handler, GEMINI_IMAGE_MODEL="my-image-model")
    img = await native.generate_image("kafe")
    assert img.filename == "idea.jpg" and len(bodies) == 2


async def test_generate_image_without_image_raises():
    blocked = {"candidates": [{"content": {"parts": [{"text": "yo'q"}]}, "finishReason": "IMAGE_SAFETY"}]}
    native, _ = _native(lambda r: httpx.Response(200, json=blocked), GEMINI_IMAGE_MODEL="m")
    with pytest.raises(GeminiNativeError) as info:
        await native.generate_image("kafe")
    assert "IMAGE_SAFETY" in str(info.value)


def test_image_model_name_is_validated():
    assert _settings(GEMINI_IMAGE_MODEL="models/gemini-2.5-flash-image").gemini_image_model == (
        "gemini-2.5-flash-image"
    )
    with pytest.raises(ValueError):
        _settings(GEMINI_IMAGE_MODEL="../../evil?x=1")


def _idea(**kw) -> ScoutIdea:
    data = dict(
        title="T" * 120,
        one_liner="O" * 400,
        target_customers="K" * 500,
        why_demand_high="D & <b>" * 100,
        why_competition_low="C" * 800,
        demand_level="yuqori",
        competition_level="past",
        startup_cost_estimate="S" * 300,
        first_steps=["a"],
        risks=["r1", "r2"],
        evidence=[Claim(text="x", kind="taxmin")],
    )
    data.update(kw)
    return ScoutIdea(**data)


def test_caption_fits_telegram_limit_even_for_longest_idea():
    caption = render_channel_caption(_idea(), web_used=False)
    assert len(caption) <= CAPTION_LIMIT
    assert "&lt;b&gt;" in caption  # AI matni HTML sifatida bajarilmaydi
    assert "SUPER PROMPT" in caption


def test_image_prompt_forbids_text():
    p = image_prompt("Kafe", "Talabalar uchun")
    assert "no text" in p and "Kafe. Talabalar uchun" in p


async def _draft(session_factory) -> int:
    idea = _idea(title="Nonushta yetkazish", one_liner="Ofislarga issiq nonushta")
    async with session_factory() as s:
        row = ChannelIdea(
            title=idea.title,
            post_text=render_channel_post(idea, True),
            details=idea.model_dump_json(),
            prompt_text="PROMPT",
            sources="[]",
            web_researched=True,
            status=IdeaStatus.DRAFT,
        )
        s.add(row)
        await s.commit()
        return row.id


def _bot():
    bot = MagicMock()
    bot.send_photo = AsyncMock(return_value=SimpleNamespace(message_id=77))
    bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=78))
    return bot


async def test_publish_with_image(session_factory):
    idea_id = await _draft(session_factory)
    ai = MagicMock()
    ai.generate_image = AsyncMock(return_value=GeneratedImage(PNG, "image/png", "m"))
    svc = ChannelService(_settings(POST_IMAGES="true"), session_factory, MagicMock(), MagicMock(), ai=ai)
    bot = _bot()
    idea = await svc.publish(bot, idea_id, "TestBot")
    assert idea.status == IdeaStatus.PUBLISHED and idea.channel_message_id == 77
    bot.send_message.assert_not_awaited()
    kwargs = bot.send_photo.call_args.kwargs
    assert len(kwargs["caption"]) <= CAPTION_LIMIT and "Nonushta yetkazish" in kwargs["caption"]
    assert "start=idea_" in kwargs["reply_markup"].inline_keyboard[0][0].url
    # Qayta joylash bo'lmaydi
    assert await svc.publish(bot, idea_id, "TestBot") is None


async def test_publish_falls_back_to_text_when_image_fails(session_factory):
    idea_id = await _draft(session_factory)
    ai = MagicMock()
    ai.generate_image = AsyncMock(side_effect=AIServiceError())
    svc = ChannelService(_settings(POST_IMAGES="true"), session_factory, MagicMock(), MagicMock(), ai=ai)
    bot = _bot()
    idea = await svc.publish(bot, idea_id, "TestBot")
    assert idea.channel_message_id == 78
    bot.send_photo.assert_not_awaited()


async def test_publish_without_images_does_not_call_ai(session_factory):
    idea_id = await _draft(session_factory)
    ai = MagicMock()
    ai.generate_image = AsyncMock()
    svc = ChannelService(_settings(), session_factory, MagicMock(), MagicMock(), ai=ai)
    bot = _bot()
    await svc.publish(bot, idea_id, "TestBot")
    ai.generate_image.assert_not_awaited()
    bot.send_message.assert_awaited_once()


FREE_TIER_429 = {
    "error": {
        "code": 429,
        "message": "You exceeded your current quota. Quota exceeded for metric: "
        "generativelanguage.googleapis.com/generate_content_free_tier_requests, limit: 0, model: x",
    }
}


async def test_image_falls_back_to_next_model_on_quota_error():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=MODELS)
        if "gemini-2.5-flash-image:" in str(request.url):
            return httpx.Response(429, json=FREE_TIER_429)
        return httpx.Response(200, json=_image_response())

    native, seen = _native(handler)
    img = await native.generate_image("kafe")
    assert img.model != "gemini-2.5-flash-image"
    # Keyingi safar ishlagan model birinchi sinaladi
    await native.generate_image("kafe 2")
    assert "gemini-2.5-flash-image:" not in str(seen[-1].url)


async def test_all_image_models_fail_with_clear_hint():
    from app.services.gemini_native import image_error_hint

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=MODELS)
        return httpx.Response(429, json=FREE_TIER_429)

    native, _ = _native(handler)
    with pytest.raises(GeminiNativeError) as info:
        await native.generate_image("kafe")
    assert len(info.value.tried) == 3 and "limit: 0" in info.value.detail
    assert "billing" in image_error_hint(info.value)


async def test_admin_failure_text_shows_google_reason_without_key():
    from app.bot.handlers.admin import _image_failure_text
    from app.services.ai_service import AIRateLimitedError

    cause = GeminiNativeError(429, FREE_TIER_429["error"]["message"] + " key=" + KEY)
    cause.tried = ["gemini-2.5-flash-image"]
    try:
        raise AIRateLimitedError() from cause
    except AIRateLimitedError as exc:
        text = _image_failure_text(exc, SimpleNamespace(settings=_settings(POST_IMAGES="true")))
    assert "limit: 0" in text and "billing" in text and "gemini-2.5-flash-image" in text
    assert KEY not in text


async def test_post_images_auto_shortcut_sets_auto_model():
    from app.bot.handlers.admin import post_images_cmd

    ctx = SimpleNamespace(
        settings=_settings(GEMINI_IMAGE_MODEL="old-model"),
        runtime=SimpleNamespace(set=AsyncMock()),
        payments=SimpleNamespace(base_amount=0, timeout=None),
    )
    message = MagicMock()
    message.answer = AsyncMock()
    await post_images_cmd(message, SimpleNamespace(args="auto"), ctx)
    ctx.runtime.set.assert_awaited_once_with("gemini_image_model", "")
