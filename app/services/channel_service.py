"""Kanal g'oyalari: kunlik izlanish, saqlash va kanalga joylash."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.idea_scout import SYSTEM_USER_ID, IdeaScoutAgent
from app.agents.prompt_engineer import PromptEngineerAgent
from app.config import Settings
from app.core.text import render_channel_post
from app.database import repository as repo
from app.database.models import ChannelIdea, IdeaStatus

logger = logging.getLogger(__name__)

DEEP_LINK_PREFIX = "idea_"


def idea_deep_link(bot_username: str, idea_id: int) -> str:
    return f"https://t.me/{bot_username}?start={DEEP_LINK_PREFIX}{idea_id}"


class ChannelService:
    def __init__(
        self,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        scout: IdeaScoutAgent,
        prompt_engineer: PromptEngineerAgent,
    ) -> None:
        self.settings = settings
        self._sf = session_factory
        self.scout = scout
        self.prompt_engineer = prompt_engineer

    async def research_and_create_drafts(self) -> list[ChannelIdea]:
        """Kunlik izlanish: yangi g'oyalarni topadi, prompt yozadi va qoralama sifatida saqlaydi."""
        count = self.settings.daily_ideas_count
        if count <= 0:
            return []
        async with self._sf() as session:
            titles = await repo.recent_idea_titles(session)
            notes = await repo.recent_research_notes(session, limit=5)
        notes_text = "\n\n".join(f"[{n.created_at:%Y-%m-%d}] {n.content}" for n in notes)
        today = datetime.now(ZoneInfo(self.settings.timezone)).date()

        result = await self.scout.run(count=count, previous_titles=titles, notes=notes_text, today=today)
        sources_json = json.dumps([asdict(s) for s in result.sources], ensure_ascii=False)

        created: list[ChannelIdea] = []
        for idea in result.data.ideas:
            details = idea.model_dump_json()
            try:
                sp = await self.prompt_engineer.run(
                    user_id=SYSTEM_USER_ID,
                    idea_data=details,
                    analysis_json="Manbalar: " + sources_json,
                )
            except Exception:  # noqa: BLE001 - bitta g'oya xatosi boshqalarini to'xtatmasin
                logger.exception("idea_prompt_failed")
                continue
            async with self._sf() as session:
                row = ChannelIdea(
                    title=idea.title[:200],
                    post_text=render_channel_post(idea, result.web_used),
                    details=details,
                    prompt_text=sp.prompt,
                    sources=sources_json,
                    web_researched=result.web_used,
                    status=IdeaStatus.DRAFT,
                )
                session.add(row)
                await session.commit()
                created.append(row)

        async with self._sf() as session:
            await repo.add_research_note(
                session,
                f"Kunlik izlanish {today.isoformat()}",
                result.data.market_notes,
                sources_json,
                result.web_used,
            )
            await session.commit()
        logger.info("daily_research_done ideas=%d web=%s", len(created), result.web_used)
        return created

    async def publish(self, bot: Bot, idea_id: int, bot_username: str) -> ChannelIdea | None:
        channel_id = self.settings.ideas_channel_id
        if not channel_id:
            raise RuntimeError("IDEAS_CHANNEL_ID sozlanmagan")
        async with self._sf() as session:
            idea = await repo.get_idea(session, idea_id)
            if idea is None or idea.status == IdeaStatus.PUBLISHED:
                return None
            kb = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="🤖 SUPER PROMPT ni olish", url=idea_deep_link(bot_username, idea.id)
                        )
                    ]
                ]
            )
            msg = await bot.send_message(channel_id, idea.post_text, reply_markup=kb)
            idea.status = IdeaStatus.PUBLISHED
            idea.channel_message_id = msg.message_id
            idea.published_at = datetime.now(UTC)
            await session.commit()
            return idea

    async def reject(self, idea_id: int) -> bool:
        async with self._sf() as session:
            idea = await repo.get_idea(session, idea_id)
            if idea is None or idea.status != IdeaStatus.DRAFT:
                return False
            idea.status = IdeaStatus.REJECTED
            await session.commit()
            return True

    async def get_prompt_for_request(self, idea_id: int) -> ChannelIdea | None:
        """Faqat kanalga joylangan g'oyalar promptini beradi."""
        async with self._sf() as session:
            idea = await repo.get_idea(session, idea_id)
            if idea is None or idea.status != IdeaStatus.PUBLISHED:
                return None
            idea.prompt_requests += 1
            await session.commit()
            return idea
