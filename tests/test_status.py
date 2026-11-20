"""Jonli holat xabarlari: «O'ylayapman» javobga aylanadi, tahlil bosqichlari yangilanadi."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from aiogram.exceptions import TelegramBadRequest

from app.agents.analyst import AnalystAgent
from app.agents.interviewer import InterviewerAgent
from app.agents.prompt_engineer import PromptEngineerAgent, SuperPrompt
from app.bot.status import AnalysisProgress, replace_status
from app.core.orchestrator import Orchestrator
from app.database import repository as repo
from app.services.ai_service import AIResult
from tests.test_ai_and_orchestrator import _analysis, _step


def _bot():
    bot = MagicMock()
    bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=10))
    bot.edit_message_text = AsyncMock()
    bot.delete_message = AsyncMock()
    return bot


async def test_replace_status_edits_first_part_and_sends_rest():
    bot = _bot()
    await replace_status(bot, 1, 10, ["Birinchi", "Ikkinchi"])
    assert bot.edit_message_text.call_args.args[0] == "Birinchi"
    assert bot.edit_message_text.call_args.kwargs["message_id"] == 10
    assert [c.args[1] for c in bot.send_message.call_args_list] == ["Ikkinchi"]
    bot.delete_message.assert_not_awaited()


async def test_replace_status_falls_back_when_edit_fails():
    bot = _bot()
    bot.edit_message_text = AsyncMock(side_effect=TelegramBadRequest(method=MagicMock(), message="not found"))
    await replace_status(bot, 1, 10, ["Javob"])
    bot.delete_message.assert_awaited_once()
    assert bot.send_message.call_args.args[1] == "Javob"


async def test_analysis_progress_marks_stages_and_ticks():
    bot = _bot()
    progress = AnalysisProgress(bot, 1, web=True, tick=0.01)
    await progress.start()
    assert "▫️ SUPER PROMPT" in bot.send_message.call_args.args[1]
    await progress.stage("prompt")
    text = bot.edit_message_text.call_args.args[0]
    assert "✅ Bozor, talab va raqobat 🌐" in text and "✅ Moliyaviy model" in text
    assert "SUPER PROMPT" in text and "▫️ Hisobot" in text
    await asyncio.sleep(0.05)  # vaqt hisoblagichi (⏳/⌛) yangilanadi
    edits = bot.edit_message_text.await_count
    await progress.stage("market")  # orqaga qaytmaydi
    assert bot.edit_message_text.await_count == edits
    await progress.finish(ok=True)
    final = bot.edit_message_text.call_args.args[0]
    assert "Tahlil tayyor" in final and "▫️" not in final and "⏳" not in final


async def test_analysis_progress_removed_on_failure():
    bot = _bot()
    progress = AnalysisProgress(bot, 1, tick=10)
    await progress.start()
    await progress.finish(ok=False)
    bot.delete_message.assert_awaited_once_with(1, 10)


async def test_orchestrator_reports_progress_in_order(session_factory):
    interviewer = MagicMock(spec=InterviewerAgent)
    interviewer.run = AsyncMock(return_value=_step(ready_for_analysis=True, reply="Rahmat"))
    analyst = MagicMock(spec=AnalystAgent)
    analyst.run = AsyncMock(return_value=AIResult(data=_analysis(), sources=[], web_used=False))
    pe = MagicMock(spec=PromptEngineerAgent)
    pe.run = AsyncMock(return_value=SuperPrompt(title="Kafe", prompt="P" * 300, how_to_use="Nusxa"))
    orch = Orchestrator(session_factory, interviewer, analyst, pe, 0, 12)
    async with session_factory() as s:
        user = await repo.get_or_create_user(s, 777, "u", "U")
        await s.commit()
    r = await orch.handle_user_text(telegram_id=777, user_db_id=user.id, text="Kafe ochmoqchiman")
    stages: list[str] = []

    async def progress(stage: str) -> None:
        stages.append(stage)
        if stage == "finance":
            raise RuntimeError("holat xabari xatosi tahlilni to'xtatmasligi kerak")

    out = await orch.run_analysis(
        telegram_id=777, user_db_id=user.id, project_id=r.project_id, progress=progress
    )
    assert stages == ["market", "finance", "prompt", "report"]
    assert out.parts
