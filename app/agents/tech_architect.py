"""Tech Architect — dasturiy mahsulotlar uchun TEXNIK PROMPT (kod, xavfsizlik, UI/UX) yozadi.

Biznes SUPER PROMPT strategiya va moliyani yoritadi. G'oya bot, ilova, sayt yoki platforma
bo'lsa, bu agent AI dasturchi agentlariga (Claude Code, Cursor, Copilot va h.k.) beriladigan
batafsil texnik topshiriq yozadi.

Bitta katta javobda model bo'limlarni qisqartirib yuboradi, shuning uchun hujjat 4 qismda
yoziladi: avval asos (mahsulot, stek, arxitektura), keyin unga tayangan holda xavfsizlik,
dizayn va yetkazib berish qismlari parallel. Har qismda aniq "kamida N ta" talablari bor.
"""

from __future__ import annotations

import asyncio
import re
from typing import TypeVar

from pydantic import BaseModel, Field

from app.agents.base import load_prompt, load_text
from app.core.security import wrap_untrusted
from app.services.ai_service import AIService

AGENT_NAME = "tech_architect"
CORE_PART = "tech_part_core"
DETAIL_PARTS = ("tech_part_security", "tech_part_design", "tech_part_delivery")

# Har qism uchun alohida chegara: uzun javob + modelning "o'ylash" tokenlari.
MAX_OUTPUT_TOKENS = 16000
TIMEOUT_SECONDS = 180.0

T = TypeVar("T", bound=BaseModel)


class TechBlueprint(BaseModel):
    """1-qism: mahsulot, funksiyalar, stek va arxitektura (bo'limlar 1–5)."""

    title: str = Field(max_length=200)
    how_to_use: str = Field(max_length=800)
    markdown: str = Field(min_length=2500, max_length=40000)


class TechSection(BaseModel):
    """Keyingi qismlardan biri (xavfsizlik / dizayn / yetkazib berish)."""

    markdown: str = Field(min_length=2000, max_length=40000)


class TechPrompt(BaseModel):
    title: str = Field(max_length=200)
    prompt: str = Field(min_length=1500, max_length=160000)
    how_to_use: str = Field(max_length=800)


_HEADING_RE = re.compile(r"^## +(.+?)\s*$", re.M)


def assemble(parts: list[str]) -> str:
    """Qismlarni bitta hujjatga yig'adi va boshiga mundarija qo'yadi."""
    body = "\n\n---\n\n".join(p.strip() for p in parts if p.strip())
    headings = _HEADING_RE.findall(body)
    if len(headings) < 3:
        return body
    toc = "\n".join(f"- {h}" for h in headings)
    return f"## MUNDARIJA\n{toc}\n\n---\n\n{body}"


class TechArchitectAgent:
    def __init__(self, ai: AIService, prompt: str | None = None, parts: dict[str, str] | None = None) -> None:
        self._ai = ai
        self._prompt = prompt or load_prompt(AGENT_NAME)
        self._parts = parts or {name: load_text(name) for name in (CORE_PART, *DETAIL_PARTS)}

    async def run(
        self, *, user_id: int, idea_data: str, analysis_json: str, product_type: str = ""
    ) -> TechPrompt:
        context = [
            wrap_untrusted(idea_data, "idea_data"),
            wrap_untrusted(analysis_json, "analysis"),
        ]
        if product_type:
            context.append(wrap_untrusted(product_type, "product_type"))

        core = await self._call(user_id, CORE_PART, context, TechBlueprint)
        detail_context = [*context, wrap_untrusted(core.markdown, "blueprint")]
        try:
            async with asyncio.TaskGroup() as tg:
                tasks = [
                    tg.create_task(self._call(user_id, name, detail_context, TechSection))
                    for name in DETAIL_PARTS
                ]
        except ExceptionGroup as group:  # bitta qism xatosi — chaqiruvchiga oddiy xato sifatida
            raise group.exceptions[0] from group
        details = [task.result().markdown for task in tasks]
        return TechPrompt(
            title=core.title,
            prompt=assemble([core.markdown, *details]),
            how_to_use=core.how_to_use,
        )

    async def _call(self, user_id: int, part: str, context: list[str], schema: type[T]) -> T:
        return await self._ai.structured(
            user_id=user_id,
            agent=AGENT_NAME,
            instructions=f"{self._prompt}\n\n{self._parts[part]}",
            user_input="\n\n".join([*context, "Hujjatning faqat ko'rsatilgan qismini yoz."]),
            schema=schema,
            max_output_tokens=MAX_OUTPUT_TOKENS,
            timeout=TIMEOUT_SECONDS,
        )
