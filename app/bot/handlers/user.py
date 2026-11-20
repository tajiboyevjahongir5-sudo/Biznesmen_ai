"""Foydalanuvchi bilan tabiiy suhbat: menyusiz, oddiy xabarlar orqali."""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import BufferedInputFile, Message, ReactionTypeEmoji
from aiogram.utils.chat_action import ChatActionSender

from app.agents.analyst import MarketAnalysis
from app.bot.context import AppContext
from app.bot.helpers import alert_admins, is_channel_member, maybe_send_promo, send_parts
from app.core.finance import build_model
from app.core.logging import user_ref
from app.core.report import build_report_html
from app.core.security import contains_sensitive_data, detect_prompt_injection, sanitize_user_text
from app.core.text import e, render_prompt
from app.database import repository as repo
from app.services.ai_service import (
    AIBadResponseError,
    AIBusyError,
    AIRateLimitedError,
    AIServiceError,
    AITimeoutError,
    AIUnavailableError,
)
from app.services.channel_service import DEEP_LINK_PREFIX

logger = logging.getLogger(__name__)
router = Router(name="user")

HELP_TEXT = (
    "<b>BIZNES MIND AI</b> — biznes g'oyangizni chuqur o'rganadigan maslahatchi.\n\n"
    "Menga shunchaki g'oyangizni oddiy so'zlar bilan yozing. Men sizga birma-bir savollar "
    "beraman, so'ng talab, raqobat va muvaffaqiyatsizlik sabablarini tahlil qilib, oxirida "
    "boshqa AI larda ishlatish uchun tayyor SUPER PROMPT beraman.\n\n"
    "Foydali buyruqlar:\n"
    "/new — yangi g'oyani boshlash\n"
    "/cancel — joriy suhbatni to'xtatish\n"
    "/history — oldingi loyihalaringiz\n"
    "/prompt <i>raqam</i> — loyiha promptini qayta olish\n"
    "/report <i>raqam</i> — to'liq hisobot fayli\n"
    "/delete <i>raqam</i> — loyihani o'chirish\n"
    "/kanal — g'oyalar kanali haqida\n\n"
    "<i>Hech qachon karta raqami, parol yoki SMS kodni yozmang — ular kerak emas.</i>"
)


def _greeting(name: str | None) -> str:
    who = f", {e(name)}" if name else ""
    return (
        f"Assalomu alaykum{who}. Men BIZNES MIND AI — biznes maslahatchingizman.\n\n"
        "Qanday biznes boshlamoqchisiz yoki qaysi g'oyani tekshirib ko'rmoqchisiz? "
        "Bir-ikki gap bilan yozing, qolganini savollar orqali birga aniqlaymiz.\n\n"
        "Agar hali aniq g'oyangiz bo'lmasa, shuni ayting — qaysi yo'nalishga qarash mumkinligini "
        "gaplashamiz."
    )


@router.message(CommandStart(deep_link=True, magic=F.args.regexp(rf"^{DEEP_LINK_PREFIX}\d{{1,9}}$")))
async def start_idea_link(message: Message, command: CommandObject, ctx: AppContext, bot: Bot) -> None:
    """Kanal postidagi tugma bosilganda: o'sha g'oyaning promptini beradi."""
    idea_id = int(command.args.removeprefix(DEEP_LINK_PREFIX))
    tg_id = message.from_user.id
    if ctx.settings.ideas_channel_id and not await is_channel_member(bot, ctx, tg_id):
        await message.answer("Bu g'oya promptlari faqat kanal a'zolari uchun. Kanalga qo'shilish shartlari:")
        await maybe_send_promo(bot, ctx, tg_id, message.chat.id, force=True)
        return
    idea = await ctx.channel.get_prompt_for_request(idea_id)
    if idea is None:
        await message.answer("Bu g'oya topilmadi yoki hali e'lon qilinmagan.")
        return
    logger.info("idea_prompt_sent user=%s idea=%d", user_ref(tg_id), idea_id)
    await send_parts(
        bot,
        message.chat.id,
        render_prompt(
            idea.title,
            idea.prompt_text,
            "Bu prompt kanaldagi g'oya asosida tayyorlangan. Uni o'z shahringiz, "
            "byudjetingiz va tajribangizga moslab to'ldiring.",
        ),
    )
    await message.answer(
        "Agar shu g'oyani o'zingizga moslab chuqurroq tahlil qilmoqchi bo'lsangiz, "
        "uni o'z so'zlaringiz bilan yozing — savollar bilan boshlaymiz."
    )


@router.message(CommandStart())
async def start(message: Message) -> None:
    await message.answer(_greeting(message.from_user.first_name))


@router.message(Command("help"))
async def help_cmd(message: Message) -> None:
    await message.answer(HELP_TEXT)


@router.message(Command("new"))
async def new_cmd(message: Message, ctx: AppContext, db_user_id: int) -> None:
    await ctx.orchestrator.cancel_active(db_user_id)
    await message.answer("Yaxshi, yangi loyihani boshlaymiz. Biznes g'oyangizni yozing.")


@router.message(Command("cancel"))
async def cancel_cmd(message: Message, ctx: AppContext, db_user_id: int) -> None:
    if await ctx.orchestrator.cancel_active(db_user_id):
        await message.answer("Joriy suhbat to'xtatildi. Yangi g'oya bo'lsa, istalgan payt yozing.")
    else:
        await message.answer("Hozir faol suhbat yo'q.")


@router.message(Command("history"))
async def history_cmd(message: Message, ctx: AppContext, db_user_id: int) -> None:
    async with ctx.session_factory() as session:
        projects = await repo.list_projects(session, db_user_id)
    if not projects:
        await message.answer("Sizda hali loyihalar yo'q. G'oyangizni yozing — boshlaymiz.")
        return
    status_names = {
        "interviewing": "suhbat davom etmoqda",
        "analyzing": "tahlil qilinmoqda",
        "done": "tayyor",
        "failed": "xato",
        "cancelled": "bekor qilingan",
    }
    lines = ["<b>Loyihalaringiz</b>", ""]
    for p in projects:
        lines.append(
            f"<b>#{p.id}</b> {e(p.title[:90])}\n"
            f"Holat: {status_names.get(p.status, p.status)} · {p.created_at:%d.%m.%Y}"
        )
    lines += ["", "Prompt: /prompt raqam · Hisobot: /report raqam · O'chirish: /delete raqam"]
    await message.answer("\n".join(lines))


def _parse_id(args: str | None) -> tuple[int | None, str]:
    if not args:
        return None, ""
    m = re.fullmatch(r"\s*#?(\d{1,9})\s*(\S*)\s*", args)
    if not m:
        return None, ""
    return int(m.group(1)), m.group(2).lower()


@router.message(Command("prompt"))
async def prompt_cmd(
    message: Message, command: CommandObject, ctx: AppContext, db_user_id: int, bot: Bot
) -> None:
    project_id, _ = _parse_id(command.args)
    if project_id is None:
        await message.answer("Loyiha raqamini yozing, masalan: /prompt 12")
        return
    async with ctx.session_factory() as session:
        text = await repo.latest_prompt(session, db_user_id, project_id)
    if not text:
        await message.answer("Bu raqamli tayyor loyiha topilmadi.")
        return
    await send_parts(bot, message.chat.id, render_prompt(f"Loyiha #{project_id}", text, ""))


@router.message(Command("report"))
async def report_cmd(
    message: Message, command: CommandObject, ctx: AppContext, db_user_id: int, bot: Bot
) -> None:
    """Tayyor loyihaning to'liq hisobotini HTML fayl qilib qayta yuboradi."""
    project_id, _ = _parse_id(command.args)
    if project_id is None:
        await message.answer("Loyiha raqamini yozing, masalan: /report 12 (raqamlar — /history da)")
        return
    async with ctx.session_factory() as session:
        project = await repo.get_project(session, db_user_id, project_id)
        analysis_json = await repo.latest_analysis(session, db_user_id, project_id)
        prompt = await repo.latest_prompt(session, db_user_id, project_id)
    if project is None or not analysis_json or not prompt:
        await message.answer("Bu raqamli tayyor loyiha topilmadi.")
        return
    try:
        analysis = MarketAnalysis.model_validate_json(analysis_json)
    except ValueError:
        await message.answer("Bu loyiha hisobotini tiklab bo'lmadi.")
        return
    finance = build_model(analysis.finance)
    html_doc = build_report_html(
        project_id=project_id,
        title=project.title or project.idea[:80],
        analysis=analysis,
        sources=[],
        web_used=False,
        finance=finance if finance.usable else None,
        prompt_title=project.title or f"Loyiha #{project_id}",
        prompt_text=prompt,
    )
    await send_report(bot, message.chat.id, project_id, html_doc)


@router.message(Command("delete"))
async def delete_cmd(message: Message, command: CommandObject, ctx: AppContext, db_user_id: int) -> None:
    project_id, confirm = _parse_id(command.args)
    if project_id is None:
        await message.answer("Loyiha raqamini yozing, masalan: /delete 12")
        return
    if confirm != "ha":
        await message.answer(
            f"Loyiha #{project_id} va unga tegishli barcha suhbat, tahlil va promptlar "
            f"butunlay o'chiriladi. Buni qaytarib bo'lmaydi.\n\n"
            f"Tasdiqlash uchun yuboring: <code>/delete {project_id} ha</code>"
        )
        return
    async with ctx.session_factory() as session:
        ok = await repo.delete_project(session, db_user_id, project_id)
        await session.commit()
    await message.answer("Loyiha butunlay o'chirildi." if ok else "Bu raqamli loyiha topilmadi.")


@router.message(Command("kanal"))
async def channel_cmd(message: Message, ctx: AppContext, bot: Bot) -> None:
    if not ctx.settings.payments_configured:
        await message.answer("G'oyalar kanali hozircha ishga tushirilmagan.")
        return
    if await is_channel_member(bot, ctx, message.from_user.id):
        await message.answer("Siz allaqachon kanal a'zosisiz.")
        return
    await maybe_send_promo(bot, ctx, message.from_user.id, message.chat.id, force=True)


@router.message(F.text & ~F.text.startswith("/"))
async def conversation(message: Message, ctx: AppContext, db_user_id: int, bot: Bot) -> None:
    """Asosiy suhbat: har qanday oddiy matn — g'oya yoki savolga javob."""
    tg_id = message.from_user.id
    s = ctx.settings
    check = sanitize_user_text(message.text, min_chars=2, max_chars=s.max_input_chars)
    if not check.ok:
        await message.answer(check.error)
        return
    text = check.text
    if contains_sensitive_data(text):
        await message.answer(
            "Xabaringizda karta raqami, parol yoki maxfiy kodga o'xshash ma'lumot bor. "
            "Xavfsizlik uchun uni saqlamadim. Iltimos, bunday ma'lumotlarni yozmang va "
            "xabarni ularsiz qayta yuboring."
        )
        return
    if detect_prompt_injection(text):
        logger.warning("prompt_injection_pattern user=%s", user_ref(tg_id))

    if not await ctx.user_locks.try_acquire(tg_id):
        await message.answer("⏳ Oldingi xabaringiz ustida ishlayapman, biroz kuting.")
        return
    job = _Job(message, tg_id, db_user_id, text, (message.from_user.first_name or "")[:40])
    queued = False
    try:
        await _react(message, "👀")  # "o'qidim, o'ylayapman"
        await _run_job(bot, ctx, job)
    except TRANSIENT_AI_ERRORS as exc:
        # AI vaqtincha ishlamayapti: xabarni navbatga olamiz va keyin o'zimiz javob beramiz.
        await message.answer(
            "⏳ AI hozir juda band. Xabaringizni <b>navbatga qo'ydim</b> — bir necha daqiqada "
            "javobni o'zim yuboraman, qayta yozish shart emas."
        )
        logger.info("ai_job_queued user=%s error=%s", user_ref(tg_id), type(exc).__name__)
        task = asyncio.create_task(_retry_job_later(bot, ctx, job))
        _background.add(task)
        task.add_done_callback(_background.discard)
        queued = True  # qulfni fon vazifasi o'zi bo'shatadi
    except AIServiceError as exc:
        await message.answer(exc.user_message)
    finally:
        if not queued:
            await ctx.user_locks.release(tg_id)


# AI vaqtincha band/sekin bo'lganda qayta urinish mantiqli bo'lgan xatolar.
TRANSIENT_AI_ERRORS = (
    AIBusyError,
    AITimeoutError,
    AIUnavailableError,
    AIRateLimitedError,
    AIBadResponseError,
)
RETRY_DELAYS = (60, 120, 240)  # soniya
_background: set[asyncio.Task] = set()


@dataclass
class _Job:
    message: Message
    tg_id: int
    db_user_id: int
    text: str
    user_name: str
    phase: str = "turn"  # turn | analysis
    project_id: int | None = None


async def _run_job(bot: Bot, ctx: AppContext, job: _Job) -> None:
    """Suhbat qadamini (va kerak bo'lsa tahlilni) bajaradi. AI xatolarini yuqoriga uzatadi."""
    chat_id = job.message.chat.id
    if job.phase == "turn":
        async with ChatActionSender.typing(bot=bot, chat_id=chat_id):
            result = await ctx.orchestrator.handle_user_text(
                telegram_id=job.tg_id, user_db_id=job.db_user_id, text=job.text, user_name=job.user_name
            )
        if result.reaction:
            await _react(job.message, result.reaction)
        await send_parts(bot, chat_id, result.messages)
        if result.looking_for_idea:
            await maybe_send_promo(bot, ctx, job.tg_id, chat_id)
        if not (result.run_analysis and result.project_id):
            return
        job.phase, job.project_id = "analysis", result.project_id
        await bot.send_message(
            chat_id,
            "📊 Endi bozor, talab, raqobat va moliyani chuqur tahlil qilaman.\n"
            "⏳ Bu 1-3 daqiqa vaqt olishi mumkin.",
        )
    await _run_analysis(bot, ctx, job)


async def _run_analysis(bot: Bot, ctx: AppContext, job: _Job) -> None:
    chat_id = job.message.chat.id
    async with ChatActionSender.typing(bot=bot, chat_id=chat_id):
        output = await ctx.orchestrator.run_analysis(
            telegram_id=job.tg_id, user_db_id=job.db_user_id, project_id=job.project_id
        )
    await send_parts(bot, chat_id, output.parts)
    if output.report_html:
        await send_report(bot, chat_id, job.project_id, output.report_html)
    await bot.send_message(
        chat_id,
        "✅ Tahlil tugadi! Promptni keyin ham /history orqali topasiz.\n\n"
        "💡 Boshqa g'oyangiz bo'lsa, shunchaki yozing — yangi suhbat boshlaymiz.",
    )
    await maybe_send_promo(bot, ctx, job.tg_id, chat_id)


async def send_report(bot: Bot, chat_id: int, project_id: int, html_doc: str) -> None:
    """To'liq hisobotni HTML fayl sifatida yuboradi (brauzerda ochiladi, PDF qilib saqlash mumkin)."""
    file = BufferedInputFile(html_doc.encode("utf-8"), filename=f"biznes-tahlil-{project_id}.html")
    try:
        await bot.send_document(
            chat_id,
            file,
            caption="📄 To'liq hisobot. Brauzerda oching — kerak bo'lsa «Chop etish → PDF» qilib saqlang.",
        )
    except TelegramAPIError:
        logger.warning("report_send_failed project=%s", project_id)


async def _retry_job_later(bot: Bot, ctx: AppContext, job: _Job) -> None:
    """Navbatdagi xabarni bir necha marta kechiktirib qayta ishlaydi; oxirida qulfni bo'shatadi."""
    chat_id = job.message.chat.id
    last: BaseException | None = None
    try:
        for delay in RETRY_DELAYS:
            await asyncio.sleep(delay)
            try:
                await _run_job(bot, ctx, job)
                logger.info("ai_job_done_after_retry user=%s", user_ref(job.tg_id))
                return
            except TRANSIENT_AI_ERRORS as exc:
                last = exc
                logger.info("ai_job_retry_failed user=%s error=%s", user_ref(job.tg_id), type(exc).__name__)
            except AIServiceError as exc:
                await bot.send_message(chat_id, exc.user_message)
                return
        await bot.send_message(
            chat_id,
            "😔 Kechirasiz, AI xizmati hali ham ishlamayapti. Iltimos, birozdan keyin "
            "xabaringizni qayta yuboring.",
        )
        await alert_admins(
            bot, ctx, "ai_down", f"⚠️ AI bir necha urinishdan keyin ham javob bermadi: {type(last).__name__}"
        )
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 — fon vazifasi botni to'xtatmasin
        logger.exception("ai_job_retry_crashed")
    finally:
        await ctx.user_locks.release(job.tg_id)


async def _react(message: Message, emoji: str) -> None:
    """Xabarga reaksiya qo'yadi; Telegram rad etsa (masalan, reaksiyalar o'chirilgan) — jim o'tadi."""
    try:
        await message.react([ReactionTypeEmoji(emoji=emoji)])
    except Exception:  # noqa: BLE001 — reaksiya ixtiyoriy bezak, suhbatni to'xtatmasin
        logger.debug("reaction_failed", exc_info=True)


@router.message(F.text.startswith("/"))
async def unknown_command(message: Message) -> None:
    await message.answer("Bunday buyruq yo'q. Yordam uchun /help yuboring.")


@router.message()
async def non_text(message: Message) -> None:
    await message.answer("Hozircha faqat matnli xabarlarni tushunaman. G'oyangizni yozib yuboring.")
