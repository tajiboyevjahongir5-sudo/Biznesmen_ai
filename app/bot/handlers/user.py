"""Foydalanuvchi bilan tabiiy suhbat: menyusiz, oddiy xabarlar orqali."""

from __future__ import annotations

import io
import logging
import re

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import Message
from aiogram.utils.chat_action import ChatActionSender

from app.agents.analyst import MarketAnalysis
from app.bot.context import AppContext
from app.bot.helpers import is_channel_member, maybe_send_promo, send_documents
from app.bot.jobs import TRANSIENT_AI_ERRORS, Job, enqueue, has_pending, react, run_job, send_report
from app.bot.status import LISTENING_TEXT, drop_status, replace_status, send_status
from app.core.finance import build_model
from app.core.logging import user_ref
from app.core.prompt_file import prompt_document
from app.core.report import build_report_html
from app.core.security import contains_sensitive_data, detect_prompt_injection, sanitize_user_text
from app.core.text import SEP, e
from app.database import repository as repo
from app.services.ai_service import AIServiceError
from app.services.channel_service import DEEP_LINK_PREFIX

logger = logging.getLogger(__name__)
router = Router(name="user")

HELP_TEXT = (
    "🧠 <b>BIZNES MIND AI</b>\n"
    "━━━━━━━━━━━━━━━━\n"
    "Biznes g'oyangizni chuqur o'rganadigan shaxsiy maslahatchingiz.\n\n"
    "⚙️ <b>Qanday ishlaydi?</b>\n"
    "1️⃣ G'oyangizni oddiy so'zlar bilan yozing yoki 🎙 aytib yuboring\n"
    "2️⃣ Men birma-bir aniqlashtiruvchi savollar beraman\n"
    "3️⃣ Talab, raqobat, xavflar va moliyaviy modelni tahlil qilaman\n"
    "4️⃣ Oxirida istalgan AI da ishlatish uchun tayyor 🤖 <b>SUPER PROMPT</b> beraman\n"
    "5️⃣ G'oya bot, ilova yoki sayt bo'lsa — dasturchi AI uchun 🛠 <b>TEXNIK PROMPT</b> ham "
    "(kod, xavfsizlik, UI/UX, animatsiya)\n\n"
    "📋 <b>Buyruqlar</b>\n"
    "🆕 /new — yangi g'oya\n"
    "⏹ /cancel — joriy suhbatni to'xtatish\n"
    "📂 /history — loyihalaringiz\n"
    "🤖 /prompt <i>raqam</i> — to'liq prompt faylini qayta olish\n"
    "📄 /report <i>raqam</i> — to'liq hisobot fayli\n"
    "🗑 /delete <i>raqam</i> — loyihani o'chirish\n"
    "💎 /kanal — kunlik g'oyalar kanali\n\n"
    "🔒 <i>Hech qachon karta raqami, parol yoki SMS kodni yozmang — ular kerak emas.</i>"
)


def _greeting(name: str | None) -> str:
    who = f", <b>{e(name)}</b>" if name else ""
    return (
        f"👋 Assalomu alaykum{who}!\n\n"
        "Men <b>BIZNES MIND AI</b> — shaxsiy biznes maslahatchingizman 💼\n\n"
        "Qanday biznes boshlamoqchisiz yoki qaysi g'oyani tekshirib ko'rmoqchisiz? "
        "Bir-ikki gap bilan yozing (yoki 🎙 aytib yuboring) — qolganini savollar orqali "
        "birga aniqlaymiz.\n\n"
        "💡 <i>Masalan: «Chilonzorda bolalar uchun robototexnika to'garagi ochmoqchiman»</i>\n\n"
        "Hali aniq g'oya bo'lmasa ham mayli — qiziqishlaringizni ayting, yo'nalish topamiz. "
        "Barcha imkoniyatlar: /help"
    )


@router.message(CommandStart(deep_link=True, magic=F.args.regexp(rf"^{DEEP_LINK_PREFIX}\d{{1,9}}$")))
async def start_idea_link(message: Message, command: CommandObject, ctx: AppContext, bot: Bot) -> None:
    """Kanal postidagi tugma bosilganda: o'sha g'oyaning promptini beradi."""
    idea_id = int(command.args.removeprefix(DEEP_LINK_PREFIX))
    tg_id = message.from_user.id
    if ctx.settings.ideas_channel_id and not await is_channel_member(bot, ctx, tg_id):
        await message.answer("🔒 Bu g'oya prompti faqat kanal a'zolari uchun. Qo'shilish shartlari:")
        await maybe_send_promo(bot, ctx, tg_id, message.chat.id, force=True)
        return
    idea = await ctx.channel.get_prompt_for_request(idea_id)
    if idea is None:
        await message.answer("🔍 Bu g'oya topilmadi yoki hali e'lon qilinmagan.")
        return
    logger.info("idea_prompt_sent user=%s idea=%d", user_ref(tg_id), idea_id)
    document = prompt_document(
        f"goya-{idea_id}",
        title=idea.title,
        prompt=idea.prompt_text,
        how_to_use="Bu prompt kanaldagi g'oya asosida tayyorlangan. Uni o'z shahringiz, "
        "byudjetingiz va tajribangizga moslab to'ldiring.",
        source=f"Kanal g'oyasi #{idea_id}",
    )
    await send_documents(bot, message.chat.id, [document])
    await message.answer(
        "💬 Shu g'oyani o'zingizga moslab chuqurroq tahlil qilmoqchimisiz? "
        "Uni o'z so'zlaringiz bilan yozing — savollar bilan boshlaymiz."
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
    await message.answer(
        "🆕 Yaxshi, yangi loyihani boshlaymiz!\n\nBiznes g'oyangizni yozing yoki 🎙 aytib yuboring."
    )


@router.message(Command("cancel"))
async def cancel_cmd(message: Message, ctx: AppContext, db_user_id: int) -> None:
    if await ctx.orchestrator.cancel_active(db_user_id):
        await message.answer(
            "⏹ Joriy suhbat to'xtatildi.\n\nYangi g'oya bo'lsa, istalgan payt yozing — men shu yerdaman."
        )
    else:
        await message.answer("ℹ️ Hozir faol suhbat yo'q. G'oyangizni yozing — boshlaymiz!")


@router.message(Command("history"))
async def history_cmd(message: Message, ctx: AppContext, db_user_id: int) -> None:
    async with ctx.session_factory() as session:
        projects = await repo.list_projects(session, db_user_id)
    if not projects:
        await message.answer(
            "📂 Sizda hali loyihalar yo'q.\n\nG'oyangizni yozing — birinchi loyihani boshlaymiz! 🚀"
        )
        return
    status_names = {
        "interviewing": ("💬", "suhbat davom etmoqda"),
        "analyzing": ("⚙️", "tahlil qilinmoqda"),
        "done": ("✅", "tayyor"),
        "failed": ("❌", "xato"),
        "followup": ("📈", "kuzatuvda"),
        "cancelled": ("⏹", "bekor qilingan"),
    }
    lines = ["📂 <b>LOYIHALARINGIZ</b>", SEP]
    for p in projects:
        icon, name = status_names.get(p.status, ("▫️", p.status))
        lines += ["", f"{icon} <b>#{p.id}</b> · {e(p.title[:90])}"]
        lines.append(f"<i>{name} · {p.created_at:%d.%m.%Y}</i>")
    lines += ["", "🤖 /prompt <i>raqam</i> · 📄 /report <i>raqam</i> · 🗑 /delete <i>raqam</i>"]
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
        await message.answer(
            "ℹ️ Loyiha raqamini yozing, masalan: <code>/prompt 12</code>\nRaqamlar — /history da."
        )
        return
    async with ctx.session_factory() as session:
        project = await repo.get_project(session, db_user_id, project_id)
        text = await repo.latest_prompt(session, db_user_id, project_id)
        tech = await repo.latest_tech_prompt(session, db_user_id, project_id)
    if project is None or not text:
        await message.answer("🔍 Bu raqamli tayyor loyiha topilmadi. Ro'yxat — /history")
        return
    document = prompt_document(
        project_id,
        title=project.title or f"Loyiha #{project_id}",
        prompt=text,
        tech_prompt=tech or "",
        source=f"Loyiha #{project_id}",
    )
    await send_documents(bot, message.chat.id, [document])


@router.message(Command("report"))
async def report_cmd(
    message: Message, command: CommandObject, ctx: AppContext, db_user_id: int, bot: Bot
) -> None:
    """Tayyor loyihaning to'liq hisobotini HTML fayl qilib qayta yuboradi."""
    project_id, _ = _parse_id(command.args)
    if project_id is None:
        await message.answer(
            "ℹ️ Loyiha raqamini yozing, masalan: <code>/report 12</code>\nRaqamlar — /history da."
        )
        return
    async with ctx.session_factory() as session:
        project = await repo.get_project(session, db_user_id, project_id)
        analysis_json = await repo.latest_analysis(session, db_user_id, project_id)
        prompt = await repo.latest_prompt(session, db_user_id, project_id)
        tech = await repo.latest_tech_prompt(session, db_user_id, project_id)
    if project is None or not analysis_json or not prompt:
        await message.answer("🔍 Bu raqamli tayyor loyiha topilmadi. Ro'yxat — /history")
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
        tech_prompt_text=tech or "",
    )
    await send_report(bot, message.chat.id, project_id, html_doc)


@router.message(Command("delete"))
async def delete_cmd(message: Message, command: CommandObject, ctx: AppContext, db_user_id: int) -> None:
    project_id, confirm = _parse_id(command.args)
    if project_id is None:
        await message.answer("ℹ️ Loyiha raqamini yozing, masalan: <code>/delete 12</code>")
        return
    if confirm != "ha":
        await message.answer(
            f"🗑 <b>Loyiha #{project_id} o'chirilsinmi?</b>\n\n"
            "Unga tegishli barcha suhbat, tahlil va promptlar butunlay o'chiriladi. "
            "⚠️ Buni qaytarib bo'lmaydi.\n\n"
            f"Tasdiqlash uchun yuboring: <code>/delete {project_id} ha</code>"
        )
        return
    async with ctx.session_factory() as session:
        ok = await repo.delete_project(session, db_user_id, project_id)
        await session.commit()
    await message.answer("✅ Loyiha butunlay o'chirildi." if ok else "🔍 Bu raqamli loyiha topilmadi.")


@router.message(Command("kanal"))
async def channel_cmd(message: Message, ctx: AppContext, bot: Bot) -> None:
    if not ctx.settings.payments_configured:
        await message.answer("💎 G'oyalar kanali hozircha ishga tushirilmagan. Tez orada!")
        return
    if await is_channel_member(bot, ctx, message.from_user.id):
        await message.answer("✅ Siz allaqachon kanal a'zosisiz.")
        return
    await maybe_send_promo(bot, ctx, message.from_user.id, message.chat.id, force=True)


@router.message(F.text & ~F.text.startswith("/"))
async def conversation(message: Message, ctx: AppContext, db_user_id: int, bot: Bot) -> None:
    """Asosiy suhbat: har qanday oddiy matn — g'oya yoki savolga javob."""
    await _process_text(message, ctx, db_user_id, bot, message.text or "")


@router.message(F.voice | F.audio | F.video_note)
async def voice_message(message: Message, ctx: AppContext, db_user_id: int, bot: Bot) -> None:
    """Ovozli xabar: Gemini matnga aylantiradi, keyin oddiy matn kabi davom etadi."""
    media = message.voice or message.audio or message.video_note
    s = ctx.settings
    if s.ai_provider != "gemini":
        await message.answer("🎙 Ovozli xabarlar hozircha qo'llab-quvvatlanmaydi. Iltimos, matn bilan yozing.")
        return
    if (media.duration or 0) > s.max_voice_seconds:
        await message.answer(
            f"🎙 Ovozli xabar juda uzun. Iltimos, {s.max_voice_seconds // 60} daqiqagacha yuboring "
            "yoki matn bilan yozing."
        )
        return
    if (media.file_size or 0) > MAX_VOICE_BYTES:
        await message.answer("🎙 Fayl juda katta. Qisqaroq ovozli xabar yuboring.")
        return
    tg_id = message.from_user.id
    if not await ctx.user_locks.try_acquire(tg_id):
        await message.answer("⏳ Oldingi xabaringiz ustida ishlayapman, biroz kuting.")
        return
    status_id = None
    try:
        await react(bot, message.chat.id, message.message_id, "👀")
        status_id = await send_status(bot, message.chat.id, LISTENING_TEXT)
        async with ChatActionSender.typing(bot=bot, chat_id=message.chat.id):
            buf = await bot.download(media, destination=io.BytesIO())
            mime = getattr(media, "mime_type", None) or ("video/mp4" if message.video_note else "audio/ogg")
            text = await ctx.ai.transcribe(user_id=tg_id, audio=buf.getvalue(), mime=mime)
    except TRANSIENT_AI_ERRORS:
        await drop_status(bot, message.chat.id, status_id)
        await message.answer("⏳ AI hozir band, ovozni tinglay olmadim. Bir daqiqadan keyin qayta yuboring.")
        return
    except AIServiceError as exc:
        await drop_status(bot, message.chat.id, status_id)
        await message.answer(exc.user_message)
        return
    except TelegramAPIError:
        await drop_status(bot, message.chat.id, status_id)
        await message.answer("🎙 Ovozli xabarni yuklab bo'lmadi. Qayta yuboring.")
        return
    finally:
        await ctx.user_locks.release(tg_id)
    if len(text) < 2:
        await drop_status(bot, message.chat.id, status_id)
        await message.answer("🎙 Ovozdan gap eshitilmadi. Iltimos, qayta yozib yuboring.")
        return
    logger.info("voice_transcribed user=%s chars=%d", user_ref(tg_id), len(text))
    echo = f"🎙 <i>{e(text[:600])}{'…' if len(text) > 600 else ''}</i>"
    await replace_status(bot, message.chat.id, status_id, [echo])  # «Tinglayapman» → aytilgan matn
    await _process_text(message, ctx, db_user_id, bot, text)


MAX_VOICE_BYTES = 15 * 1024 * 1024


async def _process_text(message: Message, ctx: AppContext, db_user_id: int, bot: Bot, raw: str) -> None:
    tg_id = message.from_user.id
    s = ctx.settings
    check = sanitize_user_text(raw, min_chars=2, max_chars=s.max_input_chars)
    if not check.ok:
        await message.answer(check.error)
        return
    text = check.text
    if contains_sensitive_data(text):
        await message.answer(
            "🔒 <b>Xavfsizlik ogohlantirishi</b>\n\n"
            "Xabaringizda karta raqami, parol yoki maxfiy kodga o'xshash ma'lumot bor — "
            "uni saqlamadim. Iltimos, xabarni bunday ma'lumotlarsiz qayta yuboring."
        )
        return
    if detect_prompt_injection(text):
        logger.warning("prompt_injection_pattern user=%s", user_ref(tg_id))

    if not await ctx.user_locks.try_acquire(tg_id):
        await message.answer("⏳ Oldingi xabaringiz ustida ishlayapman, biroz kuting.")
        return
    try:
        if await has_pending(ctx, tg_id):
            await message.answer(
                "⏳ Oldingi xabaringiz navbatda turibdi — AI bo'shashi bilan javob beraman. "
                "Biroz kuting, keyin davom etamiz."
            )
            return
        job = Job(
            chat_id=message.chat.id,
            message_id=message.message_id,
            tg_id=tg_id,
            db_user_id=db_user_id,
            text=text,
            user_name=(message.from_user.first_name or "")[:40],
        )
        await react(bot, job.chat_id, job.message_id, "👀")  # "o'qidim, o'ylayapman"
        try:
            await run_job(bot, ctx, job)
        except TRANSIENT_AI_ERRORS as exc:
            # AI vaqtincha ishlamayapti: xabarni bazadagi navbatga olamiz, javobni keyin o'zimiz beramiz.
            await enqueue(ctx, job)
            logger.info("ai_job_queued user=%s error=%s", user_ref(tg_id), type(exc).__name__)
            await message.answer(
                "⏳ AI hozir juda band. Xabaringizni <b>navbatga qo'ydim</b> — bir necha daqiqada "
                "javobni o'zim yuboraman, qayta yozish shart emas."
            )
        except AIServiceError as exc:
            await message.answer(exc.user_message)
    finally:
        await ctx.user_locks.release(tg_id)


@router.message(F.text.startswith("/"))
async def unknown_command(message: Message) -> None:
    await message.answer("🤔 Bunday buyruq yo'q. Barcha imkoniyatlar — /help")


@router.message()
async def non_text(message: Message) -> None:
    await message.answer(
        "📎 Men matn va 🎙 ovozli xabarlarni tushunaman. G'oyangizni yozing yoki aytib yuboring."
    )
