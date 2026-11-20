"""Moliyaviy model: raqamlarni AI emas, Python hisoblaydi.

AI faqat kirish qiymatlarini (narx, tannarx, xarajatlar, sotuv hajmi) suhbatdan ajratib
beradi va ularning har biri fakt yoki taxmin ekanini belgilaydi. Foyda, zararsizlik
nuqtasi va qoplash muddati shu yerda aniq formulalar bilan hisoblanadi.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, Field

Basis = Literal["fakt", "taxmin"]

SCENARIO_NAMES = {"pessimistic": "Pessimistik", "realistic": "Realistik", "optimistic": "Optimistik"}


class FinanceInputs(BaseModel):
    """Analyst agenti suhbatdan ajratadigan kirish qiymatlari (so'mda). Noma'lum bo'lsa 0."""

    unit_name: str = Field(default="dona", max_length=40)
    price_per_unit: int = Field(default=0, ge=0, le=10_000_000_000)
    variable_cost_per_unit: int = Field(default=0, ge=0, le=10_000_000_000)
    monthly_fixed_costs: int = Field(default=0, ge=0, le=100_000_000_000)
    startup_cost: int = Field(default=0, ge=0, le=1_000_000_000_000)
    units_pessimistic: int = Field(default=0, ge=0, le=100_000_000)
    units_realistic: int = Field(default=0, ge=0, le=100_000_000)
    units_optimistic: int = Field(default=0, ge=0, le=100_000_000)
    basis: Basis = "taxmin"
    notes: str = Field(default="", max_length=600)


@dataclass(frozen=True)
class Scenario:
    name: str
    units: int
    revenue: int
    variable_costs: int
    fixed_costs: int
    profit: int
    payback_months: float | None  # None — foyda yo'q, investitsiya qoplanmaydi


@dataclass(frozen=True)
class FinanceModel:
    inputs: FinanceInputs
    margin_per_unit: int
    margin_pct: float
    breakeven_units: int | None  # None — har bir sotuv zarar (marja <= 0)
    scenarios: list[Scenario]
    warnings: list[str]

    @property
    def usable(self) -> bool:
        return self.inputs.price_per_unit > 0 and any(s.units for s in self.scenarios)


def build_model(inp: FinanceInputs) -> FinanceModel:
    margin = inp.price_per_unit - inp.variable_cost_per_unit
    margin_pct = (margin / inp.price_per_unit * 100) if inp.price_per_unit else 0.0
    breakeven = math.ceil(inp.monthly_fixed_costs / margin) if margin > 0 else None

    scenarios = []
    for key, units in (
        ("pessimistic", inp.units_pessimistic),
        ("realistic", inp.units_realistic),
        ("optimistic", inp.units_optimistic),
    ):
        revenue = units * inp.price_per_unit
        var_costs = units * inp.variable_cost_per_unit
        profit = revenue - var_costs - inp.monthly_fixed_costs
        payback = round(inp.startup_cost / profit, 1) if profit > 0 and inp.startup_cost else None
        if profit > 0 and not inp.startup_cost:
            payback = 0.0
        scenarios.append(
            Scenario(SCENARIO_NAMES[key], units, revenue, var_costs, inp.monthly_fixed_costs, profit, payback)
        )

    warnings: list[str] = []
    if inp.price_per_unit and margin <= 0:
        warnings.append("Tannarx narxdan yuqori yoki teng: har bir sotuv zarar keltiradi.")
    elif inp.price_per_unit and margin_pct < 15:
        warnings.append("Marja juda past (15% dan kam): kichik xarajat o'sishi ham zararga olib keladi.")
    if breakeven and inp.units_realistic and inp.units_realistic < breakeven:
        warnings.append("Realistik sotuv hajmi zararsizlik nuqtasidan past.")
    if inp.basis == "taxmin":
        warnings.append("Raqamlar taxminiy: ularni haqiqiy narx va xarajatlar bilan tekshiring.")
    return FinanceModel(inp, margin, round(margin_pct, 1), breakeven, scenarios, warnings)


def money(amount: int | float) -> str:
    return f"{int(round(amount)):,}".replace(",", " ")


def render_finance(model: FinanceModel, html: bool = True) -> str:
    """Telegram (HTML) uchun moliyaviy model bo'limi."""
    from app.core.text import e  # aylanma importdan qochish

    inp = model.inputs
    b = (lambda x: f"<b>{x}</b>") if html else (lambda x: x)
    lines = [
        b("💰 MOLIYAVIY MODEL") + " (hisob-kitobni Python bajardi)",
        "",
        f"Narx: {money(inp.price_per_unit)} so'm / {e(inp.unit_name)}",
        f"O'zgaruvchan xarajat: {money(inp.variable_cost_per_unit)} so'm / {e(inp.unit_name)}",
        f"Marja: {b(money(model.margin_per_unit) + ' so`m')} ({model.margin_pct}%)",
        f"Oylik doimiy xarajatlar: {money(inp.monthly_fixed_costs)} so'm",
        f"Boshlang'ich sarmoya: {money(inp.startup_cost)} so'm",
        "Zararsizlik nuqtasi: "
        + (
            b(f"oyiga {money(model.breakeven_units)} {e(inp.unit_name)}")
            if model.breakeven_units is not None
            else b("erishib bo'lmaydi (marja manfiy)")
        ),
        "",
    ]
    for s in model.scenarios:
        payback = f"{s.payback_months} oyda qoplanadi" if s.payback_months is not None else "qoplanmaydi"
        sign = "📈" if s.profit > 0 else "📉"
        lines.append(
            f"{sign} {b(s.name)}: {money(s.units)} {e(inp.unit_name)}/oy → "
            f"foyda {money(s.profit)} so'm/oy, sarmoya {payback}"
        )
    if model.warnings:
        lines += [""] + [f"⚠️ {e(w)}" for w in model.warnings]
    if inp.notes:
        lines += ["", f"<i>{e(inp.notes)}</i>" if html else inp.notes]
    lines += [
        "",
        "<i>Formulalar: marja = narx − o'zgaruvchan xarajat; foyda = sotuv × marja − doimiy "
        "xarajat; zararsizlik = doimiy xarajat ÷ marja; qoplash = sarmoya ÷ oylik foyda.</i>",
    ]
    return "\n".join(lines)


def finance_summary_for_ai(model: FinanceModel) -> str:
    """Prompt engineer agentiga beriladigan qisqa matn."""
    inp = model.inputs
    rows = [
        f"Narx {inp.price_per_unit} so'm/{inp.unit_name}, o'zgaruvchan xarajat {inp.variable_cost_per_unit}, "
        f"marja {model.margin_per_unit} ({model.margin_pct}%), doimiy xarajat {inp.monthly_fixed_costs}/oy, "
        f"sarmoya {inp.startup_cost}, zararsizlik {model.breakeven_units} {inp.unit_name}/oy, "
        f"asos: {inp.basis}."
    ]
    rows += [
        f"{s.name}: {s.units}/oy, foyda {s.profit}/oy, qoplash {s.payback_months} oy" for s in model.scenarios
    ]
    return "\n".join(rows)
