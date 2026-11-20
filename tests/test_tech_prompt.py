"""TEXNIK PROMPT va foydalanuvchiga beriladigan bitta to'liq prompt fayli."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from app.agents.analyst import AnalystAgent
from app.agents.interviewer import InterviewerAgent
from app.agents.prompt_engineer import PromptEngineerAgent, SuperPrompt
from app.agents.tech_architect import TechArchitectAgent, TechBlueprint, TechPrompt, TechSection, assemble
from app.bot.helpers import send_documents
from app.bot.status import AnalysisProgress
from app.core.orchestrator import Orchestrator
from app.core.prompt_file import build_prompt_markdown, prompt_document
from app.core.report import build_report_html
from app.core.text import split_prompt
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


def test_prompt_file_combines_business_and_tech_prompts():
    doc = build_prompt_markdown(
        title="Ijara  boti",
        prompt="## 1. ROL\nBiznes strateg.",
        tech_prompt=TECH_TEXT,
        how_to_use="ChatGPT ga\nyuklang",
        tech_how_to_use="Claude Code ga bering",
        source="Loyiha #7",
    )
    assert doc.startswith("# Ijara boti\n") and "Loyiha #7" in doc
    assert "> ChatGPT ga yuklang" in doc and "> Claude Code ga bering" in doc
    headings = ("QANDAY ISHLATILADI", "AI UCHUN", "# 1-QISM", "Biznes strateg", "# 2-QISM")
    order = [doc.index(h) for h in headings]
    assert order == sorted(order)
    assert doc.index("initData HMAC") > doc.index("# 2-QISM")

    business = build_prompt_markdown(title="Kafe", prompt="Biznes prompt matni")
    assert "# SUPER PROMPT\n\nBiznes prompt matni" in business
    assert "2-QISM" not in business and "TEXNIK" not in business and "Cursor" not in business


def test_prompt_document_has_caption_and_text_fallback():
    doc = prompt_document(7, title="Bot", prompt="P" * 300, tech_prompt=TECH_TEXT, how_to_use="Yuklang")
    assert doc.filename == "toliq-prompt-7.md"
    text = doc.data.decode("utf-8")
    assert "P" * 300 in text and "initData HMAC" in text
    assert "SUPER PROMPT" in doc.caption and "Texnik" in doc.caption and "Yuklang" in doc.caption
    assert doc.fallback and "Faylni yuborib bo'lmadi" in doc.fallback[0]
    assert "initData HMAC" in "".join(doc.fallback)  # zaxira matn ham to'liq


async def test_send_documents_falls_back_to_text_when_file_fails():
    from aiogram.exceptions import TelegramBadRequest

    doc = prompt_document(7, title="Bot", prompt="P" * 300)
    bot = MagicMock()
    bot.send_message = AsyncMock()
    bot.send_document = AsyncMock()
    await send_documents(bot, 1, [doc])
    bot.send_document.assert_awaited_once()
    bot.send_message.assert_not_awaited()
    assert bot.send_document.call_args.kwargs["caption"] == doc.caption

    bot.send_document = AsyncMock(side_effect=TelegramBadRequest(method=MagicMock(), message="fail"))
    await send_documents(bot, 1, [doc])
    sent = [c.args[1] for c in bot.send_message.call_args_list]
    assert sent == doc.fallback


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


PARTS = {
    "tech_part_core": "QISM-CORE",
    "tech_part_security": "QISM-SECURITY",
    "tech_part_design": "QISM-DESIGN",
    "tech_part_delivery": "QISM-DELIVERY",
}
CORE_MD = "## 1. ROL\nSenior dasturchi.\n\n## 5. ARXITEKTURA\nlistings jadvali.\n" + "c" * 2500


def _part_reply(**kwargs):
    instructions = kwargs["instructions"]
    if "QISM-CORE" in instructions:
        return TechBlueprint(title="Ijara boti — texnik", how_to_use="Cursor ga bering", markdown=CORE_MD)
    name = next(v for v in PARTS.values() if v in instructions).split("-")[1]
    return TechSection(markdown=f"## {name}\n" + "d" * 2000)


async def test_tech_agent_writes_four_parts_on_top_of_blueprint():
    ai = MagicMock()
    ai.structured = AsyncMock(side_effect=lambda **kw: _part_reply(**kw))
    agent = TechArchitectAgent(ai, prompt="tizim", parts=PARTS)
    out = await agent.run(user_id=1, idea_data="g'oya", analysis_json="{}", product_type="Telegram Web App")

    calls = [c.kwargs for c in ai.structured.call_args_list]
    assert len(calls) == 4
    assert calls[0]["schema"] is TechBlueprint and "<blueprint>" not in calls[0]["user_input"]
    for kw in calls:
        assert kw["max_output_tokens"] > 8000 and kw["timeout"] >= 120
        assert "Telegram Web App" in kw["user_input"] and kw["instructions"].startswith("tizim")
    for kw in calls[1:]:  # keyingi qismlar 1-qismga (blueprint) tayanadi
        assert kw["schema"] is TechSection and "listings jadvali" in kw["user_input"]

    assert out.title == "Ijara boti — texnik" and out.how_to_use == "Cursor ga bering"
    assert out.prompt.startswith("## MUNDARIJA")
    order = [out.prompt.index(h) for h in ("## 1. ROL", "## SECURITY", "## DESIGN", "## DELIVERY")]
    assert order == sorted(order)


async def test_tech_agent_fails_when_a_part_fails():
    ai = MagicMock()

    async def reply(**kw):
        if "QISM-DESIGN" in kw["instructions"]:
            raise AIServiceError("vaqt tugadi")
        return _part_reply(**kw)

    ai.structured = AsyncMock(side_effect=reply)
    agent = TechArchitectAgent(ai, prompt="tizim", parts=PARTS)
    try:
        await agent.run(user_id=1, idea_data="g'oya", analysis_json="{}")
    except AIServiceError:
        pass
    else:
        raise AssertionError("xato kutilgandi")


def test_assemble_adds_table_of_contents():
    doc = assemble(["## 1. ROL\na", "## 6. XAVFSIZLIK\nb", "## 7. UI/UX\nc"])
    assert doc.startswith("## MUNDARIJA\n- 1. ROL\n- 6. XAVFSIZLIK\n- 7. UI/UX")
    assert assemble(["qisqa"]) == "qisqa"


def test_real_prompt_files_load_for_every_part():
    from app.agents.base import load_text
    from app.agents.tech_architect import CORE_PART, DETAIL_PARTS

    texts = [load_text(name) for name in (CORE_PART, *DETAIL_PARTS)]
    joined = "\n".join(texts)
    for section in range(1, 16):
        assert f"## {section}. " in joined  # 15 bo'limning hammasi qaysidir qismda bor
    assert "HMAC" in texts[1] and "HEX" in texts[2] and "ISH REJASI" in texts[3]


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
    assert not any("PROMPT" in p for p in out.parts)  # prompt chatda bo'laklab emas — faylda
    assert len(out.documents) == 1 and out.documents[0].filename == f"toliq-prompt-{project_id}.md"
    text = out.documents[0].data.decode("utf-8")
    assert "P" * 300 in text and "initData HMAC" in text and "2-QISM. TEXNIK PROMPT" in text
    assert "Texnik" in out.documents[0].caption
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
    assert "tech" not in stages and len(out.documents) == 1
    text = out.documents[0].data.decode("utf-8")
    assert "P" * 300 in text and "TEXNIK PROMPT" not in text
    async with session_factory() as s:
        assert await repo.latest_tech_prompt(s, user_id, project_id) is None


async def test_tech_prompt_disabled_by_admin(session_factory):
    tech_run = AsyncMock()
    _, _, out = await _run(_orchestrator(session_factory, SOFTWARE, tech_run, enabled=False), session_factory)
    tech_run.assert_not_awaited()
    assert len(out.documents) == 1 and "TEXNIK PROMPT" not in out.documents[0].data.decode("utf-8")


async def test_tech_prompt_failure_does_not_break_analysis(session_factory):
    tech_run = AsyncMock(side_effect=AIServiceError("vaqt tugadi"))
    user_id, project_id, out = await _run(_orchestrator(session_factory, SOFTWARE, tech_run), session_factory)
    assert out.parts and len(out.documents) == 1
    assert "TEXNIK PROMPT" not in out.documents[0].data.decode("utf-8")
    assert "Texnik" not in out.documents[0].caption
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


async def test_project_gets_short_name_after_analysis(session_factory):
    sp = SOFTWARE.model_copy(update={"project_name": "  Xorazm   maklersiz uy-joy boti "})
    user_id, project_id, out = await _run(_orchestrator(session_factory, sp, None), session_factory)
    async with session_factory() as s:
        project = await repo.get_project(s, user_id, project_id)
    assert project.title == "Xorazm maklersiz uy-joy boti"
    assert "<h1>Xorazm maklersiz uy-joy boti</h1>" in out.report_html


def test_report_clips_long_title():
    html = build_report_html(
        project_id=1,
        title="Uzun xulosa jumlasi " * 20,
        analysis=_analysis(),
        sources=[],
        web_used=False,
        finance=None,
        prompt_title="Bot",
        prompt_text="Biznes prompt",
    )
    h1 = html.split("<h1>")[1].split("</h1>")[0]
    assert len(h1) <= 120 and h1.endswith("…")
