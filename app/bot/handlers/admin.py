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
/stats            — batafsil statistika
/broadcast        — hammaga xabar (reply qilib)
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from datetime import UTC, datetime, timedelta

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from app.bot.context import AppContext
from app.bot.handlers.payment import deliver_access
from app.bot.helpers import promo_kb, promo_text, send_parts
from app.core.logging import redact
from app.core.text import e, format_money, heading, quote
from app.database import repository as repo
from app.database.models import ChannelIdea, IdeaStatus
from app.services.ai_service import AIServiceError
from app.services.channel_service import image_prompt
from app.services.gemini_native import GeminiNativeError, image_error_hint
from app.services.payment_parser import parse_payment
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
                InlineKeyboardButton(text="✅ Kanalga joylash", callback_data=f"{CB_PUBLISH}{idea_id}"),
                InlineKeyboardButton(text="❌ Rad etish", callback_data=f"{CB_REJECT}{idea_id}"),
            ]
        ]
    )


async def send_idea_preview(bot: Bot, chat_id: int, idea: ChannelIdea) -> None:
    await bot.send_message(
        chat_id, f"📝 <b>Qoralama #{idea.id}</b>\n\n{idea.post_text}", reply_markup=_idea_kb(idea.id)
    )


def _int_arg(command: CommandObject) -> int | None:
    arg = (command.args or "").strip().lstrip("#")
    return int(arg) if arg.isdigit() and len(arg) <= 15 else None


def _dot(on: bool) -> str:
    return "🟢" if on else "⚪"


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
        heading("🛠", "ADMIN PANEL"),
        f"👥 Foydalanuvchilar: <b>{users}</b>",
        f"💡 G'oyalar: 📝 {ideas.get('draft', 0)} · ✅ {ideas.get('published', 0)} · "
        f"❌ {ideas.get('rejected', 0)}",
        f"💳 To'lovlar: ✅ {pay.get('paid', 0)} · ⏳ {pay.get('pending', 0)} · ⌛ {pay.get('expired', 0)}",
        f"💰 Jami tushum: <b>{format_money(pay.get('paid_sum', 0))} so'm</b>",
        "",
        "🤖 <b>Bugungi AI sarfi</b>",
        f"• So'rovlar: <b>{u.global_requests}</b> / {s.ai_daily_requests_global}",
        f"• Tokenlar: kirish {u.input_tokens} · chiqish {u.output_tokens}",
        f"• Taxminiy xarajat: {cost}",
        "",
        "⚙️ <b>Holat</b>",
        f"{_dot(s.payments_configured)} To'lov tizimi",
        f"{_dot(s.userbot_enabled)} Avtomatik to'lov (userbot)",
        f"{_dot(s.web_search_enabled)} Internet qidiruvi",
        f"{_dot(s.post_images)} Post rasmlari",
        f"{_dot(s.ideas_auto_publish)} G'oyalarni avtomatik joylash",
        "",
        "📋 <b>Buyruqlar</b>",
        "🤖 /aitest · /websearch · /post_images",
        "💡 /research · /ideas · /publish <i>id</i> · /reject <i>id</i> · /promo",
        "💳 /payments · /confirm <i>id</i> · /parsetest · /userbot",
        "👥 /stats · /broadcast · /block <i>tg_id</i> · /unblock <i>tg_id</i>",
        "⚙️ /settings — kanal, karta, narx va boshqa sozlamalar",
    ]
    await message.answer("\n".join(lines))


@router.message(Command("aitest"))
async def aitest_cmd(message: Message, ctx: AppContext) -> None:
    """AI kaliti va modelini tekshiradi, mavjud model nomlarini ko'rsatadi."""
    await message.answer("🔌 AI ulanishini tekshiryapman...")
    lines = await ctx.ai.diagnose()
    await message.answer(heading("🩺", "AI DIAGNOSTIKA") + "\n" + "\n".join(e(x) for x in lines))


@router.message(Command("research"))
async def research_now(message: Message, ctx: AppContext, bot: Bot) -> None:
    await message.answer("🔎 Izlanish boshlandi. Bu bir necha daqiqa olishi mumkin...")
    try:
        created = await ctx.channel.research_and_create_drafts()
    except AIServiceError as exc:
        await message.answer(f"❌ Izlanish bajarilmadi: {exc.user_message}")
        return
    if not created:
        await message.answer("🤷 Yangi g'oya topilmadi (yoki kunlik g'oyalar soni 0).")
        return
    for idea in created:
        await send_idea_preview(bot, message.chat.id, idea)


@router.message(Command("ideas"))
async def list_drafts(message: Message, ctx: AppContext, bot: Bot) -> None:
    async with ctx.session_factory() as session:
        drafts = await repo.list_ideas(session, IdeaStatus.DRAFT, limit=10)
    if not drafts:
        await message.answer("📭 Qoralama g'oyalar yo'q. Yangi izlanish: /research")
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
        await message.answer("ℹ️ Masalan: /publish 5")
        return
    await message.answer(await _publish(bot, ctx, idea_id))


@router.message(Command("reject"))
async def reject_cmd(message: Message, command: CommandObject, ctx: AppContext) -> None:
    idea_id = _int_arg(command)
    if idea_id is None:
        await message.answer("ℹ️ Masalan: /reject 5")
        return
    ok = await ctx.channel.reject(idea_id)
    await message.answer("❌ Rad etildi." if ok else "🔍 Qoralama topilmadi.")


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
        await message.answer("📭 Hali to'lovlar yo'q.")
        return
    names = {
        "pending": "⏳ kutilmoqda",
        "paid": "✅ to'langan",
        "expired": "⌛ muddati o'tgan",
        "cancelled": "❌ bekor qilingan",
    }
    lines = [heading("💳", "SO'NGGI TO'LOVLAR")]
    for r in rows:
        by = f" ({e(r.confirmed_by)})" if r.confirmed_by else ""
        lines.append(
            f"<b>#{r.id}</b> · {format_money(r.amount)} so'm · {names.get(r.status, r.status)}{by}\n"
            f"<i>{r.created_at:%d.%m %H:%M} UTC</i>"
        )
    await message.answer("\n".join(lines))


@router.message(Command("confirm"))
async def confirm_cmd(message: Message, command: CommandObject, ctx: AppContext, bot: Bot) -> None:
    rid = _int_arg(command)
    if rid is None:
        await message.answer("ℹ️ Masalan: /confirm 17 (to'lov raqami /payments ro'yxatida)")
        return
    r = await ctx.payments.admin_confirm(rid, message.from_user.id)
    if r is None:
        await message.answer("🔍 To'lov topilmadi yoki allaqachon tasdiqlangan.")
        return
    ok = await deliver_access(bot, ctx, r)
    await message.answer(
        f"✅ To'lov #{rid} tasdiqlandi. " + ("Havola yuborildi." if ok else "⚠️ Havola yuborilmadi.")
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
        await message.answer("ℹ️ Masalan: /block 123456789")
        return
    ok = await _set_block(ctx, tg_id, True)
    await message.answer("🚫 Bloklandi." if ok else "🔍 Foydalanuvchi topilmadi.")


@router.message(Command("unblock"))
async def unblock_cmd(message: Message, command: CommandObject, ctx: AppContext) -> None:
    tg_id = _int_arg(command)
    if tg_id is None:
        await message.answer("ℹ️ Masalan: /unblock 123456789")
        return
    await message.answer(
        "✅ Blokdan chiqarildi." if await _set_block(ctx, tg_id, False) else "🔍 Foydalanuvchi topilmadi."
    )


# ------------------------------------------------------------------ sozlamalar

_SETTINGS_COMMANDS = (
    "/link_ideas — g'oyalar kanalini ulash (kod beriladi)\n"
    "/link_payment — to'lov kanalini ulash (kod beriladi)\n"
    "/card 8600123412341234 — karta raqami\n"
    "/card_holder Ism Familiya — karta egasi\n"
    "/price 50000 — kanalga kirish narxi (so'm)\n"
    "/pay_minutes 5 — to'lov uchun vaqt (daqiqa)\n"
    "/sub_days 30 — obuna muddati (0 — umrbod)\n"
    "/followup_days 7 — tahlildan keyin kuzatuv (0 — o'chiq)\n"
    "/bank_bot HUMOcardbot — bank boti (userbot uchun)\n"
    "/websearch on|off — internet qidiruvi\n"
    "/post_images on|off — kanal postlariga AI rasm\n"
    "/channel_title Nomi — kanal nomi (reklamada)\n"
    "/daily_ideas 2 — kuniga nechta g'oya\n"
    "/research_time 09:00 — kunlik izlanish vaqti\n"
    "/autopublish on|off — g'oyalarni tasdiqsiz joylash"
)
SETTINGS_HELP = "🛠 <b>Sozlash buyruqlari</b> <i>(ochish uchun bosing)</i>\n" + quote(
    _SETTINGS_COMMANDS, expandable=True
)


@router.message(Command("settings"))
async def settings_cmd(message: Message, ctx: AppContext) -> None:
    s = ctx.settings

    def ch(value: int | None) -> str:
        return f"<code>{value}</code>" if value else "ulanmagan"

    def on(value: bool, yes: str = "yoqilgan", no: str = "o'chiq") -> str:
        return f"{_dot(value)} {yes if value else no}"

    sub = f"{s.subscription_days} kun" if s.subscription_days else "umrbod (bir martalik)"
    follow = f"{s.followup_days} kundan keyin" if s.followup_days else "o'chiq"
    bank = f" · @{e(s.bank_bot_username)}" if s.bank_bot_username else ""
    lines = [
        heading("⚙️", "JORIY SOZLAMALAR"),
        "📢 <b>Kanallar</b>",
        f"• G'oyalar kanali: {ch(s.ideas_channel_id)}",
        f"• To'lov kanali: {ch(s.payment_channel_id)}",
        f"• Kanal nomi: {e(s.ideas_channel_title)}",
        "",
        "💳 <b>To'lov</b>",
        f"• Karta: {mask_card(s.payment_card_number)}",
        f"• Karta egasi: {e(s.payment_card_holder) or 'kiritilmagan'}",
        f"• Narx: <b>{format_money(s.channel_price_uzs)} so'm</b>",
        f"• To'lov vaqti: {s.payment_timeout_seconds // 60} daqiqa",
        f"• Obuna muddati: {sub}",
        f"• Userbot: {on(s.userbot_enabled, 'sozlangan')}{bank}",
        f"• Holat: <b>{on(s.payments_configured, 'tayyor', 'to`liq sozlanmagan')}</b>",
        "",
        "💡 <b>G'oyalar va AI</b>",
        f"• Kunlik g'oyalar: {s.daily_ideas_count} ta, soat {s.daily_research_time} ({s.timezone})",
        f"• Avtomatik joylash: {on(s.ideas_auto_publish)}",
        f"• Post rasmlari: {on(s.post_images)}",
        f"• Internet qidiruvi: {on(s.web_search_enabled)}",
        f"• Kuzatuv: {follow}",
        "",
        SETTINGS_HELP,
    ]
    await message.answer("\n".join(lines))


async def _set(message: Message, ctx: AppContext, key: str, value: str, ok_text: str) -> None:
    try:
        await ctx.runtime.set(key, value)
    except SettingError as exc:
        await message.answer(f"⚠️ Qabul qilinmadi: {e(str(exc))}")
        return
    # To'lov servisi ham yangi narx/vaqtdan foydalansin
    ctx.payments.base_amount = ctx.settings.channel_price_uzs
    ctx.payments.timeout = timedelta(seconds=ctx.settings.payment_timeout_seconds)
    await message.answer(f"✅ {ok_text}" if ok_text[:1].isalnum() else ok_text)


def _arg(command: CommandObject) -> str:
    return (command.args or "").strip()


@router.message(Command("card"))
async def card_cmd(message: Message, command: CommandObject, ctx: AppContext) -> None:
    value = _arg(command)
    if not value:
        await message.answer("ℹ️ Masalan: /card 8600 1234 5678 9012")
        return
    await _set(message, ctx, "payment_card_number", value, "Karta saqlandi.")
    # Karta raqami chatda qolmasligi uchun admin xabarini o'chirishga urinamiz
    with contextlib.suppress(TelegramAPIError):
        await message.delete()


@router.message(Command("card_holder"))
async def card_holder_cmd(message: Message, command: CommandObject, ctx: AppContext) -> None:
    value = _arg(command)[:80]
    if not value:
        await message.answer("ℹ️ Masalan: /card_holder Aliyev Vali")
        return
    await _set(message, ctx, "payment_card_holder", value, "Karta egasi saqlandi.")


@router.message(Command("price"))
async def price_cmd(message: Message, command: CommandObject, ctx: AppContext) -> None:
    value = re.sub(r"[ _.,]", "", _arg(command))
    if not value.isdigit():
        await message.answer("ℹ️ Masalan: /price 50000")
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
        await message.answer("ℹ️ Masalan: /channel_title BIZNES MIND g'oyalar")
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
        await message.answer("ℹ️ Masalan: /autopublish on yoki /autopublish off")
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


@router.message(Command("sub_days"))
async def sub_days_cmd(message: Message, command: CommandObject, ctx: AppContext) -> None:
    value = _arg(command)
    if not value.isdigit() or int(value) > 3650:
        await message.answer("ℹ️ Masalan: /sub_days 30 (oylik obuna) yoki /sub_days 0 (umrbod kirish)")
        return
    days = int(value)
    await _set(
        message,
        ctx,
        "subscription_days",
        str(days),
        f"Obuna muddati: {days} kun." if days else "Kirish endi umrbod (bir martalik to'lov).",
    )


# ------------------------------------------------------------------ statistika


@router.message(Command("stats"))
async def stats_cmd(message: Message, ctx: AppContext) -> None:
    async with ctx.session_factory() as session:
        st = await repo.bot_stats(session, datetime.now(UTC))
    conv = f"{st['done'] / st['projects'] * 100:.0f}%" if st["projects"] else "—"
    lines = [
        heading("📊", "STATISTIKA"),
        "👥 <b>Foydalanuvchilar</b>",
        f"Jami: <b>{st['users']}</b> · bloklangan: {st['blocked']}",
        f"Yangi: 24 soat — {st['users_new_day']}, 7 kun — {st['users_new_week']}",
        f"Faol: 24 soat — {st['active_day']}, 7 kun — {st['active_week']}",
        "",
        "💡 <b>Loyihalar</b>",
        f"Jami: {st['projects']} (7 kunda {st['projects_week']})",
        f"Suhbat davom etmoqda: {st['interviewing']}",
        f"Tahlil tugagan: {st['done']} (7 kunda {st['done_week']}) · konversiya {conv}",
        "",
        "💳 <b>To'lovlar</b>",
        f"24 soat: {st['paid_day']} ta — <b>{format_money(st['revenue_day'])} so'm</b>",
        f"30 kun: {st['paid_month']} ta — <b>{format_money(st['revenue_month'])} so'm</b>",
        f"Faol obunachilar: {st['subscribers']}",
        "",
        "📢 <b>Kanal</b>",
        f"Joylangan g'oyalar: {st['ideas_published']} · prompt so'rovlari: {st['prompt_requests']}",
        "",
        "🤖 <b>Bugungi AI</b>",
        f"So'rovlar: {ctx.ai.usage.global_requests}/{ctx.settings.ai_daily_requests_global}",
    ]
    await message.answer("\n".join(lines))


# ------------------------------------------------------------------ broadcast

CB_BC_SEND = "adm:bc:send"
CB_BC_CANCEL = "adm:bc:cancel"
_pending_broadcasts: dict[int, tuple[int, int]] = {}  # admin_id -> (chat_id, message_id)
_broadcast_running = False


@router.message(Command("broadcast"))
async def broadcast_cmd(message: Message, ctx: AppContext) -> None:
    """Admin biror xabarga javob (reply) qilib /broadcast yozadi — o'sha xabar hammaga nusxalanadi."""
    src = message.reply_to_message
    if src is None:
        await message.answer(
            "📣 Hammaga xabar yuborish:\n\n"
            "1. Yubormoqchi bo'lgan xabaringizni (matn, rasm, video) shu chatga yozing.\n"
            "2. O'sha xabarga <b>javob (reply)</b> qilib /broadcast yuboring.\n"
            "3. Tasdiqlash tugmasini bosing."
        )
        return
    async with ctx.session_factory() as session:
        targets = await repo.broadcast_targets(session)
    _pending_broadcasts[message.from_user.id] = (src.chat.id, src.message_id)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=f"✅ {len(targets)} kishiga yuborish", callback_data=CB_BC_SEND),
                InlineKeyboardButton(text="❌ Bekor qilish", callback_data=CB_BC_CANCEL),
            ]
        ]
    )
    await message.answer(
        f"Yuqoridagi xabar <b>{len(targets)}</b> foydalanuvchiga yuboriladi. Tasdiqlaysizmi?", reply_markup=kb
    )


@router.callback_query(F.data == CB_BC_CANCEL)
async def broadcast_cancel(cb: CallbackQuery) -> None:
    _pending_broadcasts.pop(cb.from_user.id, None)
    await cb.answer("Bekor qilindi")
    with contextlib.suppress(TelegramAPIError):
        await cb.message.edit_text("Xabar yuborish bekor qilindi.")


@router.callback_query(F.data == CB_BC_SEND)
async def broadcast_send(cb: CallbackQuery, ctx: AppContext, bot: Bot) -> None:
    global _broadcast_running
    src = _pending_broadcasts.pop(cb.from_user.id, None)
    if src is None:
        await cb.answer("Tasdiqlanadigan xabar topilmadi. /broadcast ni qayta yuboring.", show_alert=True)
        return
    if _broadcast_running:
        await cb.answer("Boshqa xabar yuborilmoqda, tugashini kuting.", show_alert=True)
        return
    await cb.answer("Yuborish boshlandi")
    with contextlib.suppress(TelegramAPIError):
        await cb.message.edit_text("📤 Yuborilmoqda...")
    _broadcast_running = True
    task = asyncio.create_task(_run_broadcast(bot, ctx, cb.from_user.id, *src))
    _bc_tasks.add(task)
    task.add_done_callback(_bc_tasks.discard)


_bc_tasks: set[asyncio.Task] = set()


async def _run_broadcast(bot: Bot, ctx: AppContext, admin_id: int, chat_id: int, message_id: int) -> None:
    global _broadcast_running
    sent = failed = 0
    try:
        async with ctx.session_factory() as session:
            targets = await repo.broadcast_targets(session)
        for tg_id in targets:
            for _ in range(2):
                try:
                    await bot.copy_message(tg_id, chat_id, message_id)
                    sent += 1
                    break
                except TelegramRetryAfter as exc:
                    await asyncio.sleep(min(exc.retry_after, 60))
                except TelegramAPIError:
                    failed += 1  # bot bloklangan yoki akkaunt o'chirilgan
                    break
            await asyncio.sleep(0.05)  # Telegram limiti: ~20 xabar/soniya
        logger.info("broadcast_done sent=%d failed=%d", sent, failed)
        await bot.send_message(
            admin_id,
            f"✅ Xabar yuborildi: <b>{sent}</b> ta. Yetib bormadi: {failed} ta (botni bloklaganlar).",
        )
    except Exception:  # noqa: BLE001
        logger.exception("broadcast_failed")
        with contextlib.suppress(TelegramAPIError):
            await bot.send_message(admin_id, f"Xabar yuborish to'xtadi. Yuborilgan: {sent} ta.")
    finally:
        _broadcast_running = False


@router.message(Command("websearch"))
async def websearch_cmd(message: Message, command: CommandObject, ctx: AppContext) -> None:
    value = _arg(command).lower()
    s = ctx.settings
    if value not in {"on", "off"}:
        state = "yoqilgan" if s.web_search_enabled else "o'chirilgan"
        await message.answer(
            f"🌐 Internet qidiruvi: <b>{state}</b>\n\n"
            "/websearch on — tahlil va kunlik g'oyalarda Google qidiruvidan foydalanish\n"
            "/websearch off — o'chirish\n\n"
            "<i>Gemini'da Google qidiruvi bepul tarifda kunlik cheklangan; billing yoqilgan bo'lsa, "
            "limitdan oshgani pullik. Narxlarni Google AI Studio'da tekshiring.</i>"
        )
        return
    provider = "none"
    if value == "on":
        provider = "gemini_search" if s.ai_provider == "gemini" else "openai_web"
    await _set(
        message,
        ctx,
        "research_provider",
        provider,
        "🌐 Internet qidiruvi yoqildi." if value == "on" else "Internet qidiruvi o'chirildi.",
    )


POST_IMAGES_HELP = (
    "/post_images on — kanal postlariga AI rasm qo'shish\n"
    "/post_images off — o'chirish (faqat matnli post)\n"
    "/post_images test — bitta sinov rasmini shu yerga yuborish\n"
    "/post_images model NOM — rasm modelini tanlash (auto — avtomatik)\n\n"
    "<i>⚠️ Gemini rasm modellari odatda pullik (billing yoqilgan kalit kerak). Har bir post — "
    "bitta rasm so'rovi. Narxni Google AI Studio'da tekshiring. Rasm yaratilmasa, post "
    "odatdagidek matn ko'rinishida joylanadi.</i>"
)


def _image_failure_text(exc: AIServiceError, ctx: AppContext) -> str:
    cause = exc.__cause__
    lines = [f"❌ <b>Rasm yaratilmadi</b> · <code>{type(exc).__name__}</code>"]
    if isinstance(cause, GeminiNativeError):
        if cause.tried:
            models = ", ".join(f"<code>{e(m)}</code>" for m in cause.tried)
            lines.append(f"🧪 Sinab ko'rilgan modellar: {models}")
        if cause.detail:
            detail = redact(cause.detail, ctx.settings.secret_values())[:400]
            lines += ["", f"📨 Google javobi ({cause.status}):", quote(e(detail), expandable=True)]
        lines += ["", f"💡 {image_error_hint(cause)}"]
    else:
        lines += ["", "💡 Gemini kaliti sozlanganini va kunlik AI limiti tugamaganini tekshiring (/aitest)."]
    if ctx.settings.post_images:
        lines += [
            "",
            "<i>Muammo hal bo'lmaguncha postlar matn ko'rinishida chiqadi. O'chirish: /post_images off</i>",
        ]
    return "\n".join(lines)


@router.message(Command("post_images"))
async def post_images_cmd(message: Message, command: CommandObject, ctx: AppContext) -> None:
    s = ctx.settings
    action, _, rest = _arg(command).partition(" ")
    action = action.lower()
    if action == "auto":  # "/post_images auto" — "/post_images model auto" bilan bir xil
        action, rest = "model", "auto"
    if action in {"on", "off"}:
        if action == "on" and s.ai_provider != "gemini":
            await message.answer("Rasm faqat AI_PROVIDER=gemini bilan ishlaydi.")
            return
        await _set(
            message,
            ctx,
            "post_images",
            "true" if action == "on" else "false",
            "🖼 Kanal postlariga rasm qo'shiladi." if action == "on" else "Kanal rasmlari o'chirildi.",
        )
        return
    if action == "model":
        name = rest.strip()
        if not name:
            await message.answer(
                "ℹ️ Masalan: /post_images model gemini-2.5-flash-image yoki /post_images model auto"
            )
            return
        value = "" if name.lower() == "auto" else name
        await _set(message, ctx, "gemini_image_model", value, f"Rasm modeli: {e(name)}")
        return
    if action == "test":
        await message.answer("🎨 Sinov rasmi tayyorlanyapti (10–40 soniya)...")
        prompt = image_prompt(
            "Mahalla uchun sog'lom nonushta yetkazib berish", "Ertalab ofis xodimlariga issiq nonushta"
        )
        try:
            image = await ctx.ai.generate_image(prompt)
        except AIServiceError as exc:
            await message.answer(_image_failure_text(exc, ctx))
            return
        await message.answer_photo(
            BufferedInputFile(image.data, image.filename),
            caption=f"✅ Rasm tayyor. Model: <code>{e(image.model)}</code>",
        )
        return
    state = "yoqilgan" if s.post_images else "o'chiq"
    model = s.gemini_image_model or "avtomatik"
    await message.answer(f"🖼 Kanal rasmlari: <b>{state}</b> · model: {e(model)}\n\n{POST_IMAGES_HELP}")


@router.message(Command("followup_days"))
async def followup_days_cmd(message: Message, command: CommandObject, ctx: AppContext) -> None:
    value = _arg(command)
    if not value.isdigit() or int(value) > 90:
        await message.answer("ℹ️ Masalan: /followup_days 7 (tahlildan 7 kun keyin so'rash) yoki 0 (o'chirish)")
        return
    await _set(
        message,
        ctx,
        "followup_days",
        value,
        f"Kuzatuv: tahlildan {value} kun keyin." if int(value) else "Kuzatuv o'chirildi.",
    )


@router.message(Command("parsetest"))
async def parsetest_cmd(message: Message, command: CommandObject, ctx: AppContext) -> None:
    """Bank xabarini parser qanday tushunishini ko'rsatadi (hech narsa saqlanmaydi)."""
    src = message.reply_to_message
    text = (src.text or src.caption or "") if src else _arg(command)
    if not text:
        await message.answer(
            "🧪 To'lov parserini tekshirish:\n\n"
            "Bank xabarini (to'lov kanalidan forward qilib) shu chatga yuboring va unga "
            "<b>javob (reply)</b> qilib /parsetest yozing. Yoki: /parsetest xabar matni"
        )
        return
    parsed = parse_payment(text, ctx.settings.payment_amount_regex)
    if parsed is None:
        result = "❌ Summa topilmadi. Bu xabar to'lov sifatida qabul qilinmaydi."
    elif not parsed.is_income:
        result = (
            f"⚠️ Summa: {format_money(parsed.amount)} so'm, lekin bu <b>chiqim</b> deb aniqlandi (e'tiborsiz)."
        )
    else:
        result = (
            f"✅ Kirim: <b>{format_money(parsed.amount)} so'm</b> — "
            "shu summa kutilayotgan to'lov bilan solishtiriladi."
        )
    custom = "maxsus regex" if ctx.settings.payment_amount_regex else "standart qoidalar"
    await message.answer(f"{result}\n\n<i>Ishlatilgan: {custom}.</i>")


@router.message(Command("userbot"))
async def userbot_cmd(message: Message, ctx: AppContext) -> None:
    from app.services.userbot import STATUS

    s = ctx.settings
    if not s.userbot_enabled:
        missing = [
            name
            for name, ok in (
                ("TG_API_ID", s.tg_api_id),
                ("TG_API_HASH", s.tg_api_hash.get_secret_value()),
                ("TG_SESSION", s.tg_session.get_secret_value()),
                ("BANK_BOT_USERNAME (/bank_bot)", s.bank_bot_username),
            )
            if not ok
        ]
        await message.answer(
            "🤖 <b>Avtomatik to'lov (userbot): o'chiq</b>\n\nYetishmayapti: " + ", ".join(missing) + "\n\n"
            "Yo'riqnoma: README → «Avtomatik to'lov (userbot)»."
        )
        return

    def t(dt):
        return f"{dt:%d.%m %H:%M} UTC" if dt else "—"

    state = "🟢 ulangan" if STATUS.connected else "🔴 ulanmagan"
    lines = [
        f"🤖 <b>Avtomatik to'lov (userbot): {state}</b>",
        f"Bank boti: @{e(s.bank_bot_username)}",
        f"Ishga tushgan: {t(STATUS.started_at)}",
        f"Oxirgi bank xabari: {t(STATUS.last_message_at)}",
        f"Qayta ishlangan: {STATUS.processed} · tasdiqlangan to'lovlar: {STATUS.matched}",
    ]
    if STATUS.error:
        lines.append(f"⚠️ Xato: {e(STATUS.error)}")
    await message.answer("\n".join(lines))


@router.message(Command("bank_bot"))
async def bank_bot_cmd(message: Message, command: CommandObject, ctx: AppContext) -> None:
    value = _arg(command)
    if not value:
        await message.answer("ℹ️ Masalan: /bank_bot HUMOcardbot (bildirishnoma keladigan bank boti username)")
        return
    await _set(
        message,
        ctx,
        "bank_bot_username",
        value,
        "Bank boti saqlandi. Kuchga kirishi uchun Railway'da botni qayta ishga tushiring (Redeploy).",
    )
