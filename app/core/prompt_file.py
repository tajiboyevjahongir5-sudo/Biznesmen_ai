"""Foydalanuvchiga beriladigan bitta to'liq prompt fayli (.md).

SUPER PROMPT (biznes) va, bo'lsa, TEXNIK PROMPT (dasturlash) bitta hujjatga yig'iladi:
foydalanuvchi faylni bir marta AI ga yuklaydi — bo'laklarni ketma-ket nusxalash shart emas.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from app.core.text import render_prompt_caption, render_prompt_fallback


@dataclass
class OutputDocument:
    """Xabarlardan keyin yuboriladigan fayl. Yuborib bo'lmasa — `fallback` xabarlari yuboriladi."""

    filename: str
    data: bytes
    caption: str = ""
    fallback: list[str] = field(default_factory=list)


def prompt_filename(key: int | str) -> str:
    return f"toliq-prompt-{key}.md"


def _one_line(text: str) -> str:
    return " ".join((text or "").split())


def build_prompt_markdown(
    *,
    title: str,
    prompt: str,
    tech_prompt: str = "",
    how_to_use: str = "",
    tech_how_to_use: str = "",
    source: str = "",
) -> str:
    """Bitta to'liq prompt hujjati: foydalanish yo'riqnomasi, so'ng biznes va texnik qismlar."""
    title = _one_line(title) or "Biznes loyiha"
    has_tech = bool(tech_prompt.strip())
    meta = " · ".join(x for x in ("BIZNES MIND AI", source, datetime.now(UTC).strftime("%d.%m.%Y")) if x)
    contents = "💼 biznes strategiya" + (" + 🛠 texnik topshiriq (dasturlash)" if has_tech else "")
    tool = "; dasturlash uchun — Claude Code yoki Cursor" if has_tech else ""

    lines = [
        f"# {title}",
        "",
        f"> {meta}  ",
        f"> To'liq prompt: {contents}",
        "",
        "## SIZ UCHUN: QANDAY ISHLATILADI",
        "",
        f"1. Shu faylni ChatGPT, Gemini yoki Claude ga yuklang{tool}.",
        "2. «Shu fayldagi topshiriq bo'yicha ishla» deb yozing.",
        "3. AI bergan savollarga javob bering va natijani bosqichma-bosqich tekshiring.",
    ]
    for note in (how_to_use, tech_how_to_use):
        if note.strip():
            lines += ["", f"> {_one_line(note)}"]

    if has_tech:
        lines += [
            "",
            "## AI UCHUN KO'RSATMA",
            "",
            "Bu fayl — bitta loyiha bo'yicha to'liq topshiriq. U ikki qismdan iborat va ikkalasi "
            "bir-biriga mos:",
            "",
            "- **1-QISM. SUPER PROMPT** — biznesni ishga tushirish: strategiya, marketing, moliya, "
            "huquqiy masalalar.",
            "- **2-QISM. TEXNIK PROMPT** — mahsulotni dasturlash: arxitektura, xavfsizlik, UI/UX, "
            "animatsiya, test, deploy.",
            "",
            "Foydalanuvchi boshqacha so'ramasa, 1-qismdan boshla. Kod yozish yoki mahsulotni qurish "
            "so'ralsa — 2-qismga to'liq amal qil, biznes talablarini esa 1-qismdan ol.",
            "",
            "---",
            "",
            "# 1-QISM. SUPER PROMPT — biznes strategiya",
            "",
            prompt.strip(),
            "",
            "---",
            "",
            "# 2-QISM. TEXNIK PROMPT — dasturchi AI uchun",
            "",
            tech_prompt.strip(),
        ]
    else:
        lines += ["", "---", "", "# SUPER PROMPT", "", prompt.strip()]
    return "\n".join(lines) + "\n"


def prompt_document(
    key: int | str,
    *,
    title: str,
    prompt: str,
    tech_prompt: str = "",
    how_to_use: str = "",
    tech_how_to_use: str = "",
    source: str = "",
) -> OutputDocument:
    """Foydalanuvchiga yuboriladigan yagona prompt fayli (izohi va zaxira matni bilan)."""
    text = build_prompt_markdown(
        title=title,
        prompt=prompt,
        tech_prompt=tech_prompt,
        how_to_use=how_to_use,
        tech_how_to_use=tech_how_to_use,
        source=source,
    )
    return OutputDocument(
        filename=prompt_filename(key),
        data=text.encode("utf-8"),
        caption=render_prompt_caption(title, how_to_use, has_tech=bool(tech_prompt.strip())),
        fallback=render_prompt_fallback(title, text),
    )
