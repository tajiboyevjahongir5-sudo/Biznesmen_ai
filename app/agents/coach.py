"""Kuzatuv (follow-up) agenti: tahlildan keyin foydalanuvchi natijalariga qarab rejani yangilaydi."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

from app.agents.base import load_prompt
from app.agents.interviewer import ALLOWED_REACTIONS, DEFAULT_REACTION, Turn, format_conversation
from app.core.security import wrap_untrusted
from app.services.ai_service import AIService

AGENT_NAME = "coach"


class CoachStep(BaseModel):
    intent: Literal["progress_update", "question", "new_idea", "stop"]
    reply: str = Field(min_length=3, max_length=2500)
    updated_next_steps: list[str] = Field(default_factory=list)
    reaction: str = Field(default=DEFAULT_REACTION, max_length=16)

    @field_validator("reaction", mode="before")
    @classmethod
    def _valid_reaction(cls, value: object) -> str:
        value = str(value or "").strip().replace("️", "")
        return value if value in ALLOWED_REACTIONS else DEFAULT_REACTION

    @field_validator("updated_next_steps")
    @classmethod
    def _trim(cls, items: list[str]) -> list[str]:
        return [x.strip()[:300] for x in items if x.strip()][:7]


class CoachAgent:
    def __init__(self, ai: AIService, prompt: str | None = None) -> None:
        self._ai = ai
        self._prompt = prompt or load_prompt(AGENT_NAME)

    async def run(
        self, *, user_id: int, idea: str, analysis_summary: str, turns: list[Turn], user_name: str = ""
    ) -> CoachStep:
        parts = [
            wrap_untrusted(idea, "user_idea"),
            wrap_untrusted(analysis_summary or "(tahlil topilmadi)", "previous_analysis"),
            wrap_untrusted(format_conversation(turns), "conversation"),
        ]
        if user_name:
            parts.append(wrap_untrusted(user_name[:40], "user_name"))
        return await self._ai.structured(
            user_id=user_id,
            agent=AGENT_NAME,
            instructions=self._prompt,
            user_input="\n\n".join(parts),
            schema=CoachStep,
            max_output_tokens=4000,
        )
