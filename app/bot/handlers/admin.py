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
/settings         — kanal, karta, narx sozlamalari (DB da saqlanadi)
"""

from __future__ import annotations

import contextlib
import logging
import re
from datetime import timedelta

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.bot.context import AppContext
from app.bot.handlers.payment import deliver_access
from app.bot.helpers import promo_kb, promo_text, send_parts
from app.core.text import e, format_money
from app.database import repository as repo
from app.database.models import ChannelIdea, IdeaStatus
from app.services.ai_service import AIServiceError
from app.services.runtime_settings import SettingError, mask_card

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
        "/aitest — AI kaliti va modelini tekshirish",
        "/research — izlanishni hozir boshlash",
        "/ideas — qoralama g'oyalar",
        "/publish id · /reject id",
        "/payments — so'nggi to'lovlar",
        "/confirm id — to'lovni qo'lda tasdiqlash",
        "/block tg_id · /unblock tg_id",
        "/promo — reklama ko'rinishi",
        "/settings — kanal, karta, narx va boshqa sozlamalar",
    ]
    await message.answer("\n".join(lines))


@router.message(Command("aitest"))
async def aitest_cmd(message: Message, ctx: AppContext) -> None:
    """AI kaliti va modelini tekshiradi, mavjud model nomlarini ko'rsatadi."""
    await message.answer("AI ulanishini tekshiryapman...")
    lines = await ctx.ai.diagnose()
    await message.answer("<b>AI DIAGNOSTIKA</b>\n\n" + "\n".join(e(x) for x in lines))


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
        return "G'oyalar kanali ulanmagan: /link_ideas"
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


# ------------------------------------------------------------------ sozlamalar

SETTINGS_HELP = (
    "<b>Sozlash buyruqlari</b>\n"
    "/link_ideas — g'oyalar kanalini ulash (kod beriladi)\n"
    "/link_payment — to'lov kanalini ulash (kod beriladi)\n"
    "/card 8600123412341234 — karta raqami\n"
    "/card_holder Ism Familiya — karta egasi\n"
    "/price 50000 — kanalga kirish narxi (so'm)\n"
    "/pay_minutes 5 — to'lov uchun vaqt (daqiqa)\n"
    "/channel_title Nomi — kanal nomi (reklamada)\n"
    "/daily_ideas 2 — kuniga nechta g'oya\n"
    "/research_time 09:00 — kunlik izlanish vaqti\n"
    "/autopublish on|off — g'oyalarni tasdiqsiz joylash"
)


@router.message(Command("settings"))
async def settings_cmd(message: Message, ctx: AppContext) -> None:
    s = ctx.settings

    def ch(value: int | None) -> str:
        return f"<code>{value}</code>" if value else "ulanmagan"

    lines = [
        "<b>JORIY SOZLAMALAR</b>",
        "",
        f"G'oyalar kanali: {ch(s.ideas_channel_id)}",
        f"To'lov kanali: {ch(s.payment_channel_id)}",
        f"Karta: {mask_card(s.payment_card_number)}",
        f"Karta egasi: {e(s.payment_card_holder) or 'kiritilmagan'}",
        f"Narx: {format_money(s.channel_price_uzs)} so'm",
        f"To'lov vaqti: {s.payment_timeout_seconds // 60} daqiqa",
        f"Kanal nomi: {e(s.ideas_channel_title)}",
        f"Kunlik g'oyalar: {s.daily_ideas_count} ta, soat {s.daily_research_time} ({s.timezone})",
        f"Avtomatik joylash: {'yoqilgan' if s.ideas_auto_publish else 'o`chirilgan'}",
        f"To'lov tizimi: <b>{'tayyor' if s.payments_configured else 'to`liq sozlanmagan'}</b>",
        "",
        SETTINGS_HELP,
    ]
    await message.answer("\n".join(lines))


async def _set(message: Message, ctx: AppContext, key: str, value: str, ok_text: str) -> None:
    try:
        await ctx.runtime.set(key, value)
    except SettingError as exc:
        await message.answer(f"Qabul qilinmadi: {e(str(exc))}")
        return
    # To'lov servisi ham yangi narx/vaqtdan foydalansin
    ctx.payments.base_amount = ctx.settings.channel_price_uzs
    ctx.payments.timeout = timedelta(seconds=ctx.settings.payment_timeout_seconds)
    await message.answer(ok_text)


def _arg(command: CommandObject) -> str:
    return (command.args or "").strip()


@router.message(Command("card"))
async def card_cmd(message: Message, command: CommandObject, ctx: AppContext) -> None:
    value = _arg(command)
    if not value:
        await message.answer("Masalan: /card 8600 1234 5678 9012")
        return
    await _set(message, ctx, "payment_card_number", value, "Karta saqlandi.")
    # Karta raqami chatda qolmasligi uchun admin xabarini o'chirishga urinamiz
    with contextlib.suppress(TelegramAPIError):
        await message.delete()


@router.message(Command("card_holder"))
async def card_holder_cmd(message: Message, command: CommandObject, ctx: AppContext) -> None:
    value = _arg(command)[:80]
    if not value:
        await message.answer("Masalan: /card_holder Aliyev Vali")
        return
    await _set(message, ctx, "payment_card_holder", value, "Karta egasi saqlandi.")


@router.message(Command("price"))
async def price_cmd(message: Message, command: CommandObject, ctx: AppContext) -> None:
    value = re.sub(r"[ _.,]", "", _arg(command))
    if not value.isdigit():
        await message.answer("Masalan: /price 50000")
        return
    await _set(message, ctx, "channel_price_uzs", value, f"Narx: {format_money(int(value))} so'm.")


@router.message(Command("pay_minutes"))
async def pay_minutes_cmd(message: Message, command: CommandObject, ctx: AppContext) -> None:
    value = _arg(command)
    if not value.isdigit() or not 1 <= int(value) <= 60:
        await message.answer("1 dan 60 gacha daqiqa kiriting. Masalan: /pay_minutes 5")
        return
    await _set(
        message, ctx, "payment_timeout_seconds", str(int(value) * 60), f"To'lov vaqti: {value} daqiqa."
    )


@router.message(Command("channel_title"))
async def channel_title_cmd(message: Message, command: CommandObject, ctx: AppContext) -> None:
    value = _arg(command)[:100]
    if not value:
        await message.answer("Masalan: /channel_title BIZNES MIND g'oyalar")
        return
    await _set(message, ctx, "ideas_channel_title", value, "Kanal nomi saqlandi.")


@router.message(Command("daily_ideas"))
async def daily_ideas_cmd(message: Message, command: CommandObject, ctx: AppContext) -> None:
    await _set(message, ctx, "daily_ideas_count", _arg(command) or "x", "Kunlik g'oyalar soni saqlandi.")


@router.message(Command("research_time"))
async def research_time_cmd(message: Message, command: CommandObject, ctx: AppContext) -> None:
    await _set(
        message,
        ctx,
        "daily_research_time",
        _arg(command) or "x",
        "Vaqt saqlandi. Yangi vaqt keyingi kundan (yoki bot qayta ishga tushganda) amal qiladi.",
    )


@router.message(Command("autopublish"))
async def autopublish_cmd(message: Message, command: CommandObject, ctx: AppContext) -> None:
    value = _arg(command).lower()
    if value not in {"on", "off"}:
        await message.answer("Masalan: /autopublish on yoki /autopublish off")
        return
    await _set(
        message,
        ctx,
        "ideas_auto_publish",
        "true" if value == "on" else "false",
        f"Avtomatik joylash: {value}.",
    )


async def _link(message: Message, ctx: AppContext, kind: str) -> None:
    code = ctx.runtime.create_link_code(kind, message.from_user.id)
    name = "g'oyalar (pullik)" if kind == "ideas" else "to'lov (bank xabarlari)"
    rights = (
        "admin qiling va 'Post messages' hamda 'Invite users via link' huquqlarini bering"
        if kind == "ideas"
        else "admin qiling"
    )
    await message.answer(
        f"<b>{name.capitalize()} kanalini ulash</b>\n\n"
        f"1. Botni shu kanalga {rights}.\n"
        f"2. Kanalga quyidagi kodni post qiling (10 daqiqa amal qiladi):\n\n"
        f"<code>{code}</code>\n\n"
        "Bot kodni ko'rib, kanalni ulaydi va sizga xabar beradi."
    )


@router.message(Command("link_ideas"))
async def link_ideas_cmd(message: Message, ctx: AppContext) -> None:
    await _link(message, ctx, "ideas")


@router.message(Command("link_payment"))
async def link_payment_cmd(message: Message, ctx: AppContext) -> None:
    await _link(message, ctx, "payment")


@router.message(Command("promo"))
async def promo_cmd(message: Message, ctx: AppContext, bot: Bot) -> None:
    await send_parts(bot, message.chat.id, [promo_text(ctx)], reply_markup=promo_kb())
