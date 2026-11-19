import asyncio
import random
from datetime import UTC, datetime, timedelta

import pytest

from app.database import repository as repo
from app.database.models import PaymentStatus
from app.services.payment_parser import parse_payment
from app.services.payment_service import NoFreeAmountError, PaymentService

BASE = 50_000


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 11, 19, 10, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: int) -> None:
        self.now += timedelta(seconds=seconds)


async def _users(sf, n: int) -> list[int]:
    ids = []
    async with sf() as session:
        for i in range(n):
            u = await repo.get_or_create_user(session, 10_000 + i, None, None)
            ids.append(u.id)
        await session.commit()
    return ids


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def service(session_factory, clock):
    return PaymentService(session_factory, BASE, 300, rng=random.Random(42), clock=clock)


async def test_amount_is_base_plus_1_to_99(service, session_factory):
    (uid,) = await _users(session_factory, 1)
    r, created = await service.reserve(uid, chat_id=1)
    assert created
    assert 1 <= r.offset <= 99
    assert r.amount == BASE + r.offset


async def test_same_user_gets_same_pending_amount(service, session_factory):
    (uid,) = await _users(session_factory, 1)
    r1, _ = await service.reserve(uid, 1)
    r2, created = await service.reserve(uid, 1)
    assert not created and r2.id == r1.id and r2.amount == r1.amount


async def test_concurrent_users_never_share_amount(service, session_factory):
    uids = await _users(session_factory, 99)
    results = await asyncio.gather(*(service.reserve(u, u) for u in uids))
    amounts = [r.amount for r, _ in results]
    assert len(set(amounts)) == 99
    # 100-foydalanuvchiga bo'sh summa qolmagan
    (extra,) = await _users_extra(session_factory)
    with pytest.raises(NoFreeAmountError):
        await service.reserve(extra, 1)


async def _users_extra(sf):
    async with sf() as session:
        u = await repo.get_or_create_user(session, 99_999, None, None)
        await session.commit()
        return [u.id]


async def test_amount_released_after_timeout(service, session_factory, clock):
    uids = await _users(session_factory, 99)
    for u in uids:
        await service.reserve(u, u)
    clock.advance(301)
    (extra,) = await _users_extra(session_factory)
    r, created = await service.reserve(extra, 1)  # muddati o'tganlar bo'shadi
    assert created
    expired = [await service.get(i) for i in range(1, 100)]
    assert all(x.status == PaymentStatus.EXPIRED for x in expired if x.id != r.id)


async def test_matching_payment_marks_paid(service, session_factory):
    (uid,) = await _users(session_factory, 1)
    r, _ = await service.reserve(uid, 1)
    res = await service.handle_notification(-100, 1, r.amount, "kirim")
    assert res.status == "matched" and res.reservation.id == r.id
    assert (await service.get(r.id)).status == PaymentStatus.PAID


async def test_wrong_amount_is_unmatched(service, session_factory):
    (uid,) = await _users(session_factory, 1)
    r, _ = await service.reserve(uid, 1)
    res = await service.handle_notification(-100, 1, BASE, "kirim")  # unikal qism yo'q
    assert res.status == "unmatched"
    assert (await service.get(r.id)).status == PaymentStatus.PENDING


async def test_late_payment_is_not_auto_confirmed(service, session_factory, clock):
    (uid,) = await _users(session_factory, 1)
    r, _ = await service.reserve(uid, 1)
    clock.advance(301)
    res = await service.handle_notification(-100, 1, r.amount, "kirim")
    assert res.status == "unmatched"
    recent = await service.recent_by_amount(r.amount)
    assert recent and recent[0].id == r.id


async def test_duplicate_notification_ignored(service, session_factory):
    (uid,) = await _users(session_factory, 1)
    r, _ = await service.reserve(uid, 1)
    assert (await service.handle_notification(-100, 7, r.amount, "x")).status == "matched"
    assert (await service.handle_notification(-100, 7, r.amount, "x")).status == "duplicate"


async def test_cancel_only_by_owner(service, session_factory):
    a, b = await _users(session_factory, 2)
    r, _ = await service.reserve(a, 1)
    assert not await service.cancel(b, r.id)
    assert await service.cancel(a, r.id)
    assert (await service.get(r.id)).status == PaymentStatus.CANCELLED


async def test_admin_confirm(service, session_factory, clock):
    (uid,) = await _users(session_factory, 1)
    r, _ = await service.reserve(uid, 1)
    clock.advance(400)
    await service.sweep_expired()
    confirmed = await service.admin_confirm(r.id, admin_id=1001)
    assert confirmed.status == PaymentStatus.PAID and confirmed.confirmed_by == "admin:1001"
    assert await service.admin_confirm(r.id, admin_id=1001) is None


# ------------------------------------------------------------------ parser


@pytest.mark.parametrize(
    "text,amount",
    [
        ("🟢 Пополнение\n➕ 50 037.00 UZS\n💳 HUMOCARD *1234\n🕓 10:01 19.11.2026", 50037),
        ("Kirim: 50 012,00 so'm\nKarta: *4321\nBalans: 1 250 000,00 so'm", 50012),
        ("Perevod na kartu 50,099.00 UZS", 50099),
        ("Zachislenie 50045 sum", 50045),
    ],
)
def test_parse_income(text, amount):
    p = parse_payment(text)
    assert p is not None and p.is_income and p.amount == amount


def test_parse_expense_ignored():
    p = parse_payment("🔴 Списание\n➖ 50 037.00 UZS\nOplata")
    assert p is not None and not p.is_income


def test_parse_with_tiyin_not_matched():
    assert parse_payment("Kirim: 50 037,50 so'm") is None


def test_parse_garbage():
    assert parse_payment("salom") is None
    assert parse_payment("") is None


def test_custom_regex():
    p = parse_payment("IN +50 077 UZS", custom_regex=r"IN \+([\d ]+) UZS")
    assert p and p.amount == 50077
