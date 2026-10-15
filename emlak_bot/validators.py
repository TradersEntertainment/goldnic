"""Resmi belge alanları için doğrulama ve biçimlendirme (T.C. Kimlik No, IBAN, tutarın yazıyla ifadesi)."""
from __future__ import annotations

import re
from decimal import ROUND_HALF_UP, Decimal


def tc_kimlik_valid(value: str | None) -> bool:
    """T.C. Kimlik No algoritması: 11 hane, ilk hane 0 olamaz, 10. ve 11. haneler kontrol hanesidir."""
    d = re.sub(r"\D", "", value or "")
    if len(d) != 11 or d[0] == "0":
        return False
    n = [int(c) for c in d]
    if (sum(n[0:9:2]) * 7 - sum(n[1:8:2])) % 10 != n[9]:
        return False
    return sum(n[:10]) % 10 == n[10]


def clean_tc(value: str | None) -> str:
    return re.sub(r"\D", "", value or "")


def iban_clean(value: str | None) -> str:
    return re.sub(r"[^0-9A-Za-z]", "", value or "").upper()


def iban_valid(value: str | None) -> bool:
    """ISO 13616 mod-97 kontrolü; TR IBAN'ları 26 karakterdir."""
    s = iban_clean(value)
    if not re.fullmatch(r"[A-Z]{2}\d{2}[0-9A-Z]{10,30}", s):
        return False
    if s.startswith("TR") and len(s) != 26:
        return False
    rearranged = s[4:] + s[:4]
    digits = "".join(str(int(ch, 36)) for ch in rearranged)
    return int(digits) % 97 == 1


def iban_format(value: str | None) -> str:
    s = iban_clean(value)
    return " ".join(s[i:i + 4] for i in range(0, len(s), 4))


# ---------------------------------------------------------------- tutarın yazıyla ifadesi
_ONES = ["", "bir", "iki", "üç", "dört", "beş", "altı", "yedi", "sekiz", "dokuz"]
_TENS = ["", "on", "yirmi", "otuz", "kırk", "elli", "altmış", "yetmiş", "seksen", "doksan"]
_SCALES = ["", "bin", "milyon", "milyar", "trilyon"]


def _three(n: int) -> list[str]:
    h, rest = divmod(n, 100)
    t, o = divmod(rest, 10)
    words = []
    if h:
        if h > 1:
            words.append(_ONES[h])
        words.append("yüz")
    if t:
        words.append(_TENS[t])
    if o:
        words.append(_ONES[o])
    return words


def number_words(n: int) -> str:
    """Tam sayıyı Türkçe yazıya çevirir: 32000 → 'otuz iki bin', 1001000 → 'bir milyon bin'."""
    if n == 0:
        return "sıfır"
    if n < 0:
        return "eksi " + number_words(-n)
    groups = []
    i = 0
    while n:
        n, chunk = divmod(n, 1000)
        if chunk:
            words = _three(chunk)
            if i == 1 and chunk == 1:   # "bir bin" değil, "bin"
                words = []
            if i:
                words.append(_SCALES[i])
            groups.append(" ".join(words))
        i += 1
    return " ".join(reversed(groups))


def tr_title(text: str) -> str:
    """Türkçe büyük harf kurallarıyla kelime başlarını büyütür (i → İ, ı → I)."""
    def cap(w: str) -> str:
        first = {"i": "İ", "ı": "I"}.get(w[0], w[0].upper())
        return first + w[1:]
    return " ".join(cap(w) for w in text.split())


def amount_words(amount: float | Decimal) -> str:
    """32000.5 → 'Otuz İki Bin Türk Lirası Elli Kuruş'."""
    value = Decimal(str(amount)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    lira = int(value)
    kurus = int((value - lira) * 100)
    text = f"{number_words(lira)} Türk lirası"
    if kurus:
        text += f" {number_words(kurus)} kuruş"
    return tr_title(text)


def money_exact(amount: float | Decimal | None) -> str:
    """Belgelerde kuruşlu tutar: 32000 → '32.000,00 TL'."""
    if amount is None:
        return "-"
    value = Decimal(str(amount)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    whole, frac = f"{value:,.2f}".split(".")
    return f"{whole.replace(',', '.')},{frac} TL"
