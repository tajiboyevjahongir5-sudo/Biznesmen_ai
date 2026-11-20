"""TEXNIK PROMPT: dasturiy g'oyalar uchun kod, xavfsizlik va UI/UX bo'yicha alohida prompt."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from app.agents.analyst import AnalystAgent
from app.agents.interviewer import InterviewerAgent
from app.agents.prompt_engineer import PromptEngineerAgent, SuperPrompt
from app.agents.tech_architect import TechArchitectAgent, TechPrompt
from app.bot.status import AnalysisProgress
from app.core.orchestrator import Orchestrator
from app.core.report import build_report_html
from app.core.text import render_prompt, render_tech_prompt, split_prompt
from app.database import repository as repo
from app.services.ai_service import AIResult, AIServiceError
from tests.test_ai_and_orchestrator import _analysis, _completion, _gemini_service, _step

TECH_TEXT = (
    "## 1. ROL\nSenior dasturchi.\n\n## 6. XAVFSIZLIK\ninitData HMAC-SHA256 tekshiruvi.\n" + "x" * 1500
)


def _sections(n: int, size: int) -> str:
    return "\n\n".join(f"## {i}. BO'LIM {i}\n" + ("matn qatori\n" * (size // 12)) for i in range(1, n + 1))


def test_split_prompt_keeps_headings_with_their_sections():
    prompt = _sections(8, 700)
    chunks = split_prompt(prompt, limit=1500)
    assert len(chunks) > 1
    for chunk in chunks:
        assert len(chunk) <= 1500
        assert chunk.startswith("## ")  # har bo'lak sarlavhadan boshlanadi
        assert not chunk.rstrip().splitlines()[-1].startswith("##")  # sarlavha oxirida yolg'iz qolmaydi
    joined = "\n\n".join(chunks)
    assert all(f"## {i}. BO'LIM {i}" in joined for i in range(1, 9))


def test_split_prompt_splits_oversized_section_and_short_prompt():
    assert split_prompt("qisqa prompt") == ["qisqa prompt"]
    huge = "## 1. KATTA\n" + "uzun qator matni\n" * 400
    chunks = split_prompt(huge, limit=1000)
    assert len(chunks) > 3 and all(len(c) <= 1000 for c in chunks)


def test_render_tech_prompt_inline_or_file():
    short = render_tech_prompt("Bot", TECH_TEXT, "Cursor ga bering")
    assert "TEXNIK PROMPT" in short[0] and "xavfsizlik" in short[0]
    assert len(short) == 2 and short[1].startswith("<pre><code")
    long = render_tech_prompt("Bot", _sections(12, 2500), "")
    assert len(long) == 1 and ".md faylni" in long[0]  # uzun bo'lsa — faqat fayl


def test_render_prompt_mentions_tech_prompt():
    parts = render_prompt("Kafe", "P" * 300, "", tech_follows=True)
    assert "TEXNIK PROMPT" in parts[0]
    assert "TEXNIK PROMPT" not in render_prompt("Kafe", "P" * 300, "")[0]


def test_report_includes_tech_prompt():
    kwargs = dict(
        project_id=1,
        title="Bot",
        analysis=_analysis(),
        sources=[],
        web_used=False,
        finance=None,
        prompt_title="Bot",
        prompt_text="Biznes prompt",
    )
    assert "TEXNIK PROMPT" not in build_report_html(**kwargs)
    html = build_report_html(**kwargs, tech_prompt_text="initData <HMAC>")
    assert "TEXNIK PROMPT" in html and "initData &lt;HMAC&gt;" in html


async def test_tech_agent_passes_bigger_budget_and_timeout():
    ai = MagicMock()
    ai.structured = AsyncMock(return_value=TechPrompt(title="T", prompt=TECH_TEXT, how_to_use="Bering"))
    agent = TechArchitectAgent(ai, prompt="tizim")
    await agent.run(user_id=1, idea_data="g'oya", analysis_json="{}", product_type="Telegram Web App")
    kw = ai.structured.call_args.kwargs
    assert kw["schema"] is TechPrompt and kw["max_output_tokens"] > 8000 and kw["timeout"] >= 120
    assert "Telegram Web App" in kw["user_input"]


async def test_gemini_call_uses_agent_timeout_and_token_budget(settings):
    ai = _gemini_service(settings, AsyncMock(return_value=_completion(_step().model_dump_json())))
    from app.agents.interviewer import InterviewStep

    await ai.structured(
        user_id=1,
        agent="t",
        instructions="i",
        user_input="u",
        schema=InterviewStep,
        max_output_tokens=16000,
        timeout=180,
    )
    assert ai._client.with_options.call_args.kwargs["timeout"] == 180
    assert ai._client.chat.completions.create.call_args.kwargs["max_tokens"] == 16000


def _orchestrator(session_factory, sp: SuperPrompt, tech_run: AsyncMock | None, enabled: bool = True):
    interviewer = MagicMock(spec=InterviewerAgent)
    interviewer.run = AsyncMock(return_value=_step(ready_for_analysis=True, reply="Rahmat"))
    analyst = MagicMock(spec=AnalystAgent)
    analyst.run = AsyncMock(return_value=AIResult(data=_analysis(), sources=[], web_used=False))
    pe = MagicMock(spec=PromptEngineerAgent)
    pe.run = AsyncMock(return_value=sp)
    tech = None
    if tech_run is not None:
        tech = MagicMock(spec=TechArchitectAgent)
        tech.run = tech_run
    return Orchestrator(
        session_factory, interviewer, analyst, pe, 0, 12, tech_architect=tech, tech_enabled=lambda: enabled
    )


async def _run(orch, session_factory, stages: list[str] | None = None):
    async with session_factory() as s:
        user = await repo.get_or_create_user(s, 555, "u", "U")
        await s.commit()
    r = await orch.handle_user_text(telegram_id=555, user_db_id=user.id, text="Ijara boti qilmoqchiman")

    async def progress(stage: str) -> None:
        if stages is not None:
            stages.append(stage)

    out = await orch.run_analysis(
        telegram_id=555, user_db_id=user.id, project_id=r.project_id, progress=progress
    )
    return user.id, r.project_id, out


SOFTWARE = SuperPrompt(
    title="Ijara boti",
    prompt="P" * 300,
    how_to_use="Nusxa",
    software_product=True,
    product_type="Telegram Web App + bot",
)


async def test_software_idea_gets_tech_prompt(session_factory):
    tech_run = AsyncMock(
        return_value=TechPrompt(title="Ijara boti — texnik", prompt=TECH_TEXT, how_to_use="Bering")
    )
    stages: list[str] = []
    orch = _orchestrator(session_factory, SOFTWARE, tech_run)
    user_id, project_id, out = await _run(orch, session_factory, stages)

    assert stages == ["market", "finance", "prompt", "tech", "report"]
    assert tech_run.call_args.kwargs["product_type"] == "Telegram Web App + bot"
    assert any("TEXNIK PROMPT" in p for p in out.parts)
    assert out.documents and out.documents[0].filename == f"texnik-prompt-{project_id}.md"
    assert "initData HMAC" in out.documents[0].data.decode("utf-8")
    assert "TEXNIK PROMPT" in out.report_html
    async with session_factory() as s:
        assert await repo.latest_tech_prompt(s, user_id, project_id) == TECH_TEXT
        assert await repo.latest_prompt(s, user_id, project_id) == "P" * 300


async def test_non_software_idea_has_no_tech_prompt(session_factory):
    tech_run = AsyncMock()
    sp = SuperPrompt(title="Kafe", prompt="P" * 300, how_to_use="Nusxa")
    stages: list[str] = []
    user_id, project_id, out = await _run(
        _orchestrator(session_factory, sp, tech_run), session_factory, stages
    )
    tech_run.assert_not_awaited()
    assert "tech" not in stages and not out.documents
    async with session_factory() as s:
        assert await repo.latest_tech_prompt(s, user_id, project_id) is None


async def test_tech_prompt_disabled_by_admin(session_factory):
    tech_run = AsyncMock()
    _, _, out = await _run(_orchestrator(session_factory, SOFTWARE, tech_run, enabled=False), session_factory)
    tech_run.assert_not_awaited()
    assert not out.documents


async def test_tech_prompt_failure_does_not_break_analysis(session_factory):
    tech_run = AsyncMock(side_effect=AIServiceError("vaqt tugadi"))
    user_id, project_id, out = await _run(_orchestrator(session_factory, SOFTWARE, tech_run), session_factory)
    assert out.parts and not out.documents
    assert not any("TEXNIK PROMPT" in p for p in out.parts)
    async with session_factory() as s:
        assert await repo.latest_prompt(s, user_id, project_id) == "P" * 300


def _bot():
    bot = MagicMock()
    bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=10))
    bot.edit_message_text = AsyncMock()
    bot.delete_message = AsyncMock()
    return bot


async def test_progress_inserts_tech_stage_before_report():
    bot = _bot()
    progress = AnalysisProgress(bot, 1, tick=10)
    await progress.start()
    assert "TEXNIK PROMPT" not in bot.send_message.call_args.args[1]
    await progress.stage("tech")
    text = bot.edit_message_text.call_args.args[0]
    assert "✅ SUPER PROMPT" in text and "TEXNIK PROMPT" in text and "▫️ Hisobot" in text
    assert text.index("TEXNIK PROMPT") < text.index("Hisobot")
    await progress.stage("report")
    await progress.finish(ok=True)
    assert "✅ TEXNIK PROMPT" in bot.edit_message_text.call_args.args[0]
