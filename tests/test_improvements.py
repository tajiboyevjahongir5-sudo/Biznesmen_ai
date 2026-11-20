"""Moliyaviy model, hisobot, obuna, migratsiya, suhbat orqaga qaytarish va statistika testlari."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.agents.analyst import AnalystAgent, MarketAnalysis
from app.agents.interviewer import InterviewerAgent
from app.agents.prompt_engineer import PromptEngineerAgent
from app.core.finance import FinanceInputs, build_model, render_finance
from app.core.orchestrator import Orchestrator
from app.core.report import build_report_html
from app.core.text import format_reply
from app.database import repository as repo
from app.database.models import BusinessProject, SubscriptionStatus
from app.database.session import init_db
from app.services.ai_service import AIBusyError, Source
from app.services.subscription_service import SubscriptionService


def _inputs(**kw) -> FinanceInputs:
    data = dict(
        unit_name="porsiya",
        price_per_unit=30_000,
        variable_cost_per_unit=18_000,
        monthly_fixed_costs=6_000_000,
        startup_cost=60_000_000,
        units_pessimistic=300,
        units_realistic=700,
        units_optimistic=1200,
        basis="taxmin",
    )
    data.update(kw)
    return FinanceInputs(**data)


def test_finance_model_math():
    m = build_model(_inputs())
    assert m.margin_per_unit == 12_000
    assert m.margin_pct == 40.0
    assert m.breakeven_units == 500  # 6 000 000 / 12 000
    pess, real, opt = m.scenarios
    assert pess.profit == 300 * 12_000 - 6_000_000 < 0 and pess.payback_months is None
    assert real.profit == 2_400_000 and real.payback_months == 25.0
    assert opt.profit == 8_400_000
    assert m.usable
    assert any("taxminiy" in w for w in m.warnings)


def test_finance_negative_margin_warns_and_no_breakeven():
    m = build_model(_inputs(variable_cost_per_unit=35_000))
    assert m.breakeven_units is None
    assert any("zarar" in w for w in m.warnings)
    assert "erishib bo'lmaydi" in render_finance(m)


def test_finance_unusable_when_no_data():
    assert not build_model(FinanceInputs()).usable


def test_format_reply_bold_only_and_escapes_html():
    out = format_reply("Salom **muhim** <script>x</script> ** yolg'iz")
    assert "<b>muhim</b>" in out
    assert "<script>" not in out and "&lt;script&gt;" in out
    assert "**" not in out


def _analysis(**kw) -> MarketAnalysis:
    data = dict(
        summary="Xulosa <b>",
        demand_level="o'rta",
        demand_evidence=[],
        competition_level="yuqori",
        competitors=[],
        differentiation="",
        strengths=[],
        failure_risks=[],
        wrong_assumptions=[],
        debate=[],
        must_verify=[],
        mvp_test="Test",
        verdict="Avval kichik test qilish kerak",
        verdict_reasons=[],
        next_steps=[],
        confidence="past",
    )
    data.update(kw)
    return MarketAnalysis(**data)


def test_report_escapes_and_blocks_bad_links():
    html = build_report_html(
        project_id=1,
        title="<img src=x onerror=alert(1)>",
        analysis=_analysis(),
        sources=[Source(url="javascript:alert(1)", title="yomon"), Source(url="https://ok.uz", title="ok")],
        web_used=True,
        finance=build_model(_inputs()),
        prompt_title="P",
        prompt_text="</pre><script>alert(1)</script>",
    )
    assert "<img src=x" not in html and "<script>alert" not in html
    assert 'href="javascript:' not in html
    assert 'href="https://ok.uz"' in html
    assert "Moliyaviy model" in html


async def test_old_analysis_json_without_finance_still_loads():
    old = _analysis().model_dump()
    old.pop("finance")
    restored = MarketAnalysis.model_validate(old)
    assert not build_model(restored.finance).usable


async def test_subscription_grant_extend_remind_expire(session_factory):
    now = [datetime(2026, 1, 1, tzinfo=UTC)]
    svc = SubscriptionService(session_factory, clock=lambda: now[0])
    async with session_factory() as s:
        user = await repo.get_or_create_user(s, 42, None, None)
        await s.commit()

    sub = await svc.grant(user.id, 42, 30, reservation_id=1)
    assert sub.expires_at.replace(tzinfo=UTC) == now[0] + timedelta(days=30)
    assert not svc.renewable(sub)

    now[0] += timedelta(days=28)  # 2 kun qoldi
    assert [x.id for x in await svc.due_reminders()] == [sub.id]
    assert svc.renewable(await svc.active_for(user.id))
    await svc.mark(sub.id, reminded=True)
    assert await svc.due_reminders() == []

    sub = await svc.grant(user.id, 42, 30, reservation_id=2)  # oldindan uzaytirish
    assert sub.expires_at.replace(tzinfo=UTC) == datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=60)

    now[0] += timedelta(days=40)
    expired = await svc.due_expired()
    assert [x.id for x in expired] == [sub.id]
    await svc.mark(sub.id, status=SubscriptionStatus.EXPIRED)
    assert await svc.active_for(user.id) is None
    assert await svc.count_active() == 0


async def test_lifetime_subscription_never_expires(session_factory):
    svc = SubscriptionService(session_factory)
    async with session_factory() as s:
        user = await repo.get_or_create_user(s, 43, None, None)
        await s.commit()
    sub = await svc.grant(user.id, 43, 0, None)
    assert sub.expires_at is None
    assert await svc.due_expired() == [] and not svc.renewable(sub)


async def test_init_db_adds_new_columns_to_old_table(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'old.db'}")
    async with engine.begin() as conn:
        # Eski versiyadagi users jadvali: last_active_at ustuni yo'q
        await conn.execute(
            text(
                "CREATE TABLE users (id INTEGER PRIMARY KEY, telegram_id BIGINT UNIQUE, "
                "username VARCHAR(64), "
                "first_name VARCHAR(128), is_blocked BOOLEAN, promo_sent_at DATETIME, created_at DATETIME)"
            )
        )
        await conn.execute(text("INSERT INTO users (id, telegram_id, is_blocked) VALUES (1, 5, 0)"))
    await init_db(engine)
    async with engine.connect() as conn:
        cols = {row[1] for row in (await conn.execute(text("PRAGMA table_info(users)"))).all()}
        count = (await conn.execute(text("SELECT count(*) FROM users"))).scalar()
    assert "last_active_at" in cols and count == 1  # ma'lumot saqlanib qoldi
    await engine.dispose()


async def test_failed_ai_turn_does_not_leave_orphan_messages(session_factory):
    interviewer = MagicMock(spec=InterviewerAgent)
    interviewer.run = AsyncMock(side_effect=AIBusyError())
    orch = Orchestrator(
        session_factory, interviewer, MagicMock(spec=AnalystAgent), MagicMock(spec=PromptEngineerAgent), 0, 12
    )
    async with session_factory() as s:
        user = await repo.get_or_create_user(s, 900, None, None)
        await s.commit()

    # Yangi loyiha: AI ishlamasa, loyiha umuman qolmaydi
    with pytest.raises(AIBusyError):
        await orch.handle_user_text(telegram_id=900, user_db_id=user.id, text="Kafe")
    async with session_factory() as s:
        assert await repo.get_active_project(s, user.id) is None

    # Davom etayotgan suhbat: javobsiz qolgan foydalanuvchi xabari o'chiriladi
    async with session_factory() as s:
        project = await repo.create_project(s, user.id, "Kafe")
        await s.commit()
        pid = project.id
    with pytest.raises(AIBusyError):
        await orch.handle_user_text(telegram_id=900, user_db_id=user.id, text="Toshkentda")
    async with session_factory() as s:
        assert await repo.get_messages(s, pid) == []
        assert (await s.get(BusinessProject, pid)) is not None


async def test_bot_stats_and_activity(session_factory):
    now = datetime.now(UTC)
    async with session_factory() as s:
        u = await repo.get_or_create_user(s, 77, None, None)
        await repo.touch_user(s, u, now)
        await repo.get_or_create_user(s, 78, None, None)
        await s.commit()
        st = await repo.bot_stats(s, now)
        targets = await repo.broadcast_targets(s)
    assert st["users"] == 2 and st["active_day"] == 1 and st["revenue_month"] == 0
    assert sorted(targets) == [77, 78]
