"""Master Prompt Engineer — yakuniy SUPER PROMPT yaratadi."""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.agents.base import load_prompt
from app.core.security import wrap_untrusted
from app.services.ai_service import AIService

AGENT_NAME = "prompt_engineer"


class SuperPrompt(BaseModel):
    title: str = Field(max_length=200)
    prompt: str = Field(min_length=200, max_length=12000)
    how_to_use: str = Field(max_length=600)


class PromptEngineerAgent:
    def __init__(self, ai: AIService, prompt: str | None = None) -> None:
        self._ai = ai
        self._prompt = prompt or load_prompt(AGENT_NAME)

    async def run(self, *, user_id: int, idea_data: str, analysis_json: str) -> SuperPrompt:
        user_input = "\n\n".join(
            [
                wrap_untrusted(idea_data, "idea_data"),
                wrap_untrusted(analysis_json, "analysis"),
                "Shu ma'lumotlar asosida SUPER PROMPT yoz.",
            ]
        )
        return await self._ai.structured(
            user_id=user_id,
            agent=AGENT_NAME,
            instructions=self._prompt,
            user_input=user_input,
            schema=SuperPrompt,
            max_output_tokens=5000,
        )
