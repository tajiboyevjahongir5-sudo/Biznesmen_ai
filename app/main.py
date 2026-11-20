"""BIZNES MIND AI — ishga tushirish nuqtasi.

python -m app.main
"""

from __future__ import annotations

import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand

from app.agents.analyst import AnalystAgent
from app.agents.coach import CoachAgent
from app.agents.idea_scout import IdeaScoutAgent
from app.agents.interviewer import InterviewerAgent
from app.agents.prompt_engineer import PromptEngineerAgent
from app.bot.context import AppContext
from app.bot.handlers import admin, errors, payment, user
from app.bot.jobs import job_worker_loop
from app.bot.middlewares.common import ThrottlingMiddleware, UserContextMiddleware
from app.config import Settings, get_settings
from app.core.logging import setup_logging
from app.core.orchestrator import Orchestrator
from app.core.security import RateLimiter, UserLocks
from app.database.session import create_engine, create_session_factory, init_db
from app.services.ai_service import AIService
from app.services.channel_service import ChannelService
from app.services.payment_service import PaymentService
from app.services.runtime_settings import RuntimeSettings
from app.services.scheduler import (
    daily_research_loop,
    followup_loop,
    payment_sweeper_loop,
    subscription_loop,
)
from app.services.subscription_service import SubscriptionService

logger = logging.getLogger("app")

USER_COMMANDS = [
    BotCommand(command="start", description="Botni boshlash"),
    BotCommand(command="new", description="Yangi biznes g'oya"),
    BotCommand(command="history", description="Mening loyihalarim"),
    BotCommand(command="kanal", description="G'oyalar kanali"),
    BotCommand(command="report", description="Loyiha hisobotini fayl qilib olish"),
    BotCommand(command="cancel", description="Joriy suhbatni to'xtatish"),
    BotCommand(command="help", description="Yordam"),
]


def build_context(settings: Settings, session_factory, runtime: RuntimeSettings) -> AppContext:
    ai = AIService(settings)
    prompt_engineer = PromptEngineerAgent(ai)
    return AppContext(
        settings=settings,
        session_factory=session_factory,
        ai=ai,
        orchestrator=Orchestrator(
            session_factory,
            InterviewerAgent(ai),
            AnalystAgent(ai),
            prompt_engineer,
            settings.min_interview_questions,
            settings.max_interview_questions,
            coach=CoachAgent(ai),
            followup_days=lambda: settings.followup_days,
        ),
        payments=PaymentService(
            session_factory, settings.channel_price_uzs, settings.payment_timeout_seconds
        ),
        channel=ChannelService(settings, session_factory, IdeaScoutAgent(ai), prompt_engineer),
        rate_limiter=RateLimiter(settings.rate_limit_messages, settings.rate_limit_window_seconds),
        user_locks=UserLocks(),
        runtime=runtime,
        subscriptions=SubscriptionService(session_factory),
    )


def build_dispatcher(ctx: AppContext) -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage(), ctx=ctx)
    for observer in (dp.message, dp.callback_query):
        observer.outer_middleware(UserContextMiddleware(ctx))
        observer.middleware(ThrottlingMiddleware(ctx))
    # Tartib muhim: admin -> to'lov -> foydalanuvchi (catch-all oxirida).
    dp.include_routers(errors.router, admin.router, payment.router, payment.channel_router, user.router)
    return dp


async def run() -> None:
    settings = get_settings()
    setup_logging(settings.log_level, settings.secret_values(), settings.log_file)

    missing = settings.missing_required()
    if missing:
        logger.error("Sozlamalar to'ldirilmagan (.env yoki Railway Variables): %s", ", ".join(missing))
        print(
            "\nXATO: quyidagi sozlamalar to'ldirilmagan (.env yoki Railway Variables): "
            + ", ".join(missing)
            + "\n.env.example dan nusxa olib, qiymatlarni kiriting (README ga qarang).",
            file=sys.stderr,
        )
        raise SystemExit(1)

    engine = create_engine(settings.database_url)
    await init_db(engine)
    session_factory = create_session_factory(engine)
    runtime = RuntimeSettings(settings, session_factory)
    loaded = await runtime.load()  # admin paneldan kiritilgan sozlamalar
    logger.info("Admin sozlamalari yuklandi: %d ta", loaded)
    ctx = build_context(settings, session_factory, runtime)

    bot = Bot(settings.bot_token.get_secret_value(), default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    me = await bot.get_me()
    ctx.bot_username = me.username or ""
    await bot.set_my_commands(USER_COMMANDS)

    if not settings.admin_ids:
        logger.warning("ADMIN_IDS bo'sh: admin panel va to'lov ogohlantirishlari ishlamaydi")
    if not settings.payments_configured:
        logger.warning("To'lov tizimi sozlanmagan: botda admin sifatida /settings yuborib kiriting")
    if not settings.web_search_enabled:
        logger.info("Internet qidiruvi o'chirilgan (RESEARCH_PROVIDER=none)")

    dp = build_dispatcher(ctx)
    restored = await payment.restore_timers(bot, ctx)  # redeploydan keyin taymerlar davom etadi
    if restored:
        logger.info("To'lov taymerlari tiklandi: %d ta", restored)
    tasks = [
        asyncio.create_task(daily_research_loop(bot, ctx)),
        asyncio.create_task(payment_sweeper_loop(ctx)),
        asyncio.create_task(subscription_loop(bot, ctx)),
        asyncio.create_task(job_worker_loop(bot, ctx)),
        asyncio.create_task(followup_loop(bot, ctx)),
    ]
    logger.info("Bot ishga tushdi: @%s", ctx.bot_username)
    try:
        await dp.start_polling(bot, allowed_updates=["message", "callback_query", "channel_post"])
    finally:
        for t in tasks:
            t.cancel()
        await bot.session.close()
        await engine.dispose()


def main() -> None:
    try:
        asyncio.run(run())
    except (KeyboardInterrupt, SystemExit) as exc:
        if isinstance(exc, SystemExit) and exc.code:
            raise


if __name__ == "__main__":
    main()
