"""Foydalanuvchi bilan tabiiy suhbat: menyusiz, oddiy xabarlar orqali."""

from __future__ import annotations

import logging
import re

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import Message
from aiogram.utils.chat_action import ChatActionSender

from app.bot.context import AppContext
from app.bot.helpers import is_channel_member, maybe_send_promo, send_parts
from app.core.logging import user_ref
from app.core.security import contains_sensitive_data, detect_prompt_injection, sanitize_user_text
from app.core.text import e, render_prompt
from app.database import repository as repo
from app.services.ai_service import AIServiceError
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
    lines += ["", "Promptni olish: /prompt raqam   O'chirish: /delete raqam"]
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
        await message.answer("Oldingi xabaringizni hali o'ylayapman, biroz kuting.")
        return
    try:
        async with ChatActionSender.typing(bot=bot, chat_id=message.chat.id):
            result = await ctx.orchestrator.handle_user_text(
                telegram_id=tg_id, user_db_id=db_user_id, text=text
            )
        await send_parts(bot, message.chat.id, result.messages)

        if result.looking_for_idea:
            await maybe_send_promo(bot, ctx, tg_id, message.chat.id)

        if result.run_analysis and result.project_id:
            await message.answer(
                "Endi bozor, talab va xavflarni tahlil qilaman. Bu 1-3 daqiqa vaqt olishi mumkin."
            )
            async with ChatActionSender.typing(bot=bot, chat_id=message.chat.id):
                parts = await ctx.orchestrator.run_analysis(
                    telegram_id=tg_id, user_db_id=db_user_id, project_id=result.project_id
                )
            await send_parts(bot, message.chat.id, parts)
            await message.answer(
                "Tahlil tugadi. Promptni keyin ham /history orqali topasiz. "
                "Boshqa g'oyangiz bo'lsa, shunchaki yozing — yangi suhbat boshlaymiz."
            )
            await maybe_send_promo(bot, ctx, tg_id, message.chat.id)
    except AIServiceError as exc:
        await message.answer(exc.user_message)
    finally:
        await ctx.user_locks.release(tg_id)


@router.message(F.text.startswith("/"))
async def unknown_command(message: Message) -> None:
    await message.answer("Bunday buyruq yo'q. Yordam uchun /help yuboring.")


@router.message()
async def non_text(message: Message) -> None:
    await message.answer("Hozircha faqat matnli xabarlarni tushunaman. G'oyangizni yozib yuboring.")
