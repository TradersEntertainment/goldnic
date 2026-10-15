"""Tarih, para ve kira artışı hesapları (saf fonksiyonlar, test edilebilir)."""
from __future__ import annotations

import re
from datetime import date, datetime

# Yenilemeye kaç gün kala hatırlatma gidecek:
# 1 ay kala ilk bildirim (30), onay yoksa haftalık (23, 16), son 2 hafta her 3 günde bir (14, 11, 8, 5, 2, 0)
STAGES = (30, 23, 16, 14, 11, 8, 5, 2, 0)


def parse_date(text: str) -> date:
    """GG.AA.YYYY, GG/AA/YYYY veya YYYY-AA-GG biçimlerini kabul eder."""
    text = text.strip()
    for fmt in ("%d.%m.%Y", "%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise ValueError("Tarih biçimi geçersiz. Örnek: 01.11.2026")


def fmt_date(d: date | str) -> str:
    """date, datetime veya ISO metni (saatli olabilir) -> GG.AA.YYYY."""
    if isinstance(d, str):
        d = date.fromisoformat(d[:10])
    return d.strftime("%d.%m.%Y")


def add_year(d: date, years: int = 1) -> date:
    try:
        return d.replace(year=d.year + years)
    except ValueError:  # 29 Şubat
        return d.replace(year=d.year + years, day=28)


def _to_float(raw: str) -> float:
    s = raw.strip().replace("₺", "").replace("TL", "").replace("tl", "").replace("%", "")
    s = s.replace(" ", "")
    if not s:
        raise ValueError("Boş değer")
    if "." in s and "," in s:
        s = s.replace(".", "").replace(",", ".")
    elif "," in s:
        s = s.replace(",", ".")
    elif re.fullmatch(r"\d{1,3}(\.\d{3})+", s):  # 45.000 -> 45000
        s = s.replace(".", "")
    try:
        return float(s)
    except ValueError as exc:
        raise ValueError(f"Sayı anlaşılamadı: {raw!r}") from exc


def parse_amount(text: str) -> float:
    value = _to_float(text)
    if value <= 0:
        raise ValueError("Tutar sıfırdan büyük olmalı")
    return value


def parse_pct(text: str) -> float:
    value = _to_float(text)
    if value < 0 or value > 1000:
        raise ValueError("Oran mantıksız görünüyor")
    return value


def pct_change(old: float, new: float) -> float:
    if old <= 0:
        raise ValueError("Eski kira sıfırdan büyük olmalı")
    return (new - old) / old * 100


def apply_pct(old: float, pct: float) -> float:
    return round(old * (1 + pct / 100), 2)


def money(x: float | None) -> str:
    if x is None:
        return "-"
    s = f"{x:,.0f}".replace(",", ".")
    return f"{s} ₺"


def pct_str(x: float | None) -> str:
    if x is None:
        return "-"
    s = f"{x:.1f}".rstrip("0").rstrip(".").replace(".", ",")
    return f"%{s}"


def pct2(x: float | None) -> str:
    """Yasal oranlar için iki ondalık (TÜİK yayımladığı hassasiyet): 31.49 -> '%31,49', 25.0 -> '%25'."""
    if x is None:
        return "-"
    s = f"{x:.2f}".rstrip("0").rstrip(".").replace(".", ",")
    return f"%{s}"


def pick_stage(days_left: int) -> int | None:
    """Bugün için gönderilmesi gereken en yakın aşama. Gecikmişse -1."""
    if days_left < 0:
        return -1 if days_left >= -60 else None
    eligible = [s for s in STAGES if days_left <= s]
    return min(eligible) if eligible else None
