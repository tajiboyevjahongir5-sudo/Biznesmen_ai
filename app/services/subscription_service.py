"""Kanal obunasi muddati.

`SUBSCRIPTION_DAYS=0` — kirish umrbod (oldingi xatti-harakat). Aks holda har bir to'lov
obunani shuncha kunga uzaytiradi; muddat tugashidan oldin eslatma yuboriladi, tugagach
foydalanuvchi kanaldan chiqariladi (ban + darhol unban — keyin to'lab qayta kira oladi).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database.models import Subscription, SubscriptionStatus

REMIND_BEFORE = timedelta(days=3)


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


class SubscriptionService:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession], clock=lambda: datetime.now(UTC)):
        self._sf = session_factory
        self._clock = clock

    async def active_for(self, user_id: int) -> Subscription | None:
        async with self._sf() as session:
            return await session.scalar(
                select(Subscription).where(
                    Subscription.user_id == user_id, Subscription.status == SubscriptionStatus.ACTIVE
                )
            )

    async def grant(
        self, user_id: int, telegram_id: int, days: int, reservation_id: int | None
    ) -> Subscription:
        """Obunani yaratadi yoki uzaytiradi. `days=0` — muddatsiz."""
        now = self._clock()
        async with self._sf() as session:
            sub = await session.scalar(
                select(Subscription).where(
                    Subscription.user_id == user_id, Subscription.status == SubscriptionStatus.ACTIVE
                )
            )
            if sub is None:
                sub = Subscription(
                    user_id=user_id, telegram_id=telegram_id, status=SubscriptionStatus.ACTIVE, starts_at=now
                )
                session.add(sub)
            if days <= 0:
                sub.expires_at = None
            else:
                base = _aware(sub.expires_at)
                start = base if base and base > now else now
                sub.expires_at = start + timedelta(days=days)
            sub.reminded = False
            sub.last_reservation_id = reservation_id
            await session.commit()
            return sub

    def renewable(self, sub: Subscription | None) -> bool:
        """A'zo foydalanuvchi oldindan uzaytira oladimi (muddati yaqin bo'lsa)."""
        if sub is None or sub.expires_at is None:
            return False
        return _aware(sub.expires_at) - self._clock() <= REMIND_BEFORE

    async def due_reminders(self) -> list[Subscription]:
        now = self._clock()
        async with self._sf() as session:
            rows = await session.scalars(
                select(Subscription).where(
                    Subscription.status == SubscriptionStatus.ACTIVE,
                    Subscription.expires_at.is_not(None),
                    Subscription.expires_at <= now + REMIND_BEFORE,
                    Subscription.expires_at > now,
                    Subscription.reminded.is_(False),
                )
            )
            return list(rows.all())

    async def due_expired(self) -> list[Subscription]:
        now = self._clock()
        async with self._sf() as session:
            rows = await session.scalars(
                select(Subscription).where(
                    Subscription.status == SubscriptionStatus.ACTIVE,
                    Subscription.expires_at.is_not(None),
                    Subscription.expires_at <= now,
                )
            )
            return list(rows.all())

    async def mark(self, sub_id: int, *, reminded: bool | None = None, status: str | None = None) -> None:
        async with self._sf() as session:
            sub = await session.get(Subscription, sub_id)
            if sub is None:
                return
            if reminded is not None:
                sub.reminded = reminded
            if status is not None:
                sub.status = status
            await session.commit()

    async def count_active(self) -> int:
        async with self._sf() as session:
            return int(
                await session.scalar(
                    select(func.count())
                    .select_from(Subscription)
                    .where(Subscription.status == SubscriptionStatus.ACTIVE)
                )
                or 0
            )
