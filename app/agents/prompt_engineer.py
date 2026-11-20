"""Master Prompt Engineer — yakuniy SUPER PROMPT yaratadi."""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.agents.base import load_prompt
from app.core.security import wrap_untrusted
from app.services.ai_service import AIService

AGENT_NAME = "prompt_engineer"


class SuperPrompt(BaseModel):
    title: str = Field(max_length=200)
    prompt: str = Field(min_length=200, max_length=30000)
    how_to_use: str = Field(max_length=600)
    project_name: str = Field(
        default="",
        max_length=80,
        description="Loyihaning qisqa nomi, 2-6 so'z (masalan: 'Xorazm maklersiz uy-joy boti').",
    )
    software_product: bool = Field(
        default=False,
        description="true — mahsulotning o'zi dastur bo'lsa: Telegram bot, Web App, mobil ilova, "
        "veb-sayt, onlayn do'kon, SaaS, platforma, marketpleys. Oddiy oflayn biznes "
        "(kafe, do'kon, xizmat) faqat Instagram/Telegram kanal orqali sotsa — false.",
    )
    product_type: str = Field(
        default="",
        max_length=120,
        description="software_product true bo'lsa — mahsulot turi va platformasi, masalan: "
        "'Telegram Web App + bot', 'mobil ilova (Android/iOS)', 'veb-sayt'. Aks holda bo'sh.",
    )


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
            max_output_tokens=12000,
            timeout=150.0,
        )
