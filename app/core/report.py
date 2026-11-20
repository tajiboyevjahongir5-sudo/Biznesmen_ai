"""To'liq tahlil hisoboti — bitta mustaqil HTML fayl.

Fayl brauzerda ochiladi, internetsiz ishlaydi (tashqi CSS/JS/rasm yo'q) va
brauzerning "Chop etish → PDF" funksiyasi bilan PDF qilib saqlanadi. Barcha AI va
foydalanuvchi matni HTML-escape qilinadi; havolalar faqat http(s) bo'lsa qo'yiladi.
"""

from __future__ import annotations

from datetime import UTC, datetime
from html import escape

from app.agents.analyst import MarketAnalysis
from app.core.finance import FinanceModel, money
from app.services.ai_service import Source

KIND = {"fakt": "Fakt", "taxmin": "Taxmin", "prognoz": "Prognoz"}

CSS = """
*{box-sizing:border-box}body{font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;
margin:0;background:#f4f6fa;color:#1d2433;line-height:1.55}
.wrap{max-width:860px;margin:0 auto;padding:32px 20px}
header{background:linear-gradient(135deg,#1f3a8a,#2563eb);color:#fff;border-radius:16px;padding:28px}
header h1{margin:0 0 6px;font-size:26px}header p{margin:0;opacity:.85}
.badges{margin-top:14px;display:flex;flex-wrap:wrap;gap:8px}
.badge{background:rgba(255,255,255,.18);padding:5px 12px;border-radius:999px;font-size:14px}
section{background:#fff;border-radius:14px;padding:22px 24px;margin-top:18px;
box-shadow:0 1px 3px rgba(0,0,0,.06)}
h2{margin:0 0 12px;font-size:19px;color:#1f3a8a}
ul{padding-left:20px;margin:6px 0}li{margin:4px 0}
.tag{display:inline-block;font-size:12px;padding:1px 8px;border-radius:6px;background:#eef2ff;
color:#3730a3;margin-right:6px}
.risk{border-left:4px solid #ef4444;padding:8px 14px;margin:10px 0;background:#fef2f2;border-radius:6px}
.debate{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin:10px 0}
.opt{background:#ecfdf5;padding:10px;border-radius:8px}.skp{background:#fff7ed;padding:10px;border-radius:8px}
.res{grid-column:1/3;background:#f1f5f9;padding:10px;border-radius:8px}
.verdict{font-size:20px;font-weight:700;color:#0f766e}
table{width:100%;border-collapse:collapse;margin-top:10px;font-size:14px}
th,td{border-bottom:1px solid #e5e7eb;padding:8px;text-align:right}
th:first-child,td:first-child{text-align:left}
.neg{color:#dc2626}.pos{color:#059669}.warn{color:#b45309}
pre{white-space:pre-wrap;word-wrap:break-word;background:#0f172a;color:#e2e8f0;padding:18px;
border-radius:10px;font-size:13px}
.muted{color:#6b7280;font-size:13px}
@media print{body{background:#fff}section,header{box-shadow:none;break-inside:avoid}}
"""


def _e(text: str) -> str:
    return escape(text or "", quote=True)


def _link(url: str, title: str) -> str:
    if not url.startswith(("https://", "http://")):
        return _e(title)
    return f'<a href="{_e(url)}" target="_blank" rel="noopener noreferrer">{_e(title)}</a>'


def _claims(items) -> str:
    if not items:
        return "<p class='muted'>Ma'lumot yo'q.</p>"
    rows = []
    for c in items:
        src = f" — {_link(c.source_url, 'manba')}" if c.source_url else ""
        rows.append(f"<li><span class='tag'>{KIND[c.kind]}</span>{_e(c.text)}{src}</li>")
    return "<ul>" + "".join(rows) + "</ul>"


def _list(items: list[str], ordered: bool = False) -> str:
    if not items:
        return ""
    tag = "ol" if ordered else "ul"
    return f"<{tag}>" + "".join(f"<li>{_e(x)}</li>" for x in items) + f"</{tag}>"


def _finance(model: FinanceModel) -> str:
    inp = model.inputs
    unit = _e(inp.unit_name)
    be = (
        f"oyiga {money(model.breakeven_units)} {unit}"
        if model.breakeven_units is not None
        else "erishib bo'lmaydi (marja manfiy)"
    )
    rows = "".join(
        f"<tr><td>{_e(s.name)}</td><td>{money(s.units)}</td><td>{money(s.revenue)}</td>"
        f"<td class='{'pos' if s.profit > 0 else 'neg'}'>{money(s.profit)}</td>"
        f"<td>{s.payback_months if s.payback_months is not None else '—'}</td></tr>"
        for s in model.scenarios
    )
    warns = "".join(f"<li class='warn'>{_e(w)}</li>" for w in model.warnings)
    notes = f"<p class='muted'>{_e(inp.notes)}</p>" if inp.notes else ""
    return f"""
<section><h2>💰 Moliyaviy model</h2>
<p>Narx: <b>{money(inp.price_per_unit)} so'm</b> / {unit} ·
O'zgaruvchan xarajat: {money(inp.variable_cost_per_unit)} so'm ·
Marja: <b>{money(model.margin_per_unit)} so'm ({model.margin_pct}%)</b><br>
Oylik doimiy xarajat: {money(inp.monthly_fixed_costs)} so'm ·
Boshlang'ich sarmoya: {money(inp.startup_cost)} so'm<br>
Zararsizlik nuqtasi: <b>{be}</b></p>
<table><tr><th>Ssenariy</th><th>Sotuv/oy</th><th>Tushum/oy</th><th>Foyda/oy</th><th>Qoplash (oy)</th></tr>
{rows}</table>
<ul>{warns}</ul>{notes}
<p class="muted">Formulalar: marja = narx − o'zgaruvchan xarajat; foyda = sotuv × marja − doimiy xarajat;
zararsizlik = doimiy xarajat ÷ marja; qoplash = sarmoya ÷ oylik foyda. Hisob-kitobni dastur bajargan,
kirish raqamlari esa {"foydalanuvchi ma'lumoti" if inp.basis == "fakt" else "taxminiy"}.</p>
</section>"""


def build_report_html(
    *,
    project_id: int,
    title: str,
    analysis: MarketAnalysis,
    sources: list[Source],
    web_used: bool,
    finance: FinanceModel | None,
    prompt_title: str,
    prompt_text: str,
    tech_prompt_text: str = "",
) -> str:
    a = analysis
    title = " ".join(title.split())
    if len(title) > 120:
        title = title[:119].rstrip(" ,.;:") + "…"
    tech = (
        "<section><h2>🛠 TEXNIK PROMPT (dasturchi AI uchun)</h2>\n"
        "<p class='muted'>Kod tuzilishi, xavfsizlik, UI/UX dizayn, animatsiya va deploy bo'yicha "
        "to'liq topshiriq. Claude Code, Cursor, ChatGPT yoki Gemini ga bering.</p>\n"
        f"<pre>{_e(tech_prompt_text)}</pre></section>\n"
        if tech_prompt_text
        else ""
    )
    date = datetime.now(UTC).strftime("%d.%m.%Y")
    competitors = "".join(
        f"<li><b>{_e(c.name)}</b> <span class='tag'>{KIND[c.kind]}</span>{_e(c.note)}</li>"
        for c in a.competitors
    )
    risks = "".join(
        f"<div class='risk'><b>{_e(r.risk)}</b> <span class='tag'>ehtimol: {_e(r.probability)}</span><br>"
        f"<b>Sabab:</b> {_e(r.why)}<br><b>Oldini olish:</b> {_e(r.prevention)}</div>"
        for r in a.failure_risks
    )
    debate = "".join(
        f"<div class='debate'><div class='opt'><b>Optimist:</b> {_e(d.optimist)}</div>"
        f"<div class='skp'><b>Skeptik:</b> {_e(d.skeptic)}</div>"
        f"<div class='res'><b>Xulosa:</b> {_e(d.resolution)}</div></div>"
        for d in a.debate
    )
    src = "".join(f"<li>{_link(s.url, s.title)}</li>" for s in sources)
    wrong = (
        "<h3>Ehtimoliy noto'g'ri taxminlar</h3>" + _list(a.wrong_assumptions) if a.wrong_assumptions else ""
    )
    web_note = (
        ""
        if web_used
        else "<p class='muted'>Bu tahlil internet qidiruvisiz bajarildi: bozor bo'yicha da'volar taxmin "
        "sifatida berilgan, ularni o'zingiz tekshiring.</p>"
    )
    return f"""<!DOCTYPE html>
<html lang="uz"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_e(title)} — BIZNES MIND AI hisoboti</title><style>{CSS}</style></head>
<body><div class="wrap">
<header><h1>{_e(title)}</h1><p>BIZNES MIND AI · Loyiha #{project_id} · {date}</p>
<div class="badges"><span class="badge">📈 Talab: {_e(a.demand_level)}</span>
<span class="badge">⚔️ Raqobat: {_e(a.competition_level)}</span>
<span class="badge">🎯 Ishonch: {_e(a.confidence)}</span></div></header>

<section><h2>📋 Qisqacha</h2><p>{_e(a.summary)}</p>{web_note}</section>

<section><h2>✅ Yakuniy qaror</h2><p class="verdict">{_e(a.verdict)}</p>{_list(a.verdict_reasons)}</section>

<section><h2>📊 Talab</h2>{_claims(a.demand_evidence)}</section>

<section><h2>⚔️ Raqobat</h2><ul>{competitors or "<li class='muted'>Aniqlanmagan</li>"}</ul>
<p><b>Qanday ajralib turish mumkin:</b> {_e(a.differentiation)}</p></section>

<section><h2>💪 Kuchli tomonlar</h2>{_claims(a.strengths)}</section>

<section><h2>⚠️ Nima uchun muvaffaqiyatsiz bo'lishi mumkin?</h2>{risks}
{wrong}</section>

{f"<section><h2>🗣 Agentlar bahsi</h2>{debate}</section>" if a.debate else ""}

{_finance(finance) if finance else ""}

<section><h2>🧪 Kichik test (MVP)</h2><p>{_e(a.mvp_test)}</p>
{"<h3>Albatta tekshiring</h3>" + _list(a.must_verify) if a.must_verify else ""}
{"<h3>Keyingi qadamlar</h3>" + _list(a.next_steps, ordered=True) if a.next_steps else ""}</section>

<section><h2>🤖 SUPER PROMPT: {_e(prompt_title)}</h2>
<p class="muted">Matnni to'liq nusxa olib, istalgan AI (ChatGPT, Claude, Gemini) ga yuboring.</p>
<pre>{_e(prompt_text)}</pre></section>

{tech}
{f"<section><h2>🔗 Internet manbalari</h2><ul>{src}</ul></section>" if src else ""}

<p class="muted" style="text-align:center;margin-top:24px">Bu hisobot maslahat xarakterida.
Hech qanday natija kafolatlanmaydi. © BIZNES MIND AI</p>
</div></body></html>"""
