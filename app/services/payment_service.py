"""Kanalga kirish uchun to'lovlarni boshqarish.

Mexanizm:
1. Foydalanuvchi to'lov qilmoqchi bo'lsa, unga `narx + N` (N = 1..99) noyob summa beriladi.
   Bir vaqtda bir xil summa ikki foydalanuvchiga berilmaydi (kod + DB unique index).
2. Summa `PAYMENT_TIMEOUT_SECONDS` (standart 5 daqiqa) davomida band turadi.
3. To'lov kanaliga aynan shu summa kelib tushsa — rezervatsiya to'langan deb belgilanadi.
4. Vaqt tugasa — rezervatsiya `expired`, summa yana bo'shaydi.
5. Kech kelgan yoki noma'lum summalar avtomatik tasdiqlanmaydi: adminlarga yuboriladi.
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database.models import PaymentNotification, PaymentReservation, PaymentStatus

MAX_OFFSET = 99


class NoFreeAmountError(Exception):
    """Hamma 99 ta noyob summa band."""


@dataclass(frozen=True)
class MatchResult:
    status: str  # matched | unmatched | duplicate | ignored
    reservation: PaymentReservation | None = None
    notification_id: int | None = None


def _now() -> datetime:
    return datetime.now(UTC)


def _aware(dt: datetime) -> datetime:
    # SQLite timezone ma'lumotini saqlamaydi — UTC deb qabul qilamiz.
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


class PaymentService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        base_amount: int,
        timeout_seconds: int,
        rng: random.Random | None = None,
        clock=_now,
    ) -> None:
        self._sf = session_factory
        self.base_amount = base_amount
        self.timeout = timedelta(seconds=timeout_seconds)
        self._rng = rng or random.SystemRandom()
        self._clock = clock
        # Bitta jarayon ichida rezervatsiyalarni ketma-ket bajaramiz;
        # DB dagi unique index esa ko'p jarayonli holatda ham himoya qiladi.
        self._lock = asyncio.Lock()

    async def expire_stale(self, session: AsyncSession) -> list[PaymentReservation]:
        """Muddati o'tgan kutilayotgan rezervatsiyalarni `expired` qiladi."""
        now = self._clock()
        rows = (
            await session.scalars(
                select(PaymentReservation).where(
                    PaymentReservation.status == PaymentStatus.PENDING,
                    PaymentReservation.expires_at <= now,
                )
            )
        ).all()
        for r in rows:
            r.status = PaymentStatus.EXPIRED
        await session.flush()
        return list(rows)

    async def get_pending_for_user(self, session: AsyncSession, user_id: int) -> PaymentReservation | None:
        now = self._clock()
        r = await session.scalar(
            select(PaymentReservation).where(
                PaymentReservation.user_id == user_id,
                PaymentReservation.status == PaymentStatus.PENDING,
            )
        )
        if r and _aware(r.expires_at) > now:
            return r
        return None

    async def reserve(self, user_id: int, chat_id: int) -> tuple[PaymentReservation, bool]:
        """Foydalanuvchi uchun noyob summa band qiladi.

        Qaytaradi: (rezervatsiya, yangi_yaratildimi). Agar foydalanuvchida faol
        rezervatsiya bo'lsa, o'shani qaytaradi (yangi summa bermaydi).
        """
        async with self._lock:
            for _attempt in range(3):
                async with self._sf() as session:
                    await self.expire_stale(session)
                    existing = await self.get_pending_for_user(session, user_id)
                    if existing:
                        await session.commit()
                        return existing, False
                    busy = set(
                        (
                            await session.scalars(
                                select(PaymentReservation.offset).where(
                                    PaymentReservation.status == PaymentStatus.PENDING
                                )
                            )
                        ).all()
                    )
                    free = [n for n in range(1, MAX_OFFSET + 1) if n not in busy]
                    if not free:
                        await session.commit()
                        raise NoFreeAmountError
                    offset = self._rng.choice(free)
                    now = self._clock()
                    reservation = PaymentReservation(
                        user_id=user_id,
                        chat_id=chat_id,
                        base_amount=self.base_amount,
                        offset=offset,
                        amount=self.base_amount + offset,
                        status=PaymentStatus.PENDING,
                        created_at=now,
                        expires_at=now + self.timeout,
                    )
                    session.add(reservation)
                    try:
                        await session.commit()
                    except IntegrityError:
                        # Boshqa jarayon shu summani ulgurib oldi — qayta urinamiz.
                        await session.rollback()
                        continue
                    return reservation, True
            raise NoFreeAmountError

    async def set_message_id(self, reservation_id: int, message_id: int) -> None:
        async with self._sf() as session:
            await session.execute(
                update(PaymentReservation)
                .where(PaymentReservation.id == reservation_id)
                .values(message_id=message_id)
            )
            await session.commit()

    async def cancel(self, user_id: int, reservation_id: int) -> bool:
        async with self._sf() as session:
            r = await session.get(PaymentReservation, reservation_id)
            if r is None or r.user_id != user_id or r.status != PaymentStatus.PENDING:
                return False
            r.status = PaymentStatus.CANCELLED
            await session.commit()
            return True

    async def get(self, reservation_id: int) -> PaymentReservation | None:
        async with self._sf() as session:
            return await session.get(PaymentReservation, reservation_id)

    async def handle_notification(
        self, chat_id: int, message_id: int, amount: int | None, raw_text: str
    ) -> MatchResult:
        """To'lov kanalidagi xabarni qayd etadi va kutilayotgan to'lov bilan solishtiradi."""
        async with self._lock, self._sf() as session:
            dup = await session.scalar(
                select(PaymentNotification.id).where(
                    PaymentNotification.chat_id == chat_id,
                    PaymentNotification.message_id == message_id,
                )
            )
            if dup:
                return MatchResult("duplicate")

            notif = PaymentNotification(
                chat_id=chat_id,
                message_id=message_id,
                amount=amount,
                raw_text=raw_text[:2000],
                status="ignored",
            )
            session.add(notif)
            if amount is None:
                await session.commit()
                return MatchResult("ignored", notification_id=notif.id)

            now = self._clock()
            reservation = await session.scalar(
                select(PaymentReservation).where(
                    PaymentReservation.amount == amount,
                    PaymentReservation.status == PaymentStatus.PENDING,
                    PaymentReservation.expires_at > now,
                )
            )
            if reservation is None:
                notif.status = "unmatched"
                await session.commit()
                return MatchResult("unmatched", notification_id=notif.id)

            reservation.status = PaymentStatus.PAID
            reservation.paid_at = now
            reservation.confirmed_by = "auto"
            await session.flush()
            notif.status = "matched"
            notif.matched_reservation_id = reservation.id
            await session.commit()
            return MatchResult("matched", reservation=reservation, notification_id=notif.id)

    async def admin_confirm(self, reservation_id: int, admin_id: int) -> PaymentReservation | None:
        """Admin qo'lda tasdiqlashi (masalan, kech kelgan to'lov uchun)."""
        async with self._lock, self._sf() as session:
            r = await session.get(PaymentReservation, reservation_id)
            if r is None or r.status == PaymentStatus.PAID:
                return None
            r.status = PaymentStatus.PAID
            r.paid_at = self._clock()
            r.confirmed_by = f"admin:{admin_id}"
            await session.commit()
            return r

    async def save_invite_link(self, reservation_id: int, link: str) -> None:
        async with self._sf() as session:
            await session.execute(
                update(PaymentReservation)
                .where(PaymentReservation.id == reservation_id)
                .values(invite_link=link)
            )
            await session.commit()

    async def recent_by_amount(self, amount: int, hours: int = 6) -> list[PaymentReservation]:
        """Admin uchun: shu summa bilan yaqinda bo'lgan (masalan, muddati o'tgan) rezervatsiyalar."""
        since = self._clock() - timedelta(hours=hours)
        async with self._sf() as session:
            rows = await session.scalars(
                select(PaymentReservation)
                .where(PaymentReservation.amount == amount, PaymentReservation.created_at >= since)
                .order_by(PaymentReservation.id.desc())
                .limit(5)
            )
            return list(rows.all())

    async def list_recent(self, limit: int = 15) -> list[PaymentReservation]:
        async with self._sf() as session:
            rows = await session.scalars(
                select(PaymentReservation).order_by(PaymentReservation.id.desc()).limit(limit)
            )
            return list(rows.all())

    async def stats(self) -> dict[str, int]:
        from sqlalchemy import func

        async with self._sf() as session:
            rows = await session.execute(
                select(PaymentReservation.status, func.count()).group_by(PaymentReservation.status)
            )
            data = {s: n for s, n in rows.all()}
            paid_sum = await session.scalar(
                select(func.coalesce(func.sum(PaymentReservation.amount), 0)).where(
                    PaymentReservation.status == PaymentStatus.PAID
                )
            )
            data["paid_sum"] = int(paid_sum or 0)
            return data

    async def sweep_expired(self) -> list[PaymentReservation]:
        async with self._lock, self._sf() as session:
            expired = await self.expire_stale(session)
            await session.commit()
            return expired
