"""Tanqidiy tahlilchi: optimist + skeptik bahsi va bosh strateg qarori (bitta chaqiruvda).

To'liq ko'p bosqichli debate engine va Python orqali moliyaviy model keyingi
bosqichlarda alohida agentlarga ajratiladi.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from app.agents.base import load_prompt
from app.core.finance import FinanceInputs
from app.core.security import wrap_untrusted
from app.services.ai_service import AIResult, AIService

AGENT_NAME = "analyst"

ClaimKind = Literal["fakt", "taxmin", "prognoz"]
Level = Literal["past", "o'rta", "yuqori", "aniqlanmagan"]
Verdict = Literal[
    "Boshlash mumkin",
    "Avval kichik test qilish kerak",
    "G'oyani o'zgartirish kerak",
    "Qo'shimcha ma'lumot kerak",
    "Hozircha boshlash tavsiya etilmaydi",
]


class Claim(BaseModel):
    text: str = Field(max_length=500)
    kind: ClaimKind
    source_url: str = Field(default="", max_length=500)


class Competitor(BaseModel):
    name: str = Field(max_length=150)
    note: str = Field(max_length=400)
    kind: ClaimKind


class FailureRisk(BaseModel):
    risk: str = Field(max_length=300)
    why: str = Field(max_length=600)
    probability: Level
    prevention: str = Field(max_length=500)


class DebatePoint(BaseModel):
    optimist: str = Field(max_length=500)
    skeptic: str = Field(max_length=500)
    resolution: str = Field(max_length=500)


class MarketAnalysis(BaseModel):
    summary: str = Field(max_length=1200)
    demand_level: Level
    demand_evidence: list[Claim]
    competition_level: Level
    competitors: list[Competitor]
    differentiation: str = Field(max_length=800)
    strengths: list[Claim]
    failure_risks: list[FailureRisk]
    wrong_assumptions: list[str]
    debate: list[DebatePoint]
    must_verify: list[str]
    mvp_test: str = Field(max_length=1000)
    verdict: Verdict
    verdict_reasons: list[str]
    next_steps: list[str]
    confidence: Level
    finance: FinanceInputs = Field(default_factory=FinanceInputs)


def build_input(idea: str, conversation: str, notes: str, web: bool) -> str:
    parts = [
        wrap_untrusted(idea, "user_idea"),
        wrap_untrusted(conversation or "(suhbat yo'q)", "conversation"),
    ]
    if notes:
        parts.append(wrap_untrusted(notes, "research_notes"))
    parts.append(
        "Internet qidiruvi: "
        + (
            "YOQILGAN — <web_research> blokidagi (yoki web_search vositasi) topilgan ma'lumotlardan foydalan."
            if web
            else "YO'Q — faqat taxminlar bilan ishla."
        )
    )
    return "\n\n".join(parts)


class AnalystAgent:
    def __init__(self, ai: AIService, prompt: str | None = None) -> None:
        self._ai = ai
        self._prompt = prompt or load_prompt(AGENT_NAME)

    async def run(
        self, *, user_id: int, idea: str, conversation: str, notes: str = ""
    ) -> AIResult[MarketAnalysis]:
        web = self._ai.web_search_available
        return await self._ai.structured_with_sources(
            user_id=user_id,
            agent=AGENT_NAME,
            instructions=self._prompt,
            user_input=build_input(idea, conversation, notes, web),
            schema=MarketAnalysis,
            max_output_tokens=8000,
            web_search=web,
        )
