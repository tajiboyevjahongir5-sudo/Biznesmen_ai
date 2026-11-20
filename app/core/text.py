"""Telegram uchun matnni formatlash va bo'laklarga ajratish (HTML parse mode).

Barcha xabarlar bir uslubda: emoji + qalin sarlavha, ajratuvchi chiziq, qisqa bo'limlar,
uzun qismlar yig'iladigan iqtibos (`<blockquote expandable>`) ichida.
AI va foydalanuvchi matni har doim `e()` orqali escape qilinadi.
"""

from __future__ import annotations

import re
from html import escape

from app.agents.analyst import MarketAnalysis
from app.agents.idea_scout import ScoutIdea
from app.services.ai_service import Source

TELEGRAM_LIMIT = 4096
SAFE_LIMIT = 3800
CAPTION_LIMIT = 1024  # rasm ostidagi matn limiti

SEP = "━━━━━━━━━━━━━━━━"

KIND_LABEL = {"fakt": "Fakt", "taxmin": "Taxmin", "prognoz": "Prognoz"}
KIND_ICON = {"fakt": "✅", "taxmin": "💭", "prognoz": "🔮"}
_LEVEL_BARS = {"past": "▰▱▱", "o'rta": "▰▰▱", "yuqori": "▰▰▰"}
# Yashil — yaxshi. Talab yuqori bo'lsa yaxshi, raqobat yoki xavf yuqori bo'lsa yomon.
_GOOD_HIGH = {"yuqori": "🟢", "o'rta": "🟡", "past": "🔴"}
_GOOD_LOW = {"yuqori": "🔴", "o'rta": "🟡", "past": "🟢"}
VERDICT_ICON = {
    "Boshlash mumkin": "🟢",
    "Avval kichik test qilish kerak": "🧪",
    "G'oyani o'zgartirish kerak": "🔄",
    "Qo'shimcha ma'lumot kerak": "🔍",
    "Hozircha boshlash tavsiya etilmaydi": "⛔",
}
_KEYCAPS = ("1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟")


def e(text: str) -> str:
    """AI yoki foydalanuvchi matnini HTML uchun xavfsiz qiladi."""
    return escape(text or "", quote=False)


def heading(emoji: str, title: str) -> str:
    """Bo'lim sarlavhasi: emoji, qalin matn va ajratuvchi chiziq."""
    return f"{emoji} <b>{title}</b>\n{SEP}"


def num(i: int) -> str:
    return _KEYCAPS[i - 1] if 1 <= i <= len(_KEYCAPS) else f"{i}."


def quote(text: str, expandable: bool = False) -> str:
    tag = "<blockquote expandable>" if expandable else "<blockquote>"
    return f"{tag}{text}</blockquote>"


def level_line(icon: str, label: str, level: str, good_high: bool = True) -> str:
    dot = (_GOOD_HIGH if good_high else _GOOD_LOW).get(level, "⚪")
    return f"{icon} {label}: {_LEVEL_BARS.get(level, '▱▱▱')} <b>{e(level)}</b> {dot}"


def _safe_url(url: str) -> str:
    """Faqat http(s) havolalar (Telegram boshqalarini rad etadi, xavfli sxemalar o'tmaydi)."""
    url = (url or "").strip()
    return escape(url, quote=True) if url.startswith(("https://", "http://")) else ""


_BOLD_RE = re.compile(r"\*\*([^\n*]{1,200}?)\*\*")


def format_reply(text: str) -> str:
    """AI suhbat javobini xavfsiz HTML ga aylantiradi: faqat **qalin** ruxsat etiladi."""
    safe = _BOLD_RE.sub(r"<b>\1</b>", e(text.strip()))
    return safe.replace("**", "")


_BQ_OPEN_RE = re.compile(r"<blockquote(?: expandable)?>")
_BQ_CLOSE = "</blockquote>"


def _quote_state(line: str, current: str) -> str:
    """Qator oxirida ochiq qolgan blockquote tegi ("" — ochiq emas)."""
    last_open = None
    for m in _BQ_OPEN_RE.finditer(line):
        last_open = m
    last_close = line.rfind(_BQ_CLOSE)
    if last_open is not None and last_open.start() > last_close:
        return last_open.group(0)
    if last_close != -1:
        return ""
    return current


def split_message(text: str, limit: int = SAFE_LIMIT) -> list[str]:
    """Uzun matnni qator chegarasida bo'laklarga ajratadi.

    Oddiy teglar bitta qator ichida ochilib yopiladi (bu modul shunday yozadi). Faqat
    blockquote bir necha qatorga cho'zilishi mumkin: bo'lak shu iqtibos ichida tugasa, u
    yopiladi va keyingi bo'lakda qayta ochiladi — HTML hech qachon buzilmaydi.
    """
    text = text.strip()
    if len(text) <= limit:
        return [text] if text else []
    budget = limit - len(_BQ_CLOSE) - len("<blockquote expandable>")
    parts: list[str] = []
    current = ""
    open_tag = ""  # `current` oxirida ochiq turgan blockquote

    def flush() -> None:
        body = current.rstrip()
        if body and body != open_tag:
            parts.append(body + (_BQ_CLOSE if open_tag else ""))

    for line in text.split("\n"):
        after = _quote_state(line, open_tag)
        while len(line) > budget:  # juda uzun bitta qator
            flush()
            current = ""
            parts.append(line[:budget])
            line = line[budget:]
        if current in ("", open_tag):
            candidate = current + line
        else:
            candidate = f"{current}\n{line}"
        if len(candidate) > budget:
            flush()
            current = open_tag + line
        else:
            current = candidate
        open_tag = after
    flush()
    return [p for p in parts if p.strip()]


def format_money(amount: int) -> str:
    return f"{amount:,}".replace(",", " ")


def _claims(items, emoji: str, title: str) -> list[str]:
    if not items:
        return []
    lines = ["", f"{emoji} <b>{title}</b>"]
    for c in items:
        url = _safe_url(c.source_url)
        src = f' — <a href="{url}">manba</a>' if url else ""
        lines.append(f"{KIND_ICON[c.kind]} <i>{KIND_LABEL[c.kind]}</i> · {e(c.text)}{src}")
    return lines


def render_analysis(a: MarketAnalysis, sources: list[Source], web_used: bool) -> list[str]:
    """Tahlilni bir nechta mantiqiy xabarga bo'lib qaytaradi."""
    msgs: list[str] = []
    verdict_icon = VERDICT_ICON.get(a.verdict, "🏁")

    head = [
        heading("📊", "TAHLIL NATIJASI"),
        quote(e(a.summary)),
        "",
        level_line("📈", "Talab", a.demand_level),
        level_line("⚔️", "Raqobat", a.competition_level, good_high=False),
        level_line("🎯", "Ishonch", a.confidence),
        "",
        f"{verdict_icon} Qisqa xulosa: <b>{e(a.verdict)}</b>",
    ]
    if not web_used:
        head += [
            "",
            "ℹ️ <i>Bu tahlil internet qidiruvisiz bajarildi: bozor bo'yicha da'volar taxmin "
            "sifatida berilgan, ularni o'zingiz tekshiring.</i>",
        ]
    msgs.append("\n".join(head))

    market = [heading("🔎", "BOZOR VA RAQOBAT")]
    market += _claims(a.demand_evidence, "📌", "Talab bo'yicha dalillar")
    if a.competitors:
        market += ["", "🏢 <b>Raqobatchilar</b>"]
        market += [
            f"• <b>{e(c.name)}</b> — {e(c.note)} <i>({KIND_LABEL[c.kind].lower()})</i>" for c in a.competitors
        ]
    if a.differentiation:
        market += ["", "💎 <b>Qanday ajralib turish mumkin</b>", e(a.differentiation)]
    market += _claims(a.strengths, "💪", "Kuchli tomonlar")
    if len(market) > 1:
        msgs.append("\n".join(market))

    risks = [heading("⚠️", "NIMA UCHUN MUVAFFAQIYATSIZ BO'LISHI MUMKIN?")]
    for i, r in enumerate(a.failure_risks, 1):
        risks += [
            "",
            f"{num(i)} <b>{e(r.risk)}</b>",
            f"{_GOOD_LOW.get(r.probability, '⚪')} Ehtimol: <b>{e(r.probability)}</b>",
            f"🔸 Sabab: {e(r.why)}",
            f"🛡 Oldini olish: {e(r.prevention)}",
        ]
    if a.wrong_assumptions:
        risks += ["", "🤔 <b>Ehtimoliy noto'g'ri taxminlar</b>"] + [f"• {e(x)}" for x in a.wrong_assumptions]
    if len(risks) > 1:
        msgs.append("\n".join(risks))

    if a.debate:
        debate = [heading("🗣", "AGENTLAR BAHSI"), "<i>Optimist va skeptik bir-birini tekshirdi:</i>"]
        for d in a.debate:
            debate += [
                "",
                quote(
                    f"😊 <b>Optimist:</b> {e(d.optimist)}\n"
                    f"🧐 <b>Skeptik:</b> {e(d.skeptic)}\n"
                    f"⚖️ <b>Xulosa:</b> {e(d.resolution)}",
                    expandable=True,
                ),
            ]
        msgs.append("\n".join(debate))

    final = [heading("🏁", "YAKUNIY QAROR"), f"{verdict_icon} <b>{e(a.verdict)}</b>"]
    final += [f"• {e(x)}" for x in a.verdict_reasons]
    if a.mvp_test:
        final += ["", "🔬 <b>Kichik test (MVP)</b>", quote(e(a.mvp_test))]
    if a.must_verify:
        final += ["", "🔍 <b>Albatta tekshiring</b>"] + [f"▫️ {e(x)}" for x in a.must_verify]
    if a.next_steps:
        final += ["", "🚀 <b>Keyingi qadamlar</b>"]
        final += [f"{num(i)} {e(x)}" for i, x in enumerate(a.next_steps, 1)]
    links = [(url, s.title) for s in sources if (url := _safe_url(s.url))]
    if links:
        items = "\n".join(f'• <a href="{url}">{e(title)}</a>' for url, title in links)
        final += ["", "🌐 <b>Internet manbalari</b>", quote(items, expandable=True)]
    final += ["", "⚖️ <i>Bu tahlil maslahat xarakterida. Hech qanday natija kafolatlanmaydi.</i>"]
    msgs.append("\n".join(final))

    out: list[str] = []
    for m in msgs:
        out.extend(split_message(m))
    return out


def render_prompt(title: str, prompt: str, how_to_use: str) -> list[str]:
    body = split_message(prompt, limit=3000)
    header = [heading("🤖", "SUPER PROMPT"), f"📌 <b>{e(title)}</b>"]
    if how_to_use:
        header += ["", e(how_to_use)]
    header += [
        "",
        "📋 <b>Qanday ishlatiladi</b>",
        f"{num(1)} Pastdagi blokni nusxalang (blok ustiga bosing)",
        f"{num(2)} ChatGPT, Gemini yoki Claude kabi istalgan AI ga yuboring",
        f"{num(3)} AI bergan rejani bosqichma-bosqich bajaring",
    ]
    if len(body) > 1:
        header += ["", f"🧩 Prompt <b>{len(body)} qismdan</b> iborat — hammasini ketma-ket yuboring."]
    # HTML escape matnni uzaytirishi mumkin (& -> &amp;), shuning uchun bo'laklar 3000 belgidan.
    blocks = [f'<pre><code class="language-prompt">{e(chunk)}</code></pre>' for chunk in body]
    return ["\n".join(header), *blocks]


def _clip(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip(" ,.;:") + "…"


def render_channel_post(idea: ScoutIdea, web_used: bool) -> str:
    """Kanal uchun to'liq post (rasmsiz). Uzun bo'lsa maydonlar qisqartiriladi — teglar buzilmaydi."""
    for budget in (800, 500, 320):
        lines = [
            f"💡 <b>{e(_clip(idea.title, 120))}</b>",
            SEP,
            f"<i>{e(_clip(idea.one_liner, budget))}</i>",
            "",
            level_line("📈", "Talab", idea.demand_level),
            level_line("⚔️", "Raqobat", idea.competition_level, good_high=False),
            "",
            "🎯 <b>Kim uchun</b>",
            e(_clip(idea.target_customers, budget)),
            "",
            "🔥 <b>Nega talab bor</b>",
            e(_clip(idea.why_demand_high, budget)),
            "",
            "🛡 <b>Nega raqobat kam</b>",
            e(_clip(idea.why_competition_low, budget)),
            "",
            "💰 <b>Boshlang'ich xarajat (taxmin)</b>",
            e(_clip(idea.startup_cost_estimate, 300)),
        ]
        if idea.first_steps:
            lines += ["", "🚀 <b>Birinchi qadamlar</b>"]
            lines += [f"{num(i)} {e(_clip(s, budget // 3))}" for i, s in enumerate(idea.first_steps[:3], 1)]
        if idea.risks:
            lines += ["", "⚠️ <b>Xavflar</b>"] + [f"• {e(_clip(r, budget // 3))}" for r in idea.risks[:3]]
        if not web_used:
            lines += ["", "ℹ️ <i>Internet ma'lumotisiz tayyorlangan, raqamlar taxminiy.</i>"]
        lines += ["", "👇 <b>To'liq SUPER PROMPT</b> — pastdagi tugmada"]
        text = "\n".join(lines)
        if len(text) <= SAFE_LIMIT:
            return text
    return render_channel_caption(idea, web_used)


def render_channel_caption(idea: ScoutIdea, web_used: bool, credit: str = "") -> str:
    """Rasmli post uchun qisqa matn (Telegram limiti 1024 belgi). `credit` — tayyor HTML."""
    demand = _GOOD_HIGH.get(idea.demand_level, "⚪")
    competition = _GOOD_LOW.get(idea.competition_level, "⚪")
    for budget in (260, 190, 130, 80):
        lines = [
            f"💡 <b>{e(_clip(idea.title, 110))}</b>",
            "",
            f"<i>{e(_clip(idea.one_liner, budget))}</i>",
            "",
            f"📈 Talab: <b>{e(idea.demand_level)}</b> {demand}  ·  "
            f"⚔️ Raqobat: <b>{e(idea.competition_level)}</b> {competition}",
            f"🎯 <b>Kim uchun:</b> {e(_clip(idea.target_customers, budget // 2))}",
            f"🔥 <b>Nega hozir:</b> {e(_clip(idea.why_demand_high, budget))}",
            f"💰 <b>Start:</b> {e(_clip(idea.startup_cost_estimate, 110))}",
        ]
        if not web_used:
            lines.append("<i>Raqamlar taxminiy.</i>")
        lines += ["", "👇 <b>To'liq SUPER PROMPT</b> — pastdagi tugmada"]
        if credit:
            lines.append(credit)
        text = "\n".join(lines)
        if len(text) <= CAPTION_LIMIT:
            return text
    tail = f"\n{credit}" if credit else ""
    return f"💡 <b>{e(_clip(idea.title, 110))}</b>\n\n👇 To'liq SUPER PROMPT — pastdagi tugmada{tail}"
