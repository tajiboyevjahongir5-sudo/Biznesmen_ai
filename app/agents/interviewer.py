"""Business Interviewer agenti — foydalanuvchi bilan tabiiy suhbat orqali g'oyani o'rganadi."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

from app.agents.base import load_prompt
from app.core.security import wrap_untrusted
from app.services.ai_service import AIService

AGENT_NAME = "interviewer"


class Turn(BaseModel):
    role: Literal["assistant", "user"]
    text: str


class InterviewStep(BaseModel):
    """Interviewer agentining strukturali javobi."""

    intent: Literal["has_idea", "looking_for_idea", "off_topic"]
    reply: str = Field(min_length=3, max_length=1500)
    summary: str = Field(max_length=1500)
    industry: str = Field(max_length=120)
    location: str = Field(max_length=120)
    known_facts: list[str]
    open_questions: list[str]
    ready_for_analysis: bool

    @field_validator("known_facts", "open_questions")
    @classmethod
    def _trim_items(cls, items: list[str]) -> list[str]:
        return [item.strip()[:300] for item in items if item.strip()][:8]


def format_conversation(turns: list[Turn]) -> str:
    names = {"assistant": "Maslahatchi", "user": "Foydalanuvchi"}
    return "\n\n".join(f"{names[t.role]}: {t.text}" for t in turns)


def build_input(idea: str, turns: list[Turn], questions_asked: int, min_q: int, max_q: int) -> str:
    """Agentga yuboriladigan qism: barcha foydalanuvchi matnlari ishonchsiz blok ichida."""
    parts = [wrap_untrusted(idea, "user_idea")]
    if turns:
        parts.append(wrap_untrusted(format_conversation(turns), "conversation"))
    rule = f"Hozirgacha berilgan savollar: {questions_asked}. "
    if questions_asked < min_q:
        rule += f"Kamida {min_q} ta savol berilmaguncha ready_for_analysis=false bo'lsin."
    elif questions_asked >= max_q:
        rule += "Savollar limiti tugadi: ready_for_analysis=true qil va yakunlovchi xabar yoz."
    else:
        rule += "Ma'lumot yetarli bo'lsa ready_for_analysis=true qilishing mumkin."
    parts.append(rule)
    return "\n\n".join(parts)


class InterviewerAgent:
    def __init__(self, ai: AIService, prompt: str | None = None) -> None:
        self._ai = ai
        self._prompt = prompt or load_prompt(AGENT_NAME)

    async def run(
        self,
        *,
        user_id: int,
        idea: str,
        turns: list[Turn],
        questions_asked: int,
        min_questions: int,
        max_questions: int,
    ) -> InterviewStep:
        step = await self._ai.structured(
            user_id=user_id,
            agent=AGENT_NAME,
            instructions=self._prompt,
            user_input=build_input(idea, turns, questions_asked, min_questions, max_questions),
            schema=InterviewStep,
            max_output_tokens=1200,
        )
        # Limitlarni kod darajasida ham majburlaymiz (AI ga to'liq ishonmaymiz).
        if questions_asked >= max_questions and step.intent == "has_idea":
            step.ready_for_analysis = True
        elif questions_asked < min_questions:
            step.ready_for_analysis = False
        return step
