"""Google qidiruvi, ovoz, bazadagi navbat va kuzatuv (follow-up) testlari."""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from sqlalchemy import select

from app.agents.analyst import AnalystAgent, MarketAnalysis
from app.agents.coach import CoachAgent, CoachStep
from app.agents.interviewer import InterviewerAgent, InterviewStep
from app.agents.prompt_engineer import PromptEngineerAgent
from app.bot import jobs
from app.config import Settings
from app.core.orchestrator import Orchestrator
from app.core.security import UserLocks
from app.database import repository as repo
from app.database.models import BusinessProject, PendingJob, ProjectStatus
from app.services.ai_service import AIBusyError, AIService
from app.services.gemini_native import GeminiNative, GeminiNativeError

KEY = "AIzaFakeGeminiKey_9876543210abc"


def _gemini_settings(**kw) -> Settings:
    data = dict(
        _env_file=None,
        AI_PROVIDER="gemini",
        GEMINI_API_KEY=KEY,
        GEMINI_MODEL="g-main",
        GEMINI_FALLBACK_MODELS="g-backup",
        RESEARCH_PROVIDER="gemini_search",
    )
    data.update(kw)
    return Settings(**data)


def _grounded(text: str, urls: list[str]) -> dict:
    return {
        "candidates": [
            {
                "content": {"parts": [{"text": text}]},
                "groundingMetadata": {
                    "groundingChunks": [{"web": {"uri": u, "title": f"t{i}"}} for i, u in enumerate(urls)]
                },
            }
        ]
    }


def _native(handler) -> tuple[GeminiNative, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    s = _gemini_settings()
    return GeminiNative(s, http=httpx.AsyncClient(transport=httpx.MockTransport(wrapped))), seen


async def test_native_search_returns_text_sources_and_hides_key():
    native, seen = _native(
        lambda r: httpx.Response(200, json=_grounded("Raqobatchi X", ["https://a.uz", "http://bad"]))
    )
    res = await native.search("kafe")
    assert res.text == "Raqobatchi X"
    assert [s.url for s in res.sources] == ["https://a.uz"]  # faqat https
    req = seen[0]
    assert KEY not in str(req.url) and req.headers["x-goog-api-key"] == KEY
    body = json.loads(req.content)
    assert body["tools"] == [{"google_search": {}}]
    assert str(req.url).endswith("/v1beta/models/g-main:generateContent")


async def test_native_falls_back_on_busy_and_stops_on_bad_request():
    calls = []

    def handler(r):
        calls.append(str(r.url))
        return (
            httpx.Response(503, json={"error": {"message": "busy"}})
            if "g-main" in str(r.url)
            else httpx.Response(200, json=_grounded("ok", []))
        )

    native, _ = _native(handler)
    assert (await native.search("x")).model == "g-backup"

    native2, seen = _native(lambda r: httpx.Response(400, json={"error": {"message": "bad"}}))
    with pytest.raises(GeminiNativeError):
        await native2.search("x")
    assert len(seen) == 1


async def test_transcribe_sends_inline_audio():
    native, seen = _native(lambda r: httpx.Response(200, json=_grounded("Kafe ochmoqchiman", [])))
    ai = AIService(_gemini_settings(), client=MagicMock(), native=native)
    text = await ai.transcribe(user_id=1, audio=b"OggS-audio", mime="audio/ogg")
    assert text == "Kafe ochmoqchiman"
    part = json.loads(seen[0].content)["contents"][0]["parts"][1]["inline_data"]
    assert part["mime_type"] == "audio/ogg" and part["data"]


def _step_json() -> str:
    return InterviewStep(
        intent="has_idea",
        reply="Savol?",
        summary="s",
        industry="i",
        location="l",
        known_facts=[],
        open_questions=[],
        ready_for_analysis=False,
    ).model_dump_json()


def _completion(content: str):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content), finish_reason="stop")], usage=None
    )


async def test_structured_with_gemini_web_research():
    native, _ = _native(lambda r: httpx.Response(200, json=_grounded("Narx 25 000", ["https://narx.uz"])))
    client = MagicMock()
    client.with_options = MagicMock(return_value=client)
    client.chat.completions.create = AsyncMock(return_value=_completion(_step_json()))
    ai = AIService(_gemini_settings(), client=client, native=native)
    res = await ai.structured_with_sources(
        user_id=1,
        agent="analyst",
        instructions="i",
        user_input="g'oya",
        schema=InterviewStep,
        web_search=True,
    )
    assert res.web_used and [s.url for s in res.sources] == ["https://narx.uz"]
    user_msg = client.chat.completions.create.call_args.kwargs["messages"][1]["content"]
    assert "<web_research>" in user_msg and "https://narx.uz" in user_msg


async def test_web_research_failure_degrades_gracefully():
    native, _ = _native(lambda r: httpx.Response(429, json={"error": {"message": "quota"}}))
    client = MagicMock()
    client.with_options = MagicMock(return_value=client)
    client.chat.completions.create = AsyncMock(return_value=_completion(_step_json()))
    ai = AIService(_gemini_settings(), client=client, native=native)
    res = await ai.structured_with_sources(
        user_id=1, agent="a", instructions="i", user_input="u", schema=InterviewStep, web_search=True
    )
    assert not res.web_used and res.sources == []


def test_web_search_flag_per_provider():
    assert _gemini_settings().web_search_enabled
    assert not _gemini_settings(RESEARCH_PROVIDER="openai_web").web_search_enabled
    assert not _gemini_settings(RESEARCH_PROVIDER="none").web_search_enabled


# ------------------------------------------------------------------ navbat


def _ctx(session_factory, orchestrator):
    return SimpleNamespace(
        session_factory=session_factory,
        user_locks=UserLocks(),
        orchestrator=orchestrator,
        settings=SimpleNamespace(followup_days=7, admin_ids=frozenset(), payments_configured=False),
    )


def _bot():
    bot = MagicMock()
    for name in (
        "send_message",
        "send_chat_action",
        "set_message_reaction",
        "send_document",
        "edit_message_text",
        "delete_message",
    ):
        setattr(bot, name, AsyncMock())
    return bot


async def _make_job(session_factory, ctx, tg=321):
    async with session_factory() as s:
        user = await repo.get_or_create_user(s, tg, None, None)
        await s.commit()
    job = jobs.Job(chat_id=tg, message_id=5, tg_id=tg, db_user_id=user.id, text="Kafe")
    await jobs.enqueue(ctx, job)
    async with session_factory() as s:  # darhol bajarish uchun vaqtini o'tkazamiz
        row = (await s.scalars(select(PendingJob))).one()
        row.next_run_at = datetime.now(UTC) - timedelta(seconds=1)
        await s.commit()
    return job


async def test_pending_job_survives_and_completes(session_factory):
    orch = MagicMock()
    orch.handle_user_text = AsyncMock(
        return_value=SimpleNamespace(
            messages=["Javob"], reaction="🔥", looking_for_idea=False, run_analysis=False, project_id=None
        )
    )
    ctx = _ctx(session_factory, orch)
    await _make_job(session_factory, ctx)
    assert await jobs.has_pending(ctx, 321)
    bot = _bot()
    assert await jobs.process_due_jobs(bot, ctx) == 1
    assert not await jobs.has_pending(ctx, 321)
    # Javob «O'ylayapman...» holat xabari o'rniga tahrir qilib qo'yiladi
    assert bot.send_message.call_args_list[0].args[1] == "💭 <i>O'ylayapman...</i>"
    assert bot.edit_message_text.call_args.args[0] == "Javob"


async def test_pending_job_retries_then_gives_up(session_factory):
    orch = MagicMock()
    orch.handle_user_text = AsyncMock(side_effect=AIBusyError())
    ctx = _ctx(session_factory, orch)
    await _make_job(session_factory, ctx)
    bot = _bot()
    for _ in range(len(jobs.RETRY_DELAYS)):
        async with session_factory() as s:
            for row in (await s.scalars(select(PendingJob))).all():
                row.next_run_at = datetime.now(UTC) - timedelta(seconds=1)
            await s.commit()
        await jobs.process_due_jobs(bot, ctx)
    assert not await jobs.has_pending(ctx, 321)
    assert "Kechirasiz" in bot.send_message.call_args_list[-1].args[1]


# ------------------------------------------------------------------ kuzatuv


def _analysis_json() -> str:
    return MarketAnalysis(
        summary="Kafe",
        demand_level="o'rta",
        demand_evidence=[],
        competition_level="o'rta",
        competitors=[],
        differentiation="",
        strengths=[],
        failure_risks=[],
        wrong_assumptions=[],
        debate=[],
        must_verify=[],
        mvp_test="2 hafta test",
        verdict="Avval kichik test qilish kerak",
        verdict_reasons=[],
        next_steps=["Test"],
        confidence="past",
    ).model_dump_json()


async def _followup_project(session_factory, tg=444):
    async with session_factory() as s:
        user = await repo.get_or_create_user(s, tg, None, None)
        project = await repo.create_project(s, user.id, "Kafe")
        project.status = ProjectStatus.FOLLOWUP
        await repo.save_analysis(s, project.id, "analyst", _analysis_json())
        await s.commit()
        return user, project.id


async def test_followup_turn_uses_coach(session_factory):
    coach = MagicMock(spec=CoachAgent)
    coach.run = AsyncMock(
        return_value=CoachStep(
            intent="progress_update", reply="📈 **Zo'r**", updated_next_steps=["Narxni oshir"]
        )
    )
    interviewer = MagicMock(spec=InterviewerAgent)
    orch = Orchestrator(
        session_factory,
        interviewer,
        MagicMock(spec=AnalystAgent),
        MagicMock(spec=PromptEngineerAgent),
        0,
        12,
        coach=coach,
    )
    user, pid = await _followup_project(session_factory)
    res = await orch.handle_user_text(telegram_id=444, user_db_id=user.id, text="20 ta sotdim")
    assert "<b>Zo'r</b>" in res.messages[0] and "Yangilangan reja" in res.messages[1]
    assert "2 hafta test" in coach.run.call_args.kwargs["analysis_summary"]
    interviewer.run.assert_not_called()
    async with session_factory() as s:
        assert (await s.get(BusinessProject, pid)).status == ProjectStatus.FOLLOWUP
        assert len(await repo.get_messages(s, pid)) == 2


async def test_followup_new_idea_starts_fresh_interview(session_factory):
    coach = MagicMock(spec=CoachAgent)
    coach.run = AsyncMock(return_value=CoachStep(intent="new_idea", reply="Yangi g'oya!"))
    interviewer = MagicMock(spec=InterviewerAgent)
    interviewer.run = AsyncMock(return_value=InterviewStep.model_validate_json(_step_json()))
    orch = Orchestrator(
        session_factory,
        interviewer,
        MagicMock(spec=AnalystAgent),
        MagicMock(spec=PromptEngineerAgent),
        0,
        12,
        coach=coach,
    )
    user, pid = await _followup_project(session_factory, tg=445)
    res = await orch.handle_user_text(telegram_id=445, user_db_id=user.id, text="Endi avtomoyka ochmoqchiman")
    assert res.project_id != pid
    async with session_factory() as s:
        old = await s.get(BusinessProject, pid)
        assert old.status == ProjectStatus.DONE  # tarixda qoladi
        assert await repo.get_messages(s, pid) == []


async def test_due_followups_query(session_factory):
    now = datetime.now(UTC)
    async with session_factory() as s:
        user = await repo.get_or_create_user(s, 446, None, None)
        p = await repo.create_project(s, user.id, "X")
        p.status = ProjectStatus.DONE
        p.followup_at = now - timedelta(hours=1)
        await s.commit()
        due = await repo.due_followups(s, now)
        assert [(x.id, tg) for x, tg in due] == [(p.id, 446)]
        p.followup_sent_at = now
        await s.commit()
        assert await repo.due_followups(s, now) == []
