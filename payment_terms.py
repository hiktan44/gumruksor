"""Ödeme şekli metnini KKDF açısından sınıflandırır.

Eski uygulama ödeme şekli metninde ``"kredi"`` alt dizesini arıyordu; bu yüzden
"akreditif" (görüldüğünde ödemeli akreditif dahil) yanlışlıkla kredili/vadeli
sayılıp KKDF %6 önerisi alıyordu. Bu modül kelime sınırlarıyla ve beyanname
ödeme şekli kodlarıyla çalışır; emin olamadığı metinde karar vermez.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Literal

PaymentClass = Literal["deferred", "cash"]

# Beyanname (kutu 28) ödeme şekli kodları. Yalnız anlamı açık olanlar eşlenir;
# diğer kodlar (ör. vesaik mukabili, görüldüğünde ödemeli akreditif) için oran
# kullanıcıya sorulur.
_DEFERRED_CODES = frozenset({"1", "6", "14", "15", "16"})  # mal mukabili, vadeli akreditif, kabul kredili
_CASH_CODES = frozenset({"2"})  # peşin

_DEFERRED_PATTERNS = (
    re.compile(r"\bmal mukabili\b"),
    re.compile(r"\bvadeli\b"),
    re.compile(r"\bkabul kredili\b"),
    re.compile(r"\bkredili\b"),
    re.compile(r"\bkredi\b"),
    re.compile(r"\bdeferred\b"),
)
_CASH_PATTERNS = (
    re.compile(r"\bpesin\b"),
    re.compile(r"\bcash\b"),
    re.compile(r"\bon odeme\b"),
)


def normalize(value: Any) -> str:
    """Casefold, Türkçe ``ı`` → ``i`` ve aksan temizliği; boşluklar tekilleşir."""

    text = unicodedata.normalize("NFKD", str(value or "").casefold().replace("ı", "i"))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return " ".join(re.split(r"[\s/,;()\-]+", text)).strip()


def classify_payment_method(value: Any) -> PaymentClass | None:
    """``deferred`` (KKDF önerisi %6), ``cash`` (KKDF %0) veya ``None`` (sorulmalı)."""

    key = normalize(value)
    if not key:
        return None
    code = key.split(" ", 1)[0]
    if code.isdigit():
        if code in _DEFERRED_CODES:
            return "deferred"
        if code in _CASH_CODES:
            return "cash"
    if any(pattern.search(key) for pattern in _DEFERRED_PATTERNS):
        return "deferred"
    if any(pattern.search(key) for pattern in _CASH_PATTERNS):
        return "cash"
    return None
