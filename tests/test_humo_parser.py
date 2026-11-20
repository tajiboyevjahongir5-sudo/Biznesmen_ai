"""HUMO Card bot bildirishnomalari (haqiqiy format namunasi asosida)."""

import pytest

from app.services.payment_parser import parse_payment

HUMO_IN = (
    "🎉 To'ldirish\n➕ {amount} UZS\n📍 PAYNET P2P HUM2HUM>T\n💳 HUMOCARD *7741\n"
    "🕓 21:43 18.11.2026\n💰 102.592,77 UZS"
)


@pytest.mark.parametrize(
    "shown,expected", [("50.037,00", 50037), ("10.000,00", 10000), ("150.099,00", 150099)]
)
def test_humo_card_income(shown, expected):
    p = parse_payment(HUMO_IN.format(amount=shown))
    assert p is not None and p.is_income and p.amount == expected


def test_humo_curly_apostrophe_and_balance_line_first():
    # Balans qatori birinchi kelsa ham, kirim (➕) qatoridagi summa olinadi.
    text = "💰 102.592,77 UZS\n🎉 To‘ldirish\n➕ 50.012,00 UZS"
    p = parse_payment(text)
    assert p is not None and p.amount == 50012


def test_humo_payment_is_expense():
    text = "💸 To'lov\n➖ 23.000,00 UZS\n📍 TBC SINGLE QR>TASHKE\n💳 HUMOCARD *7741\n💰 79.592,77 UZS"
    p = parse_payment(text)
    assert p is not None and not p.is_income


def test_humo_tiyin_amount_not_matched():
    assert parse_payment(HUMO_IN.format(amount="50.037,50")) is None
