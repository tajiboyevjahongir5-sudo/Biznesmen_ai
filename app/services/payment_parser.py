"""Bank bildirishnomasidan kirim summasini ajratib olish.

Bu modul AI ishlatmaydi — faqat deterministik regex. Bildirishnoma matni hech
qachon AI ga yuborilmaydi (prompt injection va soxtalashtirish xavfi yo'q).

Turli banklar formati har xil bo'lgani uchun:
- standart parser keng tarqalgan o'zbek/rus formatlarini taniydi;
- `.env` dagi PAYMENT_AMOUNT_REGEX orqali o'z formatingizni berishingiz mumkin.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Kirim (pul tushdi) belgilari
_INCOME_RE = re.compile(
    r"(?i)(popolnen|пополнен|zachislen|зачислен|postuplen|поступлен|kirim|tushdi|"
    r"to.ldirildi|to.ldirish|o'tkazma\s+qabul|perevod\s+na\s+kartu|перевод\s+на\s+карту|"
    r"\bincome\b|credited|received|➕|🟢)"
)
# Chiqim belgilari — bunday xabarlar e'tiborga olinmaydi
_EXPENSE_RE = re.compile(
    r"(?i)(spisan|списан|oplata|оплата|chiqim|yechildi|to'lov\s+qilindi|pokupka|покупка|"
    r"debited|➖|🔴)"
)
# Summa: 50 037.00 / 50,037.00 / 50037 / 50 037,00 + valyuta
_AMOUNT_RE = re.compile(
    r"(?<![\d])(\d{1,3}(?:[   .,]\d{3})+|\d{4,9})(?:[.,](\d{2}))?\s*"
    r"(?:uzs|so'm|so‘m|som|sum|сум|сўм)",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class ParsedPayment:
    amount: int  # butun so'mda
    is_income: bool


def _to_int(integer_part: str, fraction: str | None) -> int | None:
    digits = re.sub(r"[   .,]", "", integer_part)
    if not digits.isdigit():
        return None
    if fraction and fraction != "00":
        # Tiyinli summa — bizning noyob summalarimiz butun so'mda, mos kelmaydi.
        return None
    return int(digits)


def parse_payment(text: str, custom_regex: str = "") -> ParsedPayment | None:
    """Xabardan birinchi kirim summasini qaytaradi. Topilmasa None."""
    if not text:
        return None
    if custom_regex:
        m = re.search(custom_regex, text)
        if not m:
            return None
        amount = _to_int(m.group(1), None)
        return ParsedPayment(amount, True) if amount else None

    if _EXPENSE_RE.search(text) and not _INCOME_RE.search(text):
        return ParsedPayment(0, False)
    if not _INCOME_RE.search(text):
        return None
    # Avval kirim qatori (➕ ...) dagi summa: pastdagi "💰 balans" qatori hech qachon olinmaydi.
    m = None
    for line in text.splitlines():
        if "➕" in line or line.lstrip().startswith("+"):
            m = _AMOUNT_RE.search(line)
            if m:
                break
    m = m or _AMOUNT_RE.search(text)
    if not m:
        return None
    amount = _to_int(m.group(1), m.group(2))
    if amount is None:
        return None
    return ParsedPayment(amount, True)
