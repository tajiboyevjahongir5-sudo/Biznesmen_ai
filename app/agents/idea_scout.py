"""Idea Scout — kunlik izlanish: talabi yuqori, raqobati past g'oyalarni topadi."""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, Field

from app.agents.analyst import Claim, Level
from app.agents.base import load_prompt
from app.core.security import wrap_untrusted
from app.services.ai_service import AIResult, AIService

AGENT_NAME = "idea_scout"
SYSTEM_USER_ID = 0  # tizim chaqiruvlari uchun (foydalanuvchi limiti o'rniga global limit)


class ScoutIdea(BaseModel):
    title: str = Field(max_length=120)
    one_liner: str = Field(max_length=400)
    target_customers: str = Field(max_length=500)
    why_demand_high: str = Field(max_length=800)
    why_competition_low: str = Field(max_length=800)
    demand_level: Level
    competition_level: Level
    startup_cost_estimate: str = Field(max_length=300)
    first_steps: list[str]
    risks: list[str]
    evidence: list[Claim]
    image_query: str = Field(
        default="",
        max_length=100,
        description="Stok-foto qidiruvi uchun 2-4 ta INGLIZCHA so'z, masalan: healthy breakfast delivery",
    )


class ScoutReport(BaseModel):
    market_notes: str = Field(max_length=3000)
    ideas: list[ScoutIdea]


class IdeaScoutAgent:
    def __init__(self, ai: AIService, prompt: str | None = None) -> None:
        self._ai = ai
        self._prompt = prompt or load_prompt(AGENT_NAME)

    async def run(
        self, *, count: int, previous_titles: list[str], notes: str, today: date
    ) -> AIResult[ScoutReport]:
        web = self._ai.web_search_available
        user_input = "\n\n".join(
            [
                f"Bugungi sana: {today.isoformat()}. {count} ta yangi g'oya top.",
                wrap_untrusted("\n".join(previous_titles) or "(yo'q)", "previous_ideas"),
                wrap_untrusted(notes or "(yo'q)", "research_notes"),
                "Internet qidiruvi: " + ("YOQILGAN." if web else "YO'Q."),
            ]
        )
        result = await self._ai.structured_with_sources(
            user_id=SYSTEM_USER_ID,
            agent=AGENT_NAME,
            instructions=self._prompt,
            user_input=user_input,
            schema=ScoutReport,
            max_output_tokens=6000,
            web_search=web,
        )
        # Takrorlanmaslikni kod darajasida ham tekshiramiz.
        seen = {t.strip().lower() for t in previous_titles}
        unique: list[ScoutIdea] = []
        for idea in result.data.ideas:
            key = idea.title.strip().lower()
            if key and key not in seen:
                seen.add(key)
                unique.append(idea)
        result.data.ideas = unique[:count]
        return result
