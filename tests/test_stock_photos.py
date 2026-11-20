"""Pexels bepul fotolari: qidiruv, xavfsiz yuklash, muallif ko'rsatish, manbalar tartibi."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from app.agents.idea_scout import ScoutIdea
from app.config import Settings
from app.core.text import CAPTION_LIMIT, render_channel_post
from app.database.models import ChannelIdea, IdeaStatus
from app.services.ai_service import AIServiceError
from app.services.channel_service import ChannelService
from app.services.gemini_native import GeneratedImage
from app.services.post_images import ImageQuery, ImageUnavailableError, PostImageService
from app.services.stock_photos import PexelsClient, StockPhoto, StockPhotoError, clean_query, photo_credit

PEXELS_KEY = "pexelsFakeKey0123456789abcdefghijklmnopqrstuvwxyz"
JPEG = b"\xff\xd8\xff\xe0" + b"1" * 64


def _settings(**kw) -> Settings:
    data = dict(
        _env_file=None,
        AI_PROVIDER="gemini",
        GEMINI_API_KEY="AIzaFakeGeminiKeyForTests_0123456789abcd",
        GEMINI_MODEL="g-main",
        IDEAS_CHANNEL_ID="-1001",
        PEXELS_API_KEY=PEXELS_KEY,
    )
    data.update(kw)
    return Settings(**data)


def _photo(i: int, host: str = "images.pexels.com") -> dict:
    return {
        "id": i,
        "url": f"https://www.pexels.com/photo/x-{i}/",
        "photographer": f"Ali <{i}>",
        "photographer_url": "https://www.pexels.com/@ali",
        "src": {"landscape": f"https://{host}/photos/{i}/p.jpeg?w=1200", "large": ""},
    }


def _client(handler, **kw) -> tuple[PexelsClient, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    http = httpx.AsyncClient(transport=httpx.MockTransport(wrapped))
    return PexelsClient(_settings(**kw), http=http), seen


def _ok_handler(photos: list[dict]):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.pexels.com":
            return httpx.Response(200, json={"photos": photos})
        return httpx.Response(200, content=JPEG, headers={"content-type": "image/jpeg"})

    return handler


async def test_find_downloads_photo_with_credit_and_hides_key():
    client, seen = _client(_ok_handler([_photo(1)]))
    img = await client.find("healthy breakfast delivery")
    assert img.data == JPEG and img.mime == "image/jpeg" and img.model == "pexels"
    assert "Pexels" in img.credit and "&lt;1&gt;" in img.credit  # ism escape qilingan
    search = seen[0]
    assert search.headers["Authorization"] == PEXELS_KEY and PEXELS_KEY not in str(search.url)
    assert search.url.params["orientation"] == "landscape"
    assert seen[1].url.host == "images.pexels.com" and "Authorization" not in seen[1].headers


async def test_foreign_image_hosts_are_ignored():
    client, seen = _client(_ok_handler([_photo(1, host="evil.example.com")]))
    with pytest.raises(StockPhotoError):
        await client.find("cafe")
    assert all(r.url.host != "evil.example.com" for r in seen)  # begona manzilga so'rov yo'q


async def test_recent_photos_are_not_repeated():
    client, _ = _client(_ok_handler([_photo(1), _photo(2)]))
    a = await client.find("cafe")
    b = await client.find("cafe")
    assert a.credit != b.credit or a.data == b.data  # ikkinchi safar boshqa foto tanlanadi
    assert len(client._recent) == 2 and set(client._recent) == {1, 2}


@pytest.mark.parametrize(
    ("status", "text"), [(401, "kaliti noto'g'ri"), (429, "limiti tugadi"), (500, "HTTP 500")]
)
async def test_search_errors_are_readable(status, text):
    client, _ = _client(lambda r: httpx.Response(status, json={}))
    with pytest.raises(StockPhotoError) as info:
        await client.find("cafe")
    assert text in str(info.value)


async def test_no_key_and_empty_results():
    client, seen = _client(_ok_handler([]), PEXELS_API_KEY="")
    assert not client.enabled
    with pytest.raises(StockPhotoError):
        await client.find("cafe")
    assert not seen
    client, _ = _client(_ok_handler([]))
    with pytest.raises(StockPhotoError) as info:
        await client.find("cafe")
    assert "topilmadi" in str(info.value)


def test_clean_query_and_credit_links():
    assert clean_query("Healthy <b>breakfast</b>; DROP!") == "Healthy b breakfast b DROP"
    assert clean_query("нонушта") == ""
    bad = StockPhoto(1, "https://images.pexels.com/x.jpg", "javascript:alert(1)", "Ali", "http://evil.com")
    credit = photo_credit(bad)
    assert "javascript" not in credit and "evil.com" not in credit and "Ali" in credit


def _ai_mock(query: str = "kids robotics class"):
    ai = MagicMock()
    ai.structured = AsyncMock(return_value=ImageQuery(query=query))
    ai.generate_image = AsyncMock(return_value=GeneratedImage(b"png", "image/png", "ai-model"))
    return ai


async def test_service_uses_ai_query_when_idea_has_none():
    client, seen = _client(_ok_handler([_photo(5)]))
    ai = _ai_mock()
    svc = PostImageService(_settings(), ai, client)
    img = await svc.make("Bolalar robototexnikasi", "To'garak", "")
    assert img.model == "pexels"
    assert seen[0].url.params["query"] == "kids robotics class"
    ai.generate_image.assert_not_awaited()


async def test_auto_falls_back_to_ai_and_stock_only_reports_errors():
    client, _ = _client(_ok_handler([]))
    ai = _ai_mock()
    img = await PostImageService(_settings(POST_IMAGE_SOURCE="auto"), ai, client).make("T", "O", "cafe")
    assert img.model == "ai-model"

    ai.generate_image = AsyncMock(side_effect=AIServiceError())
    with pytest.raises(ImageUnavailableError) as info:
        await PostImageService(_settings(POST_IMAGE_SOURCE="stock"), ai, client).make("T", "O", "cafe")
    assert [src for src, _ in info.value.errors] == ["stock"]
    ai.generate_image.assert_not_awaited()  # stock rejimida AI chaqirilmaydi


def test_image_source_setting_is_validated():
    assert _settings(POST_IMAGE_SOURCE="AUTO").post_image_source == "auto"
    with pytest.raises(ValueError):
        _settings(POST_IMAGE_SOURCE="google")
    assert PEXELS_KEY in _settings().secret_values()  # loglardan yashiriladi


async def test_publish_with_stock_photo_adds_credit(session_factory):
    idea = ScoutIdea(
        title="Nonushta yetkazish",
        one_liner="Ofislarga issiq nonushta",
        target_customers="Ofis xodimlari",
        why_demand_high="Vaqt yo'q",
        why_competition_low="Kam",
        demand_level="yuqori",
        competition_level="past",
        startup_cost_estimate="20 mln",
        first_steps=["a"],
        risks=["r"],
        evidence=[],
        image_query="healthy breakfast delivery",
    )
    async with session_factory() as s:
        row = ChannelIdea(
            title=idea.title,
            post_text=render_channel_post(idea, True),
            details=idea.model_dump_json(),
            prompt_text="P",
            sources="[]",
            web_researched=True,
            status=IdeaStatus.DRAFT,
        )
        s.add(row)
        await s.commit()
        idea_id = row.id

    client, seen = _client(_ok_handler([_photo(9)]))
    settings = _settings(POST_IMAGES="true")
    images = PostImageService(settings, None, client)
    svc = ChannelService(settings, session_factory, MagicMock(), MagicMock(), images=images)
    bot = MagicMock()
    bot.send_photo = AsyncMock(return_value=SimpleNamespace(message_id=5))
    bot.send_message = AsyncMock()
    published = await svc.publish(bot, idea_id, "TestBot")
    assert published.channel_message_id == 5
    caption = bot.send_photo.call_args.kwargs["caption"]
    assert "📷" in caption and "Pexels" in caption and len(caption) <= CAPTION_LIMIT
    assert seen[0].url.params["query"] == "healthy breakfast delivery"
    bot.send_message.assert_not_awaited()
