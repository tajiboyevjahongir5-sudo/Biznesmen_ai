"""To'lov oqimi: noyob summa, jonli taymer, to'lov kanalini kuzatish, bir martalik havola."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.bot.context import AppContext
from app.bot.helpers import CB_PAY_CANCEL, CB_PAY_START, is_channel_member
from app.core.logging import user_ref
from app.core.text import e, format_money
from app.database.models import PaymentReservation, PaymentStatus
from app.services.payment_parser import parse_payment
from app.services.payment_service import NoFreeAmountError

logger = logging.getLogger(__name__)
router = Router(name="payment")
channel_router = Router(name="payment_channel")

TIMER_TICK_SECONDS = 30
_timers: dict[int, asyncio.Task] = {}


def _card(number: str) -> str:
    return " ".join(number[i : i + 4] for i in range(0, len(number), 4))


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def payment_text(ctx: AppContext, r: PaymentReservation, now: datetime) -> str:
    s = ctx.settings
    left = max(0, int((_aware(r.expires_at) - now).total_seconds()))
    mm, ss = divmod(left, 60)
    holder = f"\nKarta egasi: <b>{e(s.payment_card_holder)}</b>" if s.payment_card_holder else ""
    return (
        "<b>Kanalga kirish uchun to'lov</b>\n\n"
        f"Karta raqami: <code>{_card(s.payment_card_number)}</code>{holder}\n"
        f"To'lov summasi: <code>{r.amount}</code> so'm\n"
        f"(<b>{format_money(r.amount)} so'm</b> — aynan shu summani o'tkazing)\n\n"
        f"Qolgan vaqt: <b>{mm}:{ss:02d}</b>\n\n"
        "Muhim:\n"
        f"• Summaning oxirgi raqamlari ({r.offset}) sizning to'lovingizni boshqalardan ajratadi. "
        "Summani yaxlitlamang.\n"
        "• To'lov tushishi bilan sizga bir martalik kirish havolasini yuboraman.\n"
        "• Vaqt tugagandan keyin bu summaga to'lov qilmang."
    )


def payment_kb(reservation_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Bekor qilish", callback_data=f"{CB_PAY_CANCEL}{reservation_id}")]
        ]
    )


@router.callback_query(F.data == CB_PAY_START)
async def pay_start(cb: CallbackQuery, ctx: AppContext, db_user_id: int, bot: Bot) -> None:
    s = ctx.settings
    tg_id = cb.from_user.id
    if not s.payments_configured:
        await cb.answer("To'lov hozircha sozlanmagan.", show_alert=True)
        return
    if await is_channel_member(bot, ctx, tg_id):
        await cb.answer("Siz allaqachon kanal a'zosisiz.", show_alert=True)
        return
    try:
        reservation, created = await ctx.payments.reserve(db_user_id, cb.message.chat.id)
    except NoFreeAmountError:
        await cb.answer(
            "Hozir to'lov navbati to'la. Iltimos, 2-3 daqiqadan keyin qayta urinib ko'ring.",
            show_alert=True,
        )
        return
    await cb.answer()
    now = datetime.now(UTC)
    if not created:
        await cb.message.answer("Sizda tugallanmagan to'lov bor, o'sha summadan foydalaning:")
    msg = await cb.message.answer(
        payment_text(ctx, reservation, now), reply_markup=payment_kb(reservation.id)
    )
    await ctx.payments.set_message_id(reservation.id, msg.message_id)
    logger.info("payment_reserved user=%s reservation=%d", user_ref(tg_id), reservation.id)
    start_timer(bot, ctx, reservation.id, msg.chat.id, msg.message_id)


@router.callback_query(F.data.startswith(CB_PAY_CANCEL))
async def pay_cancel(cb: CallbackQuery, ctx: AppContext, db_user_id: int) -> None:
    raw = cb.data.removeprefix(CB_PAY_CANCEL)
    if not raw.isdigit():
        await cb.answer()
        return
    ok = await ctx.payments.cancel(db_user_id, int(raw))
    stop_timer(int(raw))
    await cb.answer("To'lov bekor qilindi." if ok else "Bu to'lov allaqachon yakunlangan.")
    if ok:
        try:
            await cb.message.edit_text("To'lov bekor qilindi. Summa boshqa foydalanuvchiga bo'shatildi.")
        except TelegramBadRequest:
            pass


def start_timer(bot: Bot, ctx: AppContext, reservation_id: int, chat_id: int, message_id: int) -> None:
    stop_timer(reservation_id)
    _timers[reservation_id] = asyncio.create_task(_timer_loop(bot, ctx, reservation_id, chat_id, message_id))


def stop_timer(reservation_id: int) -> None:
    task = _timers.pop(reservation_id, None)
    if task and not task.done() and task is not asyncio.current_task():
        task.cancel()


async def _timer_loop(bot: Bot, ctx: AppContext, reservation_id: int, chat_id: int, message_id: int) -> None:
    """To'lov xabaridagi taymerni yangilaydi; vaqt tugasa summani bo'shatadi."""
    try:
        while True:
            await asyncio.sleep(TIMER_TICK_SECONDS)
            r = await ctx.payments.get(reservation_id)
            if r is None or r.status != PaymentStatus.PENDING:
                return
            now = datetime.now(UTC)
            if _aware(r.expires_at) <= now:
                await ctx.payments.sweep_expired()
                r = await ctx.payments.get(reservation_id)
                if r and r.status == PaymentStatus.EXPIRED:
                    await _safe_edit(
                        bot,
                        chat_id,
                        message_id,
                        "To'lov vaqti tugadi va summa bekor qilindi.\n\n"
                        "Agar pul o'tkazgan bo'lsangiz, xavotir olmang — administrator "
                        "tekshiradi. Yangi to'lov uchun /kanal yuboring.",
                    )
                return
            await _safe_edit(bot, chat_id, message_id, payment_text(ctx, r, now), payment_kb(r.id))
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - taymer xatosi botni to'xtatmasin
        logger.exception("payment_timer_failed reservation=%d", reservation_id)
    finally:
        if _timers.get(reservation_id) is asyncio.current_task():
            _timers.pop(reservation_id, None)


async def _safe_edit(
    bot: Bot, chat_id: int, message_id: int, text: str, kb: InlineKeyboardMarkup | None = None
) -> None:
    try:
        await bot.edit_message_text(text, chat_id=chat_id, message_id=message_id, reply_markup=kb)
    except TelegramBadRequest:
        pass  # xabar o'zgarmagan yoki o'chirilgan


async def deliver_access(bot: Bot, ctx: AppContext, r: PaymentReservation) -> bool:
    """Bir martalik (member_limit=1) va muddatli kirish havolasini yaratib yuboradi."""
    s = ctx.settings
    stop_timer(r.id)
    try:
        link = await bot.create_chat_invite_link(
            chat_id=s.ideas_channel_id,
            name=f"pay-{r.id}",
            expire_date=datetime.now(UTC) + timedelta(hours=s.invite_link_ttl_hours),
            member_limit=1,
        )
    except TelegramAPIError:
        logger.exception("invite_link_failed reservation=%d", r.id)
        await _notify_admins(
            bot,
            ctx,
            f"Havola yaratib bo'lmadi! To'lov #{r.id}, summa {r.amount}. "
            "Bot kanalda admin va 'invite users' huquqiga egami?",
        )
        await bot.send_message(
            r.chat_id, "To'lovingiz qabul qilindi. Kirish havolasini administrator tez orada yuboradi."
        )
        return False
    await ctx.payments.save_invite_link(r.id, link.invite_link)
    if r.message_id:
        await _safe_edit(
            bot, r.chat_id, r.message_id, f"To'lov qabul qilindi: <b>{format_money(r.amount)} so'm</b>."
        )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Kanalga kirish", url=link.invite_link)]]
    )
    await bot.send_message(
        r.chat_id,
        "Rahmat! To'lovingiz tasdiqlandi.\n\n"
        f"Bu havola <b>faqat bir marta</b> ishlaydi va {s.invite_link_ttl_hours} soat ichida "
        "amal qiladi. Uni boshqalarga bermang.",
        reply_markup=kb,
    )
    logger.info("access_delivered reservation=%d", r.id)
    return True


async def _notify_admins(bot: Bot, ctx: AppContext, text: str) -> None:
    for admin_id in ctx.settings.admin_ids:
        try:
            await bot.send_message(admin_id, text)
        except TelegramAPIError:
            logger.warning("admin_notify_failed")


@channel_router.channel_post()
async def payment_channel_post(post: Message, ctx: AppContext, bot: Bot) -> None:
    """To'lov kanaliga kelgan bank xabarlarini kuzatadi. Faqat sozlangan kanal qabul qilinadi."""
    s = ctx.settings
    if not s.payment_channel_id or post.chat.id != s.payment_channel_id:
        return
    text = post.text or post.caption or ""
    parsed = parse_payment(text, s.payment_amount_regex)
    amount = parsed.amount if parsed and parsed.is_income and parsed.amount > 0 else None
    result = await ctx.payments.handle_notification(post.chat.id, post.message_id, amount, text)
    logger.info("payment_notification status=%s amount=%s", result.status, amount)

    if result.status == "matched" and result.reservation:
        await deliver_access(bot, ctx, result.reservation)
    elif result.status == "unmatched":
        recent = await ctx.payments.recent_by_amount(amount)
        hint = ""
        if recent:
            hint = (
                "\n\nShu summa bilan yaqindagi to'lovlar:\n"
                + "\n".join(f"#{r.id} — holat: {r.status}, {_aware(r.created_at):%H:%M} UTC" for r in recent)
                + "\n\nTasdiqlash: /confirm raqam"
            )
        await _notify_admins(
            bot,
            ctx,
            f"Mos kelmagan to'lov: <b>{format_money(amount)} so'm</b> (kech yoki noto'g'ri summa).{hint}",
        )
