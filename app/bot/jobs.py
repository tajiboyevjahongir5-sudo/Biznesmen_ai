"""Suhbat qadamini bajarish va AI band bo'lganda bazadagi navbat orqali qayta urinish.

Navbat `pending_jobs` jadvalida saqlanadi, shuning uchun bot qayta ishga tushsa
(Railway redeploy) ham kutilayotgan xabarlar yo'qolmaydi: fon vazifasi ishga
tushganda ularni topib, davom ettiradi.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import BufferedInputFile, ReactionTypeEmoji
from aiogram.utils.chat_action import ChatActionSender
from sqlalchemy import delete, select

from app.bot.context import AppContext
from app.bot.helpers import alert_admins, maybe_send_promo, send_parts
from app.core.logging import user_ref
from app.database.models import PendingJob
from app.services.ai_service import (
    AIBadResponseError,
    AIBusyError,
    AIRateLimitedError,
    AIServiceError,
    AITimeoutError,
    AIUnavailableError,
)

logger = logging.getLogger(__name__)

# AI vaqtincha band/sekin bo'lganda qayta urinish mantiqli bo'lgan xatolar.
TRANSIENT_AI_ERRORS = (
    AIBusyError,
    AITimeoutError,
    AIUnavailableError,
    AIRateLimitedError,
    AIBadResponseError,
)
RETRY_DELAYS = (60, 120, 240)  # soniya: 1-, 2-, 3-urinishdan oldin


@dataclass
class Job:
    chat_id: int
    message_id: int
    tg_id: int
    db_user_id: int
    text: str
    user_name: str = ""
    phase: str = "turn"  # turn | analysis
    project_id: int | None = None


def _now() -> datetime:
    return datetime.now(UTC)


async def react(bot: Bot, chat_id: int, message_id: int, emoji: str) -> None:
    """Xabarga reaksiya; Telegram rad etsa (reaksiyalar o'chirilgan va h.k.) — jim o'tadi."""
    try:
        await bot.set_message_reaction(chat_id, message_id, [ReactionTypeEmoji(emoji=emoji)])
    except Exception:  # noqa: BLE001 — reaksiya ixtiyoriy bezak
        logger.debug("reaction_failed", exc_info=True)


async def run_job(bot: Bot, ctx: AppContext, job: Job) -> None:
    """Suhbat qadamini (va kerak bo'lsa tahlilni) bajaradi. AI xatolarini yuqoriga uzatadi."""
    if job.phase == "turn":
        async with ChatActionSender.typing(bot=bot, chat_id=job.chat_id):
            result = await ctx.orchestrator.handle_user_text(
                telegram_id=job.tg_id, user_db_id=job.db_user_id, text=job.text, user_name=job.user_name
            )
        if result.reaction:
            await react(bot, job.chat_id, job.message_id, result.reaction)
        await send_parts(bot, job.chat_id, result.messages)
        if result.looking_for_idea:
            await maybe_send_promo(bot, ctx, job.tg_id, job.chat_id)
        if not (result.run_analysis and result.project_id):
            return
        job.phase, job.project_id = "analysis", result.project_id
        await bot.send_message(
            job.chat_id,
            "🧠 <b>Ma'lumot yetarli — tahlilni boshladim!</b>\n\n"
            "🔎 Bozor va talab\n"
            "⚔️ Raqobatchilar\n"
            "⚠️ Xavflar va agentlar bahsi\n"
            "💰 Moliyaviy model\n"
            "🤖 SUPER PROMPT\n\n"
            "⏳ <i>Bu 1–3 daqiqa vaqt oladi, kuting.</i>",
        )
    await run_analysis(bot, ctx, job)


async def run_analysis(bot: Bot, ctx: AppContext, job: Job) -> None:
    async with ChatActionSender.typing(bot=bot, chat_id=job.chat_id):
        output = await ctx.orchestrator.run_analysis(
            telegram_id=job.tg_id, user_db_id=job.db_user_id, project_id=job.project_id
        )
    await send_parts(bot, job.chat_id, output.parts)
    if output.report_html:
        await send_report(bot, job.chat_id, job.project_id, output.report_html)
    days = ctx.settings.followup_days
    later = (
        f"\n\n📅 {days} kundan keyin natijalaringiz bilan qiziqaman — rejani birga yangilaymiz."
        if days
        else ""
    )
    await bot.send_message(
        job.chat_id,
        "🎉 <b>Tahlil tayyor!</b>\n\n"
        "📂 Prompt va hisobotni keyin ham /history orqali topasiz.\n"
        "💡 Boshqa g'oyangiz bo'lsa, shunchaki yozing — yangi suhbat boshlaymiz." + later,
    )
    await maybe_send_promo(bot, ctx, job.tg_id, job.chat_id)


async def send_report(bot: Bot, chat_id: int, project_id: int, html_doc: str) -> None:
    """To'liq hisobotni HTML fayl sifatida yuboradi (brauzerda ochiladi, PDF qilib saqlash mumkin)."""
    file = BufferedInputFile(html_doc.encode("utf-8"), filename=f"biznes-tahlil-{project_id}.html")
    try:
        await bot.send_document(
            chat_id,
            file,
            caption="📄 <b>To'liq hisobot</b>\n\n"
            "Brauzerda oching — kerak bo'lsa «Chop etish → PDF» qilib saqlang.",
        )
    except TelegramAPIError:
        logger.warning("report_send_failed project=%s", project_id)


# ------------------------------------------------------------------ navbat


async def enqueue(ctx: AppContext, job: Job) -> None:
    async with ctx.session_factory() as session:
        session.add(
            PendingJob(
                telegram_id=job.tg_id,
                user_id=job.db_user_id,
                chat_id=job.chat_id,
                message_id=job.message_id,
                text=job.text,
                user_name=job.user_name,
                phase=job.phase,
                project_id=job.project_id,
                attempts=0,
                next_run_at=_now() + timedelta(seconds=RETRY_DELAYS[0]),
            )
        )
        await session.commit()


async def has_pending(ctx: AppContext, tg_id: int) -> bool:
    async with ctx.session_factory() as session:
        return (
            await session.scalar(select(PendingJob.id).where(PendingJob.telegram_id == tg_id).limit(1))
        ) is not None


async def process_due_jobs(bot: Bot, ctx: AppContext) -> int:
    """Vaqti kelgan navbatdagi ishlarni bajaradi. Qaytaradi: muvaffaqiyatli bajarilganlar soni."""
    async with ctx.session_factory() as session:
        rows = list(
            (
                await session.scalars(
                    select(PendingJob)
                    .where(PendingJob.next_run_at <= _now())
                    .order_by(PendingJob.id)
                    .limit(20)
                )
            ).all()
        )
    done = 0
    for row in rows:
        if not await ctx.user_locks.try_acquire(row.telegram_id):
            continue
        try:
            done += await _process_one(bot, ctx, row)
        finally:
            await ctx.user_locks.release(row.telegram_id)
    return done


async def _process_one(bot: Bot, ctx: AppContext, row: PendingJob) -> int:
    job = Job(
        chat_id=row.chat_id,
        message_id=row.message_id,
        tg_id=row.telegram_id,
        db_user_id=row.user_id,
        text=row.text,
        user_name=row.user_name or "",
        phase=row.phase,
        project_id=row.project_id,
    )
    try:
        await run_job(bot, ctx, job)
    except TRANSIENT_AI_ERRORS as exc:
        await _reschedule(bot, ctx, row.id, job, exc)
        return 0
    except AIServiceError as exc:
        await _remove(ctx, row.id)
        await _safe_send(bot, job.chat_id, exc.user_message)
        return 0
    except Exception as exc:  # noqa: BLE001 — bitta buzuq ish butun navbatni to'xtatmasin
        logger.exception("pending_job_crashed job=%d", row.id)
        await _reschedule(bot, ctx, row.id, job, exc)
        return 0
    await _remove(ctx, row.id)
    logger.info("pending_job_done user=%s attempts=%d", user_ref(job.tg_id), row.attempts + 1)
    return 1


async def _reschedule(bot: Bot, ctx: AppContext, job_id: int, job: Job, exc: BaseException) -> None:
    async with ctx.session_factory() as session:
        row = await session.get(PendingJob, job_id)
        if row is None:
            return
        row.attempts += 1
        row.phase, row.project_id = job.phase, job.project_id  # tahlil bosqichiga o'tgan bo'lishi mumkin
        if row.attempts >= len(RETRY_DELAYS):
            await session.delete(row)
            await session.commit()
            await _safe_send(
                bot,
                job.chat_id,
                "😔 Kechirasiz, AI xizmati hali ham ishlamayapti. Iltimos, birozdan keyin "
                "xabaringizni qayta yuboring.",
            )
            await alert_admins(
                bot,
                ctx,
                "ai_down",
                f"⚠️ AI bir necha urinishdan keyin ham javob bermadi: {type(exc).__name__}",
            )
            return
        row.next_run_at = _now() + timedelta(seconds=RETRY_DELAYS[row.attempts])
        await session.commit()
    logger.info("pending_job_retry user=%s error=%s", user_ref(job.tg_id), type(exc).__name__)


async def _remove(ctx: AppContext, job_id: int) -> None:
    async with ctx.session_factory() as session:
        await session.execute(delete(PendingJob).where(PendingJob.id == job_id))
        await session.commit()


async def _safe_send(bot: Bot, chat_id: int, text: str) -> None:
    try:
        await bot.send_message(chat_id, text)
    except TelegramAPIError:
        logger.info("job_notify_failed")


async def job_worker_loop(bot: Bot, ctx: AppContext, interval: float = 15.0) -> None:
    while True:
        try:
            await process_due_jobs(bot, ctx)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("job_worker_failed")
        await asyncio.sleep(interval)
