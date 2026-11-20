"""Xabarlar dizayni: teglar har bir bo'lakda to'g'ri yopilgan, limitlar saqlangan."""

from html.parser import HTMLParser

from app.agents.analyst import Claim, MarketAnalysis
from app.agents.idea_scout import ScoutIdea
from app.core.finance import FinanceInputs, build_model, render_finance
from app.core.text import (
    SAFE_LIMIT,
    TELEGRAM_LIMIT,
    level_line,
    num,
    render_analysis,
    render_channel_post,
    render_prompt,
    split_message,
)
from app.services.ai_service import Source

ALLOWED = {"b", "i", "a", "blockquote", "pre", "code"}


class _Checker(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.stack: list[str] = []
        self.errors: list[str] = []
        self.visible = 0  # Telegram limiti teglar va &amp; ochilgandan keyingi uzunlikka qo'llanadi

    def handle_data(self, data):
        self.visible += len(data)

    def handle_starttag(self, tag, attrs):
        if tag not in ALLOWED:
            self.errors.append(f"ruxsatsiz teg {tag}")
        self.stack.append(tag)

    def handle_endtag(self, tag):
        if not self.stack or self.stack[-1] != tag:
            self.errors.append(f"noto'g'ri yopilgan {tag}")
        else:
            self.stack.pop()


def assert_valid_html(part: str) -> None:
    c = _Checker()
    c.feed(part)
    c.close()
    assert not c.errors and not c.stack, (c.errors, c.stack, part[:200])
    assert 0 < c.visible <= TELEGRAM_LIMIT


def test_split_keeps_blockquote_balanced():
    body = "\n".join(f"qator {i} " + "x" * 150 for i in range(60))
    text = "Sarlavha\n<blockquote expandable>" + body + "</blockquote>\nOxiri"
    parts = split_message(text, limit=1000)
    assert len(parts) > 3
    for p in parts:
        assert_valid_html(p)
        assert len(p) <= 1000
    # Ikkinchi va keyingi bo'laklar iqtibos ichida davom etadi
    assert parts[1].startswith("<blockquote expandable>")


def _big_analysis() -> MarketAnalysis:
    claim = Claim(text="Dalil & <script>" + "d" * 450, kind="fakt", source_url="https://stat.uz/a?b=1&c=2")
    bad = Claim(text="yomon havola", kind="taxmin", source_url="javascript:alert(1)")
    return MarketAnalysis(
        summary="X" * 1200,
        demand_level="yuqori",
        demand_evidence=[claim] * 6 + [bad],
        competition_level="past",
        competitors=[{"name": "Raqib", "note": "n" * 400, "kind": "taxmin"}] * 5,
        differentiation="D" * 800,
        strengths=[claim] * 4,
        failure_risks=[
            {"risk": "R" * 300, "why": "W" * 600, "probability": "yuqori", "prevention": "P" * 500}
        ]
        * 6,
        wrong_assumptions=["a" * 300] * 5,
        debate=[{"optimist": "o" * 500, "skeptic": "s" * 500, "resolution": "r" * 500}] * 6,
        must_verify=["m" * 200] * 5,
        mvp_test="M" * 1000,
        verdict="Boshlash mumkin",
        verdict_reasons=["sabab"] * 4,
        next_steps=["qadam"] * 12,
        confidence="o'rta",
    )


def test_render_analysis_parts_are_valid_html():
    sources = [Source(f"https://site{i}.uz/x?a=1&b=2", f"Manba <{i}>") for i in range(12)]
    parts = render_analysis(_big_analysis(), sources, web_used=True)
    assert len(parts) >= 5
    for p in parts:
        assert_valid_html(p)
    joined = "\n".join(parts)
    assert "javascript:" not in joined  # xavfli havola chiqarilmadi
    assert "&lt;script&gt;" in joined and "<script>" not in joined
    assert "🟢 Qisqa xulosa" in joined and "🔟" in joined and "11." in joined


def test_render_prompt_and_finance_are_valid_html():
    parts = render_prompt("Kafe <1>", "Siz & biz\n" * 900, "Nusxa oling")
    assert len(parts) >= 3 and "2 qismdan" not in parts[0]
    for p in parts:
        assert_valid_html(p)
    assert parts[1].startswith('<pre><code class="language-prompt">')

    model = build_model(
        FinanceInputs(
            unit_name="porsiya <x>",
            price_per_unit=30_000,
            variable_cost_per_unit=12_000,
            monthly_fixed_costs=9_000_000,
            startup_cost=60_000_000,
            units_pessimistic=300,
            units_realistic=700,
            units_optimistic=1200,
            notes="izoh & <b>",
        )
    )
    text = render_finance(model)
    assert_valid_html(text)
    assert "MOLIYAVIY MODEL" in text and "oyiga 500" in text


def test_channel_post_never_breaks_html_even_when_fields_are_long():
    idea = ScoutIdea(
        title="T" * 120,
        one_liner="O" * 400,
        target_customers="K" * 500,
        why_demand_high="D & <b>" * 110,
        why_competition_low="C" * 800,
        demand_level="yuqori",
        competition_level="past",
        startup_cost_estimate="S" * 300,
        first_steps=["f" * 400] * 5,
        risks=["r" * 400] * 5,
        evidence=[],
    )
    post = render_channel_post(idea, web_used=False)
    assert_valid_html(post)
    assert len(post) <= SAFE_LIMIT and "SUPER PROMPT" in post


def test_small_helpers():
    assert num(1) == "1️⃣" and num(10) == "🔟" and num(11) == "11."
    assert "🔴" in level_line("⚔️", "Raqobat", "yuqori", good_high=False)
    assert "🟢" in level_line("📈", "Talab", "yuqori")
