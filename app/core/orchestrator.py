"""Suhbat orkestratori: interviewer -> analyst -> prompt engineer.

Har bir loyiha uchun agent chaqiruvlari soni cheklangan (cheksiz sikl yo'q).
Telegramdan mustaqil: natijani matn bo'laklari ko'rinishida qaytaradi.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.analyst import AnalystAgent, MarketAnalysis
from app.agents.coach import CoachAgent
from app.agents.interviewer import InterviewerAgent, Turn
from app.agents.prompt_engineer import PromptEngineerAgent
from app.core.finance import build_model, finance_summary_for_ai, render_finance
from app.core.logging import user_ref
from app.core.report import build_report_html
from app.core.text import format_reply, render_analysis, render_prompt, split_message
from app.database import repository as repo
from app.database.models import BusinessProject, Conversation, ProjectStatus

logger = logging.getLogger(__name__)


@dataclass
class AnalysisOutput:
    parts: list[str]  # Telegram xabarlari (HTML)
    report_html: str = ""  # yuklab olinadigan to'liq hisobot


@dataclass
class TurnResult:
    """Foydalanuvchiga yuboriladigan xabarlar va keyingi harakat."""

    messages: list[str] = field(default_factory=list)  # HTML
    run_analysis: bool = False
    looking_for_idea: bool = False
    project_id: int | None = None
    reaction: str | None = None  # foydalanuvchi xabariga qo'yiladigan reaksiya


class Orchestrator:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        interviewer: InterviewerAgent,
        analyst: AnalystAgent,
        prompt_engineer: PromptEngineerAgent,
        min_questions: int,
        max_questions: int,
        coach: CoachAgent | None = None,
        followup_days: Callable[[], int] = lambda: 0,
    ) -> None:
        self._sf = session_factory
        self.interviewer = interviewer
        self.analyst = analyst
        self.prompt_engineer = prompt_engineer
        self.coach = coach
        self.min_q = min_questions
        self.max_q = max_questions
        self._followup_days = followup_days

    async def handle_user_text(
        self, *, telegram_id: int, user_db_id: int, text: str, user_name: str = ""
    ) -> TurnResult:
        """Foydalanuvchi xabarini qayta ishlaydi: yangi g'oya yoki savolga javob."""
        async with self._sf() as session:
            project = await repo.get_active_project(session, user_db_id)
            if project and project.status == ProjectStatus.ANALYZING:
                return TurnResult(
                    messages=["Tahlil hali davom etmoqda. Natija tayyor bo'lishi bilan yuboraman."]
                )
            followup_id = project.id if project and project.status == ProjectStatus.FOLLOWUP else None
        if followup_id is not None and self.coach is not None:
            result = await self._followup_turn(telegram_id, user_db_id, followup_id, text, user_name)
            if result is not None:
                return result
        async with self._sf() as session:
            project = await repo.get_active_project(session, user_db_id)
            if project is not None and project.status == ProjectStatus.FOLLOWUP:
                project.status = ProjectStatus.DONE  # coach yo'q yoki yangi g'oya: kuzatuv tugadi
                project = None
            new_project = project is None
            added_msg_id: int | None = None
            if project is None:
                project = await repo.create_project(session, user_db_id, text)
            else:
                added_msg_id = await repo.add_message(session, project.id, "user", text)
            turns = [Turn(role=m.role, text=m.message) for m in await repo.get_messages(session, project.id)]
            idea, asked, project_id = project.idea, project.questions_asked, project.id
            await session.commit()

        try:
            step = await self.interviewer.run(
                user_id=telegram_id,
                idea=idea,
                turns=turns,
                questions_asked=asked,
                min_questions=self.min_q,
                max_questions=self.max_q,
                user_name=user_name,
            )
        except Exception:
            # AI javob bermadi: xabar tarixda "javobsiz" qolib ketmasin va qayta yuborilganda
            # takrorlanmasin — kiritilgan yozuvni bekor qilamiz.
            await self._rollback_turn(project_id, new_project, added_msg_id)
            raise

        async with self._sf() as session:
            project = await session.get(BusinessProject, project_id)
            result = TurnResult(project_id=project_id, reaction=step.reaction)
            if step.intent == "off_topic":
                project.status = ProjectStatus.CANCELLED
                result.messages.append(format_reply(step.reply))
            elif step.intent == "looking_for_idea" and asked == 0:
                project.status = ProjectStatus.CANCELLED
                result.looking_for_idea = True
                result.messages.append(format_reply(step.reply))
            else:
                project.industry = step.industry[:120]
                project.location = step.location[:120]
                if step.summary:
                    project.title = step.summary[:200]
                await repo.add_message(session, project_id, "assistant", step.reply)
                result.messages.append(format_reply(step.reply))
                if step.ready_for_analysis:
                    project.status = ProjectStatus.ANALYZING
                    project.pending_question = None
                    result.run_analysis = True
                else:
                    project.pending_question = step.reply
                    project.questions_asked = asked + 1
            await session.commit()
        logger.info(
            "interview_turn user=%s project=%s asked=%d ready=%s",
            user_ref(telegram_id),
            project_id,
            asked,
            step.ready_for_analysis,
        )
        return result

    async def _rollback_turn(self, project_id: int, new_project: bool, msg_id: int | None) -> None:
        async with self._sf() as session:
            if new_project:
                project = await session.get(BusinessProject, project_id)
                if project is not None:
                    await session.delete(project)
            elif msg_id is not None:
                msg = await session.get(Conversation, msg_id)
                if msg is not None:
                    await session.delete(msg)
            await session.commit()

    async def run_analysis(
        self,
        *,
        telegram_id: int,
        user_db_id: int,
        project_id: int,
        progress: Callable[[str], Awaitable[None]] | None = None,
    ) -> AnalysisOutput:
        """Tahlil va SUPER PROMPT. Xato bo'lsa loyiha suhbat holatiga qaytadi.

        `progress(bosqich)` — foydalanuvchiga jonli holat ko'rsatish uchun:
        market → finance → prompt → report.
        """

        async def notify(stage: str) -> None:
            if progress is None:
                return
            try:
                await progress(stage)
            except Exception:  # noqa: BLE001 - holat xabari tahlilni to'xtatmasin
                logger.debug("progress_failed", exc_info=True)

        async with self._sf() as session:
            project = await repo.get_project(session, user_db_id, project_id)
            if project is None:
                return AnalysisOutput(["Loyiha topilmadi."])
            if project.status != ProjectStatus.ANALYZING:
                project.status = ProjectStatus.ANALYZING  # navbatdan qayta urinishda
                await session.commit()
            messages = await repo.get_messages(session, project_id)
            notes = await repo.recent_research_notes(session, limit=3)
            idea = project.idea
        conversation = "\n\n".join(
            f"{'Maslahatchi' if m.role == 'assistant' else 'Foydalanuvchi'}: {m.message}" for m in messages
        )
        notes_text = "\n\n".join(f"[{n.created_at:%Y-%m-%d}] {n.content}" for n in notes)

        try:
            await notify("market")
            analysis = await self.analyst.run(
                user_id=telegram_id, idea=idea, conversation=conversation, notes=notes_text
            )
            analysis_json = analysis.data.model_dump_json()
            await notify("finance")
            finance = build_model(analysis.data.finance)
            idea_data = f"G'oya: {idea}\n\nSuhbat:\n{conversation}"
            sources_json = json.dumps([s.__dict__ for s in analysis.sources], ensure_ascii=False)
            finance_text = (
                finance_summary_for_ai(finance) if finance.usable else "(moliyaviy ma'lumot yetarli emas)"
            )
            await notify("prompt")
            super_prompt = await self.prompt_engineer.run(
                user_id=telegram_id,
                idea_data=idea_data,
                analysis_json=analysis_json
                + "\nManbalar: "
                + sources_json
                + "\nMOLIYAVIY MODEL (dastur hisobi):\n"
                + finance_text,
            )
        except Exception:
            async with self._sf() as session:
                project = await session.get(BusinessProject, project_id)
                if project:
                    # Qayta urinish mumkin bo'lsin: suhbat holatiga qaytaramiz.
                    project.status = ProjectStatus.INTERVIEWING
                    await session.commit()
            raise

        async with self._sf() as session:
            project = await session.get(BusinessProject, project_id)
            await repo.save_analysis(session, project_id, "analyst", analysis_json)
            await repo.save_prompt(session, project_id, super_prompt.prompt)
            project.status = ProjectStatus.DONE
            days = self._followup_days()
            project.followup_at = datetime.now(UTC) + timedelta(days=days) if days > 0 else None
            title = project.title or idea[:80]
            await session.commit()

        await notify("report")
        out = render_analysis(analysis.data, analysis.sources, analysis.web_used)
        if finance.usable:
            out += split_message(render_finance(finance))
        out += render_prompt(super_prompt.title, super_prompt.prompt, super_prompt.how_to_use)
        report = build_report_html(
            project_id=project_id,
            title=title,
            analysis=analysis.data,
            sources=analysis.sources,
            web_used=analysis.web_used,
            finance=finance if finance.usable else None,
            prompt_title=super_prompt.title,
            prompt_text=super_prompt.prompt,
        )
        return AnalysisOutput(out, report)

    async def cancel_active(self, user_db_id: int) -> bool:
        async with self._sf() as session:
            project = await repo.get_active_project(session, user_db_id)
            if project is None:
                return False
            project.status = (
                ProjectStatus.DONE if project.status == ProjectStatus.FOLLOWUP else ProjectStatus.CANCELLED
            )
            await session.commit()
            return True

    async def _followup_turn(
        self, telegram_id: int, user_db_id: int, project_id: int, text: str, user_name: str
    ) -> TurnResult | None:
        """Kuzatuv suhbati. Foydalanuvchi yangi g'oya yozsa — None (oddiy suhbat boshlanadi)."""
        async with self._sf() as session:
            project = await repo.get_project(session, user_db_id, project_id)
            if project is None:
                return None
            msg_id = await repo.add_message(session, project_id, "user", text)
            turns = [Turn(role=m.role, text=m.message) for m in await repo.get_messages(session, project_id)]
            analysis_json = await repo.latest_analysis(session, user_db_id, project_id)
            idea = project.idea
            await session.commit()
        try:
            step = await self.coach.run(
                user_id=telegram_id,
                idea=idea,
                analysis_summary=_analysis_digest(analysis_json),
                turns=turns[-14:],
                user_name=user_name,
            )
        except Exception:
            await self._rollback_turn(project_id, False, msg_id)
            raise
        async with self._sf() as session:
            project = await session.get(BusinessProject, project_id)
            if step.intent == "new_idea":
                await session.delete(await session.get(Conversation, msg_id))
                project.status = ProjectStatus.DONE
                await session.commit()
                return None
            await repo.add_message(session, project_id, "assistant", step.reply)
            if step.updated_next_steps:
                await repo.save_analysis(
                    session, project_id, "coach", json.dumps(step.updated_next_steps, ensure_ascii=False)
                )
            if step.intent == "stop":
                project.status = ProjectStatus.DONE
            await session.commit()
        messages = [format_reply(step.reply)]
        if step.updated_next_steps:
            steps = "\n".join(f"{i}. {format_reply(x)}" for i, x in enumerate(step.updated_next_steps, 1))
            messages.append("🗺 <b>Yangilangan reja</b>\n\n" + steps)
        if step.intent != "stop":
            messages[-1] += "\n\n<i>Yangi g'oya bilan boshlash uchun: /new</i>"
        logger.info(
            "followup_turn user=%s project=%s intent=%s", user_ref(telegram_id), project_id, step.intent
        )
        return TurnResult(messages=messages, project_id=project_id, reaction=step.reaction)


def _analysis_digest(analysis_json: str | None) -> str:
    """Coach uchun oldingi tahlilning qisqa mazmuni."""
    if not analysis_json:
        return ""
    try:
        a = MarketAnalysis.model_validate_json(analysis_json)
    except ValueError:
        return ""
    lines = [
        f"Xulosa: {a.summary}",
        f"Qaror: {a.verdict}",
        f"Kichik test (MVP): {a.mvp_test}",
        "Keyingi qadamlar: " + "; ".join(a.next_steps),
        "Asosiy xavflar: " + "; ".join(r.risk for r in a.failure_risks[:5]),
    ]
    f = a.finance
    if f.price_per_unit:
        lines.append(
            f"Moliya (reja): narx {f.price_per_unit}, tannarx {f.variable_cost_per_unit}, "
            f"doimiy xarajat {f.monthly_fixed_costs}/oy, realistik sotuv {f.units_realistic}/oy"
        )
    return "\n".join(lines)
