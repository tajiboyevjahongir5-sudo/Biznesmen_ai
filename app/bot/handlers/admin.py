"""Admin panel (faqat ADMIN_IDS dagi foydalanuvchilar uchun).

Buyruqlar:
/admin            — statistika va buyruqlar ro'yxati
/research         — kunlik izlanishni hozir ishga tushirish
/ideas            — qoralama g'oyalar (joylash / rad etish tugmalari bilan)
/publish <id>     — g'oyani kanalga joylash
/reject <id>      — g'oyani rad etish
/payments         — so'nggi to'lovlar
/confirm <id>     — to'lovni qo'lda tasdiqlash va havola yuborish
/block <tg_id>    — foydalanuvchini bloklash
/unblock <tg_id>  — blokdan chiqarish
/promo            — kanal reklamasini ko'rish
"""

from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.bot.context import AppContext
from app.bot.handlers.payment import deliver_access
from app.bot.helpers import promo_kb, promo_text, send_parts
from app.core.text import e, format_money
from app.database import repository as repo
from app.database.models import ChannelIdea, IdeaStatus
from app.services.ai_service import AIServiceError

logger = logging.getLogger(__name__)
router = Router(name="admin")

CB_PUBLISH = "adm:pub:"
CB_REJECT = "adm:rej:"


def _only_admins(event: Message | CallbackQuery, is_admin: bool = False) -> bool:
    """`is_admin` UserContextMiddleware (outer) tomonidan beriladi."""
    return is_admin


router.message.filter(_only_admins)
router.callback_query.filter(_only_admins)


def _idea_kb(idea_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Kanalga joylash", callback_data=f"{CB_PUBLISH}{idea_id}"),
                InlineKeyboardButton(text="Rad etish", callback_data=f"{CB_REJECT}{idea_id}"),
            ]
        ]
    )


async def send_idea_preview(bot: Bot, chat_id: int, idea: ChannelIdea) -> None:
    await bot.send_message(
        chat_id, f"<b>Qoralama #{idea.id}</b>\n\n{idea.post_text}", reply_markup=_idea_kb(idea.id)
    )


def _int_arg(command: CommandObject) -> int | None:
    arg = (command.args or "").strip().lstrip("#")
    return int(arg) if arg.isdigit() and len(arg) <= 15 else None


@router.message(Command("admin"))
async def admin_home(message: Message, ctx: AppContext) -> None:
    async with ctx.session_factory() as session:
        users = await repo.count_users(session)
        ideas = await repo.count_ideas(session)
    pay = await ctx.payments.stats()
    u = ctx.ai.usage
    s = ctx.settings
    cost = (
        f"${u.cost_usd:.4f}"
        if (s.openai_input_price_per_1m or s.openai_output_price_per_1m)
        else "narx sozlanmagan"
    )
    lines = [
        "<b>ADMIN PANEL — BIZNES MIND AI</b>",
        "",
        f"Foydalanuvchilar: <b>{users}</b>",
        f"G'oyalar: qoralama {ideas.get('draft', 0)}, joylangan {ideas.get('published', 0)}, "
        f"rad etilgan {ideas.get('rejected', 0)}",
        f"To'lovlar: to'langan {pay.get('paid', 0)}, kutilmoqda {pay.get('pending', 0)}, "
        f"muddati o'tgan {pay.get('expired', 0)}",
        f"Jami tushum: <b>{format_money(pay.get('paid_sum', 0))} so'm</b>",
        "",
        "<b>Bugungi AI sarfi</b>",
        f"So'rovlar: {u.global_requests}/{s.ai_daily_requests_global}",
        f"Tokenlar: kirish {u.input_tokens}, chiqish {u.output_tokens}",
        f"Taxminiy xarajat: {cost}",
        f"Internet qidiruvi: {'yoqilgan' if s.web_search_enabled else 'o`chirilgan'}",
        f"To'lov tizimi: {'sozlangan' if s.payments_configured else 'sozlanmagan'}",
        "",
        "<b>Buyruqlar</b>",
        "/research — izlanishni hozir boshlash",
        "/ideas — qoralama g'oyalar",
        "/publish id · /reject id",
        "/payments — so'nggi to'lovlar",
        "/confirm id — to'lovni qo'lda tasdiqlash",
        "/block tg_id · /unblock tg_id",
        "/promo — reklama ko'rinishi",
    ]
    await message.answer("\n".join(lines))


@router.message(Command("research"))
async def research_now(message: Message, ctx: AppContext, bot: Bot) -> None:
    await message.answer("Izlanish boshlandi. Bu bir necha daqiqa olishi mumkin.")
    try:
        created = await ctx.channel.research_and_create_drafts()
    except AIServiceError as exc:
        await message.answer(f"Izlanish bajarilmadi: {exc.user_message}")
        return
    if not created:
        await message.answer("Yangi g'oya topilmadi (yoki DAILY_IDEAS_COUNT=0).")
        return
    for idea in created:
        await send_idea_preview(bot, message.chat.id, idea)


@router.message(Command("ideas"))
async def list_drafts(message: Message, ctx: AppContext, bot: Bot) -> None:
    async with ctx.session_factory() as session:
        drafts = await repo.list_ideas(session, IdeaStatus.DRAFT, limit=10)
    if not drafts:
        await message.answer("Qoralama g'oyalar yo'q. Yangi izlanish: /research")
        return
    for idea in drafts:
        await send_idea_preview(bot, message.chat.id, idea)


async def _publish(bot: Bot, ctx: AppContext, idea_id: int) -> str:
    if not ctx.settings.ideas_channel_id:
        return "IDEAS_CHANNEL_ID sozlanmagan."
    try:
        idea = await ctx.channel.publish(bot, idea_id, ctx.bot_username)
    except Exception:  # noqa: BLE001
        logger.exception("publish_failed idea=%d", idea_id)
        return "Kanalga joylab bo'lmadi. Bot kanalda admin ekanini tekshiring."
    return f"G'oya #{idea_id} kanalga joylandi." if idea else "G'oya topilmadi yoki allaqachon joylangan."


@router.message(Command("publish"))
async def publish_cmd(message: Message, command: CommandObject, ctx: AppContext, bot: Bot) -> None:
    idea_id = _int_arg(command)
    if idea_id is None:
        await message.answer("Masalan: /publish 5")
        return
    await message.answer(await _publish(bot, ctx, idea_id))


@router.message(Command("reject"))
async def reject_cmd(message: Message, command: CommandObject, ctx: AppContext) -> None:
    idea_id = _int_arg(command)
    if idea_id is None:
        await message.answer("Masalan: /reject 5")
        return
    ok = await ctx.channel.reject(idea_id)
    await message.answer("Rad etildi." if ok else "Qoralama topilmadi.")


@router.callback_query(F.data.startswith(CB_PUBLISH))
async def publish_cb(cb: CallbackQuery, ctx: AppContext, bot: Bot) -> None:
    raw = cb.data.removeprefix(CB_PUBLISH)
    if not raw.isdigit():
        await cb.answer()
        return
    text = await _publish(bot, ctx, int(raw))
    await cb.answer(text, show_alert=True)
    if "joylandi" in text:
        await cb.message.edit_reply_markup(reply_markup=None)


@router.callback_query(F.data.startswith(CB_REJECT))
async def reject_cb(cb: CallbackQuery, ctx: AppContext) -> None:
    raw = cb.data.removeprefix(CB_REJECT)
    if not raw.isdigit():
        await cb.answer()
        return
    ok = await ctx.channel.reject(int(raw))
    await cb.answer("Rad etildi." if ok else "Qoralama topilmadi.")
    if ok:
        await cb.message.edit_reply_markup(reply_markup=None)


@router.message(Command("payments"))
async def payments_cmd(message: Message, ctx: AppContext) -> None:
    rows = await ctx.payments.list_recent()
    if not rows:
        await message.answer("Hali to'lovlar yo'q.")
        return
    names = {
        "pending": "kutilmoqda",
        "paid": "to'langan",
        "expired": "muddati o'tgan",
        "cancelled": "bekor qilingan",
    }
    lines = ["<b>So'nggi to'lovlar</b>", ""]
    for r in rows:
        by = f" ({e(r.confirmed_by)})" if r.confirmed_by else ""
        lines.append(
            f"#{r.id} · {format_money(r.amount)} so'm · {names.get(r.status, r.status)}{by} · "
            f"{r.created_at:%d.%m %H:%M} UTC"
        )
    await message.answer("\n".join(lines))


@router.message(Command("confirm"))
async def confirm_cmd(message: Message, command: CommandObject, ctx: AppContext, bot: Bot) -> None:
    rid = _int_arg(command)
    if rid is None:
        await message.answer("Masalan: /confirm 17 (to'lov raqami /payments ro'yxatida)")
        return
    r = await ctx.payments.admin_confirm(rid, message.from_user.id)
    if r is None:
        await message.answer("To'lov topilmadi yoki allaqachon tasdiqlangan.")
        return
    ok = await deliver_access(bot, ctx, r)
    await message.answer(
        f"To'lov #{rid} tasdiqlandi. " + ("Havola yuborildi." if ok else "Havola yuborilmadi.")
    )


async def _set_block(ctx: AppContext, tg_id: int, value: bool) -> bool:
    async with ctx.session_factory() as session:
        user = await repo.get_user_by_tg(session, tg_id)
        if user is None:
            return False
        user.is_blocked = value
        await session.commit()
        return True


@router.message(Command("block"))
async def block_cmd(message: Message, command: CommandObject, ctx: AppContext) -> None:
    tg_id = _int_arg(command)
    if tg_id is None or tg_id in ctx.settings.admin_ids:
        await message.answer("Masalan: /block 123456789")
        return
    await message.answer("Bloklandi." if await _set_block(ctx, tg_id, True) else "Foydalanuvchi topilmadi.")


@router.message(Command("unblock"))
async def unblock_cmd(message: Message, command: CommandObject, ctx: AppContext) -> None:
    tg_id = _int_arg(command)
    if tg_id is None:
        await message.answer("Masalan: /unblock 123456789")
        return
    await message.answer(
        "Blokdan chiqarildi." if await _set_block(ctx, tg_id, False) else "Foydalanuvchi topilmadi."
    )


@router.message(Command("promo"))
async def promo_cmd(message: Message, ctx: AppContext, bot: Bot) -> None:
    await send_parts(bot, message.chat.id, [promo_text(ctx)], reply_markup=promo_kb())
