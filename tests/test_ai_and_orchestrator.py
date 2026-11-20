from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import openai
import pytest

from app.agents.analyst import AnalystAgent, MarketAnalysis
from app.agents.interviewer import InterviewerAgent, InterviewStep
from app.agents.prompt_engineer import PromptEngineerAgent, SuperPrompt
from app.core.orchestrator import Orchestrator
from app.core.text import render_analysis, split_message
from app.database import repository as repo
from app.database.models import BusinessProject, ProjectStatus
from app.services.ai_service import (
    AIBadResponseError,
    AIBusyError,
    AIConfigError,
    AIQuotaExceededError,
    AIRateLimitedError,
    AIResult,
    AIService,
    AIServiceError,
    AITimeoutError,
    AIUnavailableError,
    GlobalLimitError,
    UsageLimitError,
    extract_citations,
)

REQ = httpx.Request("POST", "https://api.openai.com/v1/responses")


def _resp(status: int, code: str | None = None) -> httpx.Response:
    return httpx.Response(status, request=REQ, json={"error": {"code": code}})


def _step(**kw) -> InterviewStep:
    data = dict(
        intent="has_idea",
        reply="Tushundim. Qaysi shaharda?",
        summary="Kafe",
        industry="HoReCa",
        location="Toshkent",
        known_facts=[],
        open_questions=[],
        ready_for_analysis=False,
    )
    data.update(kw)
    return InterviewStep(**data)


def _service(settings, parse_mock) -> AIService:
    client = MagicMock()
    client.responses.parse = parse_mock
    return AIService(settings, client=client)


async def test_structured_success_tracks_usage(settings):
    step = _step()
    resp = SimpleNamespace(
        output_parsed=step,
        usage=SimpleNamespace(input_tokens=100, output_tokens=50),
        output=[],
        status="completed",
    )
    ai = _service(settings, AsyncMock(return_value=resp))
    out = await ai.structured(user_id=1, agent="t", instructions="i", user_input="u", schema=InterviewStep)
    assert out == step
    assert ai.usage.input_tokens == 100 and ai.usage.output_tokens == 50
    kwargs = ai._client.responses.parse.call_args.kwargs
    assert kwargs["model"] == "test-model"  # model .env dan
    assert kwargs["max_output_tokens"] <= settings.openai_max_output_tokens
    assert "tools" not in kwargs  # web qidiruv o'chirilgan


@pytest.mark.parametrize(
    "exc,expected",
    [
        (openai.APITimeoutError(request=REQ), AITimeoutError),
        (openai.APIConnectionError(request=REQ), AIUnavailableError),
        (
            openai.RateLimitError("rl", response=_resp(429), body={"code": "rate_limit_exceeded"}),
            AIRateLimitedError,
        ),
        (
            openai.RateLimitError("q", response=_resp(429), body={"code": "insufficient_quota"}),
            AIQuotaExceededError,
        ),
        (openai.AuthenticationError("auth", response=_resp(401), body=None), AIConfigError),
        (openai.InternalServerError("srv", response=_resp(500), body=None), AIUnavailableError),
    ],
)
async def test_api_errors_mapped_to_safe_errors(settings, exc, expected):
    ai = _service(settings, AsyncMock(side_effect=exc))
    with pytest.raises(expected) as info:
        await ai.structured(user_id=1, agent="t", instructions="i", user_input="u", schema=InterviewStep)
    assert "sk-" not in info.value.user_message


async def test_empty_parsed_output_is_bad_response(settings):
    resp = SimpleNamespace(output_parsed=None, usage=None, output=[], status="incomplete")
    ai = _service(settings, AsyncMock(return_value=resp))
    with pytest.raises(AIBadResponseError):
        await ai.structured(user_id=1, agent="t", instructions="i", user_input="u", schema=InterviewStep)


async def test_per_user_and_global_limits(settings):
    settings.ai_daily_requests_per_user = 2
    settings.ai_daily_requests_global = 3
    resp = SimpleNamespace(output_parsed=_step(), usage=None, output=[])
    ai = _service(settings, AsyncMock(return_value=resp))
    call = dict(agent="t", instructions="i", user_input="u", schema=InterviewStep)
    await ai.structured(user_id=1, **call)
    await ai.structured(user_id=1, **call)
    with pytest.raises(UsageLimitError):
        await ai.structured(user_id=1, **call)
    await ai.structured(user_id=2, **call)
    with pytest.raises(GlobalLimitError):
        await ai.structured(user_id=3, **call)
    assert ai._client.responses.parse.await_count == 3  # limitdan keyin API chaqirilmadi


async def test_missing_config_raises_before_call(settings):
    settings.openai_model = ""
    ai = AIService(settings)
    with pytest.raises(AIConfigError):
        await ai.structured(user_id=1, agent="t", instructions="i", user_input="u", schema=InterviewStep)


def test_extract_citations_only_real_urls():
    ann = [
        SimpleNamespace(type="url_citation", url="https://stat.uz/x", title="Stat"),
        SimpleNamespace(type="url_citation", url="javascript:alert(1)", title="bad"),
        SimpleNamespace(type="url_citation", url="https://stat.uz/x", title="dup"),
    ]
    resp = SimpleNamespace(output=[SimpleNamespace(content=[SimpleNamespace(annotations=ann)])])
    sources = extract_citations(resp)
    assert [s.url for s in sources] == ["https://stat.uz/x"]


def _analysis() -> MarketAnalysis:
    return MarketAnalysis(
        summary="Qisqa <b>xulosa</b>",
        demand_level="o'rta",
        demand_evidence=[],
        competition_level="yuqori",
        competitors=[],
        differentiation="",
        strengths=[],
        failure_risks=[
            {"risk": "Raqobat", "why": "Ko'p kafe", "probability": "yuqori", "prevention": "Nisha tanlash"}
        ],
        wrong_assumptions=[],
        debate=[],
        must_verify=[],
        mvp_test="1 hafta pop-up",
        verdict="Avval kichik test qilish kerak",
        verdict_reasons=["Talab tekshirilmagan"],
        next_steps=["Mijozlar bilan gaplashish"],
        confidence="past",
    )


def test_render_analysis_escapes_ai_html_and_warns_without_web():
    parts = render_analysis(_analysis(), [], web_used=False)
    joined = "\n".join(parts)
    assert "&lt;b&gt;xulosa&lt;/b&gt;" in joined
    assert "internet qidiruvisiz" in joined
    assert "NIMA UCHUN MUVAFFAQIYATSIZ" in joined
    assert all(len(p) <= 4096 for p in parts)


def test_split_message_respects_limit():
    text = "\n".join(f"qator {i} " + "x" * 200 for i in range(100))
    parts = split_message(text, limit=1000)
    assert all(len(p) <= 1000 for p in parts)
    assert "".join(parts).replace("\n", "") == text.replace("\n", "")


async def test_orchestrator_full_flow(session_factory):
    interviewer = MagicMock(spec=InterviewerAgent)
    interviewer.run = AsyncMock(side_effect=[_step(), _step(ready_for_analysis=True, reply="Rahmat")])
    analyst = MagicMock(spec=AnalystAgent)
    analyst.run = AsyncMock(return_value=AIResult(data=_analysis(), sources=[], web_used=False))
    pe = MagicMock(spec=PromptEngineerAgent)
    pe.run = AsyncMock(return_value=SuperPrompt(title="Kafe", prompt="P" * 300, how_to_use="Nusxa oling"))
    orch = Orchestrator(session_factory, interviewer, analyst, pe, 0, 12)

    async with session_factory() as s:
        user = await repo.get_or_create_user(s, 555, "u", "U")
        other = await repo.get_or_create_user(s, 666, "o", "O")
        await s.commit()

    r1 = await orch.handle_user_text(telegram_id=555, user_db_id=user.id, text="Kafe ochmoqchiman")
    assert not r1.run_analysis and r1.messages
    r2 = await orch.handle_user_text(telegram_id=555, user_db_id=user.id, text="Toshkentda")
    assert r2.run_analysis and r2.project_id == r1.project_id

    out = await orch.run_analysis(telegram_id=555, user_db_id=user.id, project_id=r2.project_id)
    assert len(out.documents) == 1 and "SUPER PROMPT" in out.documents[0].caption
    assert "P" * 300 in out.documents[0].data.decode("utf-8")
    assert out.report_html.startswith("<!DOCTYPE html>") and "P" * 300 in out.report_html

    async with session_factory() as s:
        project = await s.get(BusinessProject, r1.project_id)
        assert project.status == ProjectStatus.DONE
        assert await repo.latest_prompt(s, user.id, project.id) == "P" * 300
        # Boshqa foydalanuvchi bu loyihaga kira olmaydi
        assert await repo.get_project(s, other.id, project.id) is None
        assert await repo.latest_prompt(s, other.id, project.id) is None
        assert not await repo.delete_project(s, other.id, project.id)
        # Egasi o'chira oladi
        assert await repo.delete_project(s, user.id, project.id)
        await s.commit()
    async with session_factory() as s:
        assert await s.get(BusinessProject, r1.project_id) is None
        assert await repo.get_messages(s, r1.project_id) == []


async def test_orchestrator_analysis_failure_allows_retry(session_factory):
    interviewer = MagicMock(spec=InterviewerAgent)
    interviewer.run = AsyncMock(return_value=_step(ready_for_analysis=True))
    analyst = MagicMock(spec=AnalystAgent)
    analyst.run = AsyncMock(side_effect=AIUnavailableError())
    orch = Orchestrator(session_factory, interviewer, analyst, MagicMock(spec=PromptEngineerAgent), 0, 12)
    async with session_factory() as s:
        user = await repo.get_or_create_user(s, 777, None, None)
        await s.commit()
    r = await orch.handle_user_text(telegram_id=777, user_db_id=user.id, text="Do'kon ochaman")
    with pytest.raises(AIUnavailableError):
        await orch.run_analysis(telegram_id=777, user_db_id=user.id, project_id=r.project_id)
    async with session_factory() as s:
        assert (await s.get(BusinessProject, r.project_id)).status == ProjectStatus.INTERVIEWING


async def test_interviewer_enforces_question_limits():
    ai = MagicMock()
    ai.structured = AsyncMock(return_value=_step(ready_for_analysis=True))
    agent = InterviewerAgent(ai, prompt="p")
    early = await agent.run(
        user_id=1, idea="x", turns=[], questions_asked=1, min_questions=6, max_questions=12
    )
    assert not early.ready_for_analysis  # minimal savollar soniga yetmagan
    ai.structured = AsyncMock(return_value=_step(ready_for_analysis=False))
    late = await agent.run(
        user_id=1, idea="x", turns=[], questions_asked=12, min_questions=6, max_questions=12
    )
    assert late.ready_for_analysis  # cheksiz savol berilmaydi


def _gemini_service(settings, create_mock, fallbacks: str = "") -> AIService:
    settings.ai_provider = "gemini"
    settings.gemini_api_key = "AIzaFakeGeminiKey_123456"
    settings.gemini_model = "gemini-test"
    settings.gemini_fallback_models = fallbacks
    client = MagicMock()
    client.chat.completions.create = create_mock
    client.with_options = MagicMock(return_value=client)
    ai = AIService(settings, client=client)
    ai._retry_delays = (0.0,)  # testlarda kutmaymiz
    return ai


def _completion(content: str) -> SimpleNamespace:
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content), finish_reason="stop")],
        usage=SimpleNamespace(prompt_tokens=30, completion_tokens=20),
    )


async def test_gemini_structured_output(settings):
    step = _step()
    content = "```json\n" + step.model_dump_json() + "\n```"
    ai = _gemini_service(settings, AsyncMock(return_value=_completion(content)))
    out = await ai.structured(user_id=1, agent="t", instructions="i", user_input="u", schema=InterviewStep)
    assert out == step
    assert ai.usage.input_tokens == 30 and ai.usage.output_tokens == 20
    kwargs = ai._client.chat.completions.create.call_args.kwargs
    assert kwargs["model"] == "gemini-test"
    assert kwargs["response_format"] == {"type": "json_object"}
    assert "ready_for_analysis" in kwargs["messages"][0]["content"]  # schema tizim promptida
    assert kwargs["messages"][1] == {"role": "user", "content": "u"}  # foydalanuvchi alohida


async def test_gemini_invalid_json_is_bad_response(settings):
    ai = _gemini_service(settings, AsyncMock(return_value=_completion('{"intent": "nimadir"}')))
    with pytest.raises(AIBadResponseError):
        await ai.structured(user_id=1, agent="t", instructions="i", user_input="u", schema=InterviewStep)


async def test_gemini_never_uses_web_search(settings):
    settings.research_provider = "openai_web"
    ai = _gemini_service(settings, AsyncMock(return_value=_completion(_step().model_dump_json())))
    res = await ai.structured_with_sources(
        user_id=1, agent="t", instructions="i", user_input="u", schema=InterviewStep, web_search=True
    )
    assert not res.web_used and res.sources == []


async def test_diagnose_reports_missing_model_and_hides_key(settings):
    settings.ai_provider = "gemini"
    key = "AIzaFakeGeminiKey_1234567890abcdef"
    settings.gemini_api_key = key
    settings.gemini_model = "gemini-old"
    client = MagicMock()
    client.models.list = AsyncMock(
        return_value=SimpleNamespace(
            data=[SimpleNamespace(id="models/gemini-2.5-flash"), SimpleNamespace(id="models/gemini-2.5-pro")]
        )
    )
    client.chat.completions.create = AsyncMock(
        side_effect=openai.NotFoundError(f"model not found for key {key}", response=_resp(404), body=None)
    )
    client.with_options = MagicMock(return_value=client)
    lines = await AIService(settings, client=client).diagnose()
    text = "\n".join(lines)
    assert "RO'YXATDA YO'Q" in text
    assert "gemini-2.5-flash" in text
    assert "status=404" in text
    assert key not in text


def _busy() -> openai.InternalServerError:
    return openai.InternalServerError("high demand", response=_resp(503), body=None)


async def test_gemini_falls_back_to_next_model_when_busy(settings):
    ok = _completion(_step().model_dump_json())
    create = AsyncMock(side_effect=[_busy(), openai.APITimeoutError(request=REQ), ok])
    ai = _gemini_service(settings, create, fallbacks="gemini-b, gemini-c,gemini-test")
    out = await ai.structured(user_id=1, agent="t", instructions="i", user_input="u", schema=InterviewStep)
    assert out == _step()
    models = [c.kwargs["model"] for c in create.call_args_list]
    assert models == ["gemini-test", "gemini-b", "gemini-c"]  # takror model qayta sinalmaydi
    assert all(c.kwargs["reasoning_effort"] == "low" for c in create.call_args_list)


async def test_gemini_all_models_busy_gives_busy_message(settings):
    create = AsyncMock(side_effect=[_busy(), _busy()])
    ai = _gemini_service(settings, create, fallbacks="gemini-b")
    with pytest.raises(AIBusyError) as info:
        await ai.structured(user_id=1, agent="t", instructions="i", user_input="u", schema=InterviewStep)
    assert "band" in info.value.user_message


async def test_gemini_bad_request_not_retried_on_other_models(settings):
    bad = openai.BadRequestError("invalid argument", response=_resp(400), body=None)
    create = AsyncMock(side_effect=bad)
    ai = _gemini_service(settings, create, fallbacks="gemini-b")
    with pytest.raises(AIServiceError):
        await ai.structured(user_id=1, agent="t", instructions="i", user_input="u", schema=InterviewStep)
    assert create.call_count == 1


async def test_gemini_reasoning_effort_dropped_if_unsupported(settings):
    ok = _completion(_step().model_dump_json())
    bad = openai.BadRequestError("Unknown name reasoning_effort", response=_resp(400), body=None)
    create = AsyncMock(side_effect=[bad, ok, ok])
    ai = _gemini_service(settings, create)
    call = dict(user_id=1, agent="t", instructions="i", user_input="u", schema=InterviewStep)
    await ai.structured(**call)
    await ai.structured(**call)
    assert "reasoning_effort" not in create.call_args_list[1].kwargs
    assert "reasoning_effort" not in create.call_args_list[2].kwargs  # keyin umuman yuborilmaydi


def test_gemini_models_list_and_effort_validation(settings):
    from app.config import Settings

    s = Settings(_env_file=None, GEMINI_MODEL="models/a", GEMINI_FALLBACK_MODELS=" b ,a,, c")
    assert s.gemini_models == ["a", "b", "c"]
    with pytest.raises(ValueError):
        Settings(_env_file=None, AI_REASONING_EFFORT="turbo")


async def test_gemini_bad_json_tries_next_model_and_rounds(settings):
    ok = _completion(_step(reaction="🔥").model_dump_json())
    create = AsyncMock(side_effect=[_completion('{"intent": "x"}'), _busy(), ok])
    ai = _gemini_service(settings, create, fallbacks="gemini-b")
    ai._retry_delays = (0.0, 0.0)
    out = await ai.structured(user_id=1, agent="t", instructions="i", user_input="u", schema=InterviewStep)
    assert out.reaction == "🔥"
    assert [c.kwargs["model"] for c in create.call_args_list] == ["gemini-test", "gemini-b", "gemini-test"]


def test_reaction_falls_back_to_allowed():
    assert _step(reaction="🦖").reaction == "👍"
    assert _step(reaction="❤️").reaction == "❤"
    assert _step().reaction == "👍"
