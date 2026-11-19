"""Telegram uchun matnni formatlash va bo'laklarga ajratish (HTML parse mode)."""

from __future__ import annotations

from html import escape

from app.agents.analyst import MarketAnalysis
from app.agents.idea_scout import ScoutIdea
from app.services.ai_service import Source

TELEGRAM_LIMIT = 4096
SAFE_LIMIT = 3800

KIND_LABEL = {"fakt": "Fakt", "taxmin": "Taxmin", "prognoz": "Prognoz"}


def e(text: str) -> str:
    """AI yoki foydalanuvchi matnini HTML uchun xavfsiz qiladi."""
    return escape(text or "", quote=False)


def split_message(text: str, limit: int = SAFE_LIMIT) -> list[str]:
    """Uzun matnni paragraf/qator chegarasida bo'laklarga ajratadi.

    HTML teglar bitta qator ichida ochilib yopilishi kerak (bu modul shunday yozadi),
    shuning uchun qator chegarasida bo'lish teglarni buzmaydi.
    """
    text = text.strip()
    if len(text) <= limit:
        return [text] if text else []
    parts: list[str] = []
    current = ""
    for line in text.split("\n"):
        while len(line) > limit:  # juda uzun bitta qator
            if current:
                parts.append(current.rstrip())
                current = ""
            parts.append(line[:limit])
            line = line[limit:]
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit:
            parts.append(current.rstrip())
            current = line
        else:
            current = candidate
    if current.strip():
        parts.append(current.rstrip())
    return [p for p in parts if p.strip()]


def format_money(amount: int) -> str:
    return f"{amount:,}".replace(",", " ")


def _claims(items, title: str) -> list[str]:
    if not items:
        return []
    lines = [f"<b>{title}</b>"]
    for c in items:
        src = f' — <a href="{escape(c.source_url)}">manba</a>' if c.source_url else ""
        lines.append(f"• [{KIND_LABEL[c.kind]}] {e(c.text)}{src}")
    return lines


def render_analysis(a: MarketAnalysis, sources: list[Source], web_used: bool) -> list[str]:
    """Tahlilni bir nechta mantiqiy xabarga bo'lib qaytaradi."""
    msgs: list[str] = []

    head = [
        "<b>BIZNES MIND AI — TAHLIL NATIJASI</b>",
        "",
        e(a.summary),
        "",
        f"<b>Talab:</b> {e(a.demand_level)}    <b>Raqobat:</b> {e(a.competition_level)}",
        f"<b>Ishonch darajasi:</b> {e(a.confidence)}",
    ]
    if not web_used:
        head += [
            "",
            "<i>Eslatma: bu tahlil internet qidiruvisiz bajarildi. Bozor bo'yicha "
            "da'volar taxmin sifatida berilgan va ularni o'zingiz tekshirishingiz kerak.</i>",
        ]
    msgs.append("\n".join(head))

    market = _claims(a.demand_evidence, "Talab bo'yicha dalillar")
    if a.competitors:
        market += ["", "<b>Raqobatchilar</b>"]
        market += [f"• {e(c.name)} [{KIND_LABEL[c.kind]}] — {e(c.note)}" for c in a.competitors]
    if a.differentiation:
        market += ["", "<b>Qanday ajralib turish mumkin</b>", e(a.differentiation)]
    market += [""] + _claims(a.strengths, "Kuchli tomonlar")
    msgs.append("\n".join(market).strip())

    risks = ["<b>Nima uchun muvaffaqiyatsiz bo'lishi mumkin?</b>"]
    for r in a.failure_risks:
        risks += [
            "",
            f"<b>{e(r.risk)}</b> (ehtimol: {e(r.probability)})",
            f"Sabab: {e(r.why)}",
            f"Oldini olish: {e(r.prevention)}",
        ]
    if a.wrong_assumptions:
        risks += ["", "<b>Ehtimoliy noto'g'ri taxminlar</b>"] + [f"• {e(x)}" for x in a.wrong_assumptions]
    msgs.append("\n".join(risks))

    if a.debate:
        debate = ["<b>Agentlar bahsi</b>"]
        for d in a.debate:
            debate += [
                "",
                f"<b>Optimist:</b> {e(d.optimist)}",
                f"<b>Skeptik:</b> {e(d.skeptic)}",
                f"<b>Xulosa:</b> {e(d.resolution)}",
            ]
        msgs.append("\n".join(debate))

    final = [f"<b>YAKUNIY QAROR: {e(a.verdict)}</b>"]
    final += [f"• {e(x)}" for x in a.verdict_reasons]
    if a.mvp_test:
        final += ["", "<b>Kichik test (MVP)</b>", e(a.mvp_test)]
    if a.must_verify:
        final += ["", "<b>Albatta tekshiring</b>"] + [f"• {e(x)}" for x in a.must_verify]
    if a.next_steps:
        final += ["", "<b>Keyingi qadamlar</b>"] + [f"{i}. {e(x)}" for i, x in enumerate(a.next_steps, 1)]
    if sources:
        final += ["", "<b>Internet manbalari</b>"]
        final += [f'• <a href="{escape(s.url)}">{e(s.title)}</a>' for s in sources]
    final += ["", "<i>Bu tahlil maslahat xarakterida. Hech qanday natija kafolatlanmaydi.</i>"]
    msgs.append("\n".join(final))

    out: list[str] = []
    for m in msgs:
        out.extend(split_message(m))
    return out


def render_prompt(title: str, prompt: str, how_to_use: str) -> list[str]:
    header = (
        f"<b>SUPER PROMPT: {e(title)}</b>\n\n{e(how_to_use)}\n\n"
        "Quyidagi matnni to'liq nusxa olib, istalgan AI ga yuboring:"
    )
    # HTML escape matnni uzaytirishi mumkin (& -> &amp;), shuning uchun zaxira qoldiramiz.
    body = split_message(prompt, limit=3000)
    return [header] + [f"<pre>{e(chunk)}</pre>" for chunk in body]


def render_channel_post(idea: ScoutIdea, web_used: bool) -> str:
    lines = [
        f"<b>{e(idea.title)}</b>",
        "",
        e(idea.one_liner),
        "",
        f"<b>Talab:</b> {e(idea.demand_level)}   <b>Raqobat:</b> {e(idea.competition_level)}",
        f"<b>Kim uchun:</b> {e(idea.target_customers)}",
        "",
        f"<b>Nega talab bor:</b> {e(idea.why_demand_high)}",
        "",
        f"<b>Nega raqobat kam:</b> {e(idea.why_competition_low)}",
        "",
        f"<b>Boshlang'ich xarajat (taxmin):</b> {e(idea.startup_cost_estimate)}",
    ]
    if idea.risks:
        lines += ["", "<b>Xavflar:</b>"] + [f"• {e(r)}" for r in idea.risks[:4]]
    if not web_used:
        lines += ["", "<i>Internet ma'lumotisiz tayyorlangan, raqamlar taxminiy.</i>"]
    lines += ["", "To'liq SUPER PROMPT ni olish uchun pastdagi tugmani bosing."]
    text = "\n".join(lines)
    return text[: TELEGRAM_LIMIT - 100]
