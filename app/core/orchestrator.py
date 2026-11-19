"""Suhbat orkestratori: interviewer -> analyst -> prompt engineer.

Har bir loyiha uchun agent chaqiruvlari soni cheklangan (cheksiz sikl yo'q).
Telegramdan mustaqil: natijani matn bo'laklari ko'rinishida qaytaradi.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.analyst import AnalystAgent
from app.agents.interviewer import InterviewerAgent, Turn
from app.agents.prompt_engineer import PromptEngineerAgent
from app.core.logging import user_ref
from app.core.text import e, render_analysis, render_prompt
from app.database import repository as repo
from app.database.models import BusinessProject, ProjectStatus

logger = logging.getLogger(__name__)


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
    ) -> None:
        self._sf = session_factory
        self.interviewer = interviewer
        self.analyst = analyst
        self.prompt_engineer = prompt_engineer
        self.min_q = min_questions
        self.max_q = max_questions

    async def handle_user_text(self, *, telegram_id: int, user_db_id: int, text: str) -> TurnResult:
        """Foydalanuvchi xabarini qayta ishlaydi: yangi g'oya yoki savolga javob."""
        async with self._sf() as session:
            project = await repo.get_active_project(session, user_db_id)
            if project and project.status == ProjectStatus.ANALYZING:
                return TurnResult(
                    messages=["Tahlil hali davom etmoqda. Natija tayyor bo'lishi bilan yuboraman."]
                )
            if project is None:
                project = await repo.create_project(session, user_db_id, text)
            else:
                await repo.add_message(session, project.id, "user", text)
            turns = [Turn(role=m.role, text=m.message) for m in await repo.get_messages(session, project.id)]
            idea, asked, project_id = project.idea, project.questions_asked, project.id
            await session.commit()

        step = await self.interviewer.run(
            user_id=telegram_id,
            idea=idea,
            turns=turns,
            questions_asked=asked,
            min_questions=self.min_q,
            max_questions=self.max_q,
        )

        async with self._sf() as session:
            project = await session.get(BusinessProject, project_id)
            result = TurnResult(project_id=project_id, reaction=step.reaction)
            if step.intent == "off_topic":
                project.status = ProjectStatus.CANCELLED
                result.messages.append(e(step.reply))
            elif step.intent == "looking_for_idea" and asked == 0:
                project.status = ProjectStatus.CANCELLED
                result.looking_for_idea = True
                result.messages.append(e(step.reply))
            else:
                project.industry = step.industry[:120]
                project.location = step.location[:120]
                if step.summary:
                    project.title = step.summary[:200]
                await repo.add_message(session, project_id, "assistant", step.reply)
                result.messages.append(e(step.reply))
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

    async def run_analysis(self, *, telegram_id: int, user_db_id: int, project_id: int) -> list[str]:
        """Tahlil va SUPER PROMPT. Xato bo'lsa loyiha suhbat holatiga qaytadi."""
        async with self._sf() as session:
            project = await repo.get_project(session, user_db_id, project_id)
            if project is None:
                return ["Loyiha topilmadi."]
            messages = await repo.get_messages(session, project_id)
            notes = await repo.recent_research_notes(session, limit=3)
            idea = project.idea
        conversation = "\n\n".join(
            f"{'Maslahatchi' if m.role == 'assistant' else 'Foydalanuvchi'}: {m.message}" for m in messages
        )
        notes_text = "\n\n".join(f"[{n.created_at:%Y-%m-%d}] {n.content}" for n in notes)

        try:
            analysis = await self.analyst.run(
                user_id=telegram_id, idea=idea, conversation=conversation, notes=notes_text
            )
            analysis_json = analysis.data.model_dump_json()
            idea_data = f"G'oya: {idea}\n\nSuhbat:\n{conversation}"
            sources_json = json.dumps([s.__dict__ for s in analysis.sources], ensure_ascii=False)
            super_prompt = await self.prompt_engineer.run(
                user_id=telegram_id,
                idea_data=idea_data,
                analysis_json=analysis_json + "\nManbalar: " + sources_json,
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
            await session.commit()

        out = render_analysis(analysis.data, analysis.sources, analysis.web_used)
        out += render_prompt(super_prompt.title, super_prompt.prompt, super_prompt.how_to_use)
        return out

    async def cancel_active(self, user_db_id: int) -> bool:
        async with self._sf() as session:
            project = await repo.get_active_project(session, user_db_id)
            if project is None:
                return False
            project.status = ProjectStatus.CANCELLED
            await session.commit()
            return True
