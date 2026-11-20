"""Tech Architect — dasturiy mahsulotlar uchun TEXNIK PROMPT (kod, xavfsizlik, UI/UX) yozadi.

Biznes SUPER PROMPT strategiya va moliyani yoritadi. G'oya bot, ilova, sayt yoki platforma
bo'lsa, bu agent AI dasturchi agentlariga (Claude Code, Cursor, Copilot va h.k.) beriladigan
batafsil texnik topshiriq yozadi: arxitektura, ma'lumotlar modeli, xavfsizlik, dizayn tizimi,
animatsiya, qulaylik, test va deploy.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.agents.base import load_prompt
from app.core.security import wrap_untrusted
from app.services.ai_service import AIService

AGENT_NAME = "tech_architect"

# Uzun javob: model ko'p token yozadi, shuning uchun alohida chegara va vaqt.
MAX_OUTPUT_TOKENS = 16000
TIMEOUT_SECONDS = 180.0


class TechPrompt(BaseModel):
    title: str = Field(max_length=200)
    prompt: str = Field(min_length=1500, max_length=40000)
    how_to_use: str = Field(max_length=800)


class TechArchitectAgent:
    def __init__(self, ai: AIService, prompt: str | None = None) -> None:
        self._ai = ai
        self._prompt = prompt or load_prompt(AGENT_NAME)

    async def run(
        self, *, user_id: int, idea_data: str, analysis_json: str, product_type: str = ""
    ) -> TechPrompt:
        parts = [
            wrap_untrusted(idea_data, "idea_data"),
            wrap_untrusted(analysis_json, "analysis"),
        ]
        if product_type:
            parts.append(wrap_untrusted(product_type, "product_type"))
        parts.append("Shu dasturiy mahsulot uchun TEXNIK PROMPT yoz.")
        return await self._ai.structured(
            user_id=user_id,
            agent=AGENT_NAME,
            instructions=self._prompt,
            user_input="\n\n".join(parts),
            schema=TechPrompt,
            max_output_tokens=MAX_OUTPUT_TOKENS,
            timeout=TIMEOUT_SECONDS,
        )
