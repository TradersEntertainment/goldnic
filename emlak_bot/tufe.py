"""TÜFE (12 aylık ortalamalara göre değişim) veri katmanı.

Kaynak sırası: TÜİK haber bülteni / TCMB EVDS (canlı) → elle giriş (ofis sahibi) → yerleşik tablo.
Yerleşik tablo TÜİK bültenlerinde yayımlanan değerlerden derlenmiştir ve VERİ AYINA göre
anahtarlanır (ör. "2026-09": Eylül 2026 verisi, 05.10.2026'da açıklandı → Ekim 2026 yenilemeleri).
"""
from __future__ import annotations

import html as htmllib
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Callable
from urllib.parse import urljoin

from .db import now_iso
from .law import CpiRate, add_months, announcement_date, period_of, shift_period

log = logging.getLogger(__name__)

# Veri ayı → 12 aylık ortalamalara göre TÜFE değişimi (%). TÜİK haber bültenlerinden.
BUILTIN_AVG12: dict[str, float] = {
    "2024-01": 54.72, "2024-02": 55.91, "2024-03": 57.50, "2024-04": 59.64, "2024-05": 62.51,
    "2024-06": 65.07, "2024-07": 65.93, "2024-08": 64.91, "2024-09": 63.47, "2024-10": 62.02,
    "2024-11": 60.45, "2024-12": 58.51,
    "2025-01": 56.35, "2025-02": 53.83, "2025-03": 51.26, "2025-04": 48.73, "2025-05": 45.80,
    "2025-06": 43.23, "2025-07": 41.13, "2025-08": 39.62, "2025-09": 38.36, "2025-10": 37.15,
    "2025-11": 35.91, "2025-12": 34.88,
    "2026-01": 33.98, "2026-02": 33.39, "2026-03": 32.82, "2026-04": 32.43, "2026-05": 32.24,
    "2026-06": 32.03, "2026-07": 31.90, "2026-08": 31.79, "2026-09": 31.49,
}
# Bülten tarihi resmi takvimden sapan (tatil vb.) aylar; diğerleri ayın 3'ü (hafta sonu → pazartesi).
KNOWN_ANNOUNCEMENTS: dict[str, date] = {}

SOURCE_PRIORITY = {"builtin": 0, "manual": 1, "evds": 2, "tuik": 2}
SOURCE_LABELS = {"builtin": "Yerleşik tablo (TÜİK bültenleri)", "manual": "Elle giriş", "evds": "TCMB EVDS",
                 "tuik": "TÜİK haber bülteni"}

TUIK_LIST_URL = "https://data.tuik.gov.tr/Kategori/GetKategori?p=enflasyon-ve-fiyat-106&dil=1"
EVDS_BASE_URL = "https://evds3.tcmb.gov.tr/igmevdsms-dis/"
USER_AGENT = "Mozilla/5.0 (compatible; GoldNicomediaOS/1.0; +kira-artis-takibi)"

MONTHS = {
    "ocak": 1, "subat": 2, "şubat": 2, "mart": 3, "nisan": 4, "mayis": 5, "mayıs": 5, "haziran": 6,
    "temmuz": 7, "agustos": 8, "ağustos": 8, "eylul": 9, "eylül": 9, "ekim": 10, "kasim": 11, "kasım": 11,
    "aralik": 12, "aralık": 12,
}

HttpGet = Callable[[str, dict], tuple[int, str, str]]   # (url, headers) -> (status, body, final_url)


@dataclass
class ParsedCpi:
    period: str
    avg12: float
    annual: float | None = None


# ---------------------------------------------------------------- saklama
def ensure_builtin(db) -> None:
    now = now_iso()
    rows = [(p, v, None, announcement_for(p).isoformat(), "builtin", now) for p, v in BUILTIN_AVG12.items()]
    with db.connection() as c:
        c.executemany(
            "INSERT OR IGNORE INTO cpi_rates (period, avg12, annual, announced_on, source, fetched_at)"
            " VALUES (?,?,?,?,?,?)", rows)


def announcement_for(period: str) -> date:
    return KNOWN_ANNOUNCEMENTS.get(period) or announcement_date(period)


def load_rates(db) -> dict[str, CpiRate]:
    return {
        r["period"]: CpiRate(period=r["period"], avg12=r["avg12"], announced_on=date.fromisoformat(r["announced_on"]),
                             source=r["source"], annual=r["annual"])
        for r in db.query("SELECT * FROM cpi_rates")
    }


def upsert_rate(db, period: str, avg12: float, source: str, annual: float | None = None,
                announced_on: date | None = None) -> bool:
    """Oranı kaydeder; düşük öncelikli kaynak (yerleşik < elle < resmi) yüksek önceliklinin üzerine yazamaz."""
    if not re.fullmatch(r"\d{4}-\d{2}", period) or not (0 <= avg12 < 300):
        raise ValueError("Geçersiz TÜFE kaydı.")
    current = db.one("SELECT source FROM cpi_rates WHERE period=?", (period,))
    if current and SOURCE_PRIORITY.get(current["source"], 0) > SOURCE_PRIORITY.get(source, 0):
        return False
    db.execute(
        "INSERT INTO cpi_rates (period, avg12, annual, announced_on, source, fetched_at) VALUES (?,?,?,?,?,?)"
        " ON CONFLICT(period) DO UPDATE SET avg12=excluded.avg12, annual=COALESCE(excluded.annual, annual),"
        " announced_on=excluded.announced_on, source=excluded.source, fetched_at=excluded.fetched_at",
        (period, round(avg12, 2), annual, (announced_on or announcement_for(period)).isoformat(), source,
         now_iso()),
    )
    return True


def expected_latest_period(today: date) -> str:
    """Bugün itibarıyla açıklanmış olması gereken en son veri ayı."""
    p = shift_period(period_of(today), -1)
    while announcement_for(p) > today:
        p = shift_period(p, -1)
    return p


def needs_refresh(db, today: date) -> bool:
    return db.one("SELECT 1 FROM cpi_rates WHERE period=?", (expected_latest_period(today),)) is None


# ---------------------------------------------------------------- TÜİK bülteni
def _text(html_text: str) -> str:
    t = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", html_text)
    t = re.sub(r"<[^>]+>", " ", t)
    return re.sub(r"\s+", " ", htmllib.unescape(t)).strip()


def _num(raw: str, word: str) -> float:
    v = float(raw.replace(".", "").replace(",", ".")) if "," in raw else float(raw)
    return -v if word.lower().startswith("azal") else v


_HEADLINE = re.compile(
    r"TÜFE\s*['’`]?\s*deki\s*(?:\([^)]*\))?\s*değişim\s+(\d{4})\s+yılı\s+([A-Za-zÇĞİÖŞÜçğıöşü]+)\s+ayında(.{0,500}?)gerçekleşti",
    re.IGNORECASE | re.DOTALL,
)
_AVG12 = re.compile(r"on\s+iki\s+aylık\s+ortalamalara\s+göre\s+%\s*([0-9]+(?:[.,][0-9]+)?)\s+(artış|azalış)", re.I)
_ANNUAL = re.compile(r"önceki\s+yılın\s+aynı\s+ayına\s+göre\s+%\s*([0-9]+(?:[.,][0-9]+)?)\s+(artış|azalış)", re.I)


def parse_bulletin(page: str) -> ParsedCpi | None:
    """TÜİK TÜFE bülteninin manşet cümlesinden veri ayını ve 12 aylık ortalamayı çıkarır."""
    text = _text(page)
    m = _HEADLINE.search(text)
    if not m:
        return None
    month = MONTHS.get(m.group(2).lower().replace("i̇", "i"))
    clause = m.group(3)
    a = _AVG12.search(clause)
    if not month or not a:
        return None
    annual = _ANNUAL.search(clause)
    return ParsedCpi(period=f"{int(m.group(1)):04d}-{month:02d}", avg12=_num(*a.groups()),
                     annual=_num(*annual.groups()) if annual else None)


_LINK = re.compile(r"""href=["']([^"']*Tuketici-Fiyat-Endeksi-([A-Za-z]+)-(\d{4})-\d+[^"']*)["']""", re.I)


def find_bulletin_links(listing_html: str, base_url: str) -> list[tuple[str, str]]:
    """Kategori sayfasındaki TÜFE bülteni bağlantıları: [(veri_ayı, url)], en yeni önce."""
    found: dict[str, str] = {}
    for href, month_name, year in _LINK.findall(listing_html):
        month = MONTHS.get(month_name.lower())
        if month:
            found.setdefault(f"{int(year):04d}-{month:02d}", urljoin(base_url, htmllib.unescape(href)))
    return sorted(found.items(), reverse=True)


# ---------------------------------------------------------------- TCMB EVDS
def parse_evds(payload: dict, series: str) -> dict[str, float]:
    key = series.replace(".", "_")
    out: dict[str, float] = {}
    for item in payload.get("items", []):
        raw, tarih = item.get(key), str(item.get("Tarih", ""))
        m = re.fullmatch(r"(\d{4})-(\d{1,2})", tarih)
        if raw in (None, "", "null") or not m:
            continue
        out[f"{int(m.group(1)):04d}-{int(m.group(2)):02d}"] = float(raw)
    return out


def avg12_from_index(index: dict[str, float]) -> dict[str, float]:
    """Endeks seviyelerinden 12 aylık ortalamalara göre değişim: ort(son 12 ay) / ort(önceki 12 ay) − 1."""
    out: dict[str, float] = {}
    for p in sorted(index):
        months = [shift_period(p, -i) for i in range(24)]
        if all(m in index for m in months):
            recent = sum(index[m] for m in months[:12]) / 12
            prior = sum(index[m] for m in months[12:]) / 12
            out[p] = round((recent / prior - 1) * 100, 2)
    return out


# ---------------------------------------------------------------- canlı çekme
def default_get(url: str, headers: dict) -> tuple[int, str, str]:
    import httpx

    r = httpx.get(url, headers={"User-Agent": USER_AGENT, **headers}, timeout=20, follow_redirects=True)
    return r.status_code, r.text, str(r.url)


def fetch_tuik(get: HttpGet, list_url: str = TUIK_LIST_URL) -> ParsedCpi | None:
    status, body, final = get(list_url, {"Accept-Language": "tr-TR,tr;q=0.9"})
    if status != 200:
        raise RuntimeError(f"TÜİK kategori sayfası HTTP {status}")
    for period, url in find_bulletin_links(body, final)[:3]:
        st, page, _ = get(url, {"Accept-Language": "tr-TR,tr;q=0.9"})
        if st != 200:
            continue
        parsed = parse_bulletin(page)
        if parsed:
            return parsed
    return None


def fetch_evds(get: HttpGet, api_key: str, series: str = "TP.FG.J0", base_url: str = EVDS_BASE_URL,
               today: date | None = None) -> list[ParsedCpi]:
    today = today or date.today()
    start = add_months(today.replace(day=1), -40)
    url = (f"{base_url.rstrip('/')}/series={series}&startDate={start:%d-%m-%Y}&endDate={today:%d-%m-%Y}"
           f"&type=json&frequency=5")
    status, body, _ = get(url, {"key": api_key})
    if status != 200:
        raise RuntimeError(f"EVDS HTTP {status}")
    import json

    rates = avg12_from_index(parse_evds(json.loads(body), series))
    return [ParsedCpi(period=p, avg12=v) for p, v in sorted(rates.items())[-12:]]


@dataclass
class RefreshResult:
    ok: bool
    source: str | None = None
    periods: list[str] | None = None
    message: str = ""


def refresh(db, today: date | None = None, *, get: HttpGet | None = None, evds_key: str = "",
            evds_series: str = "TP.FG.J0", evds_url: str = EVDS_BASE_URL, tuik_url: str = TUIK_LIST_URL) -> RefreshResult:
    """Canlı kaynaklardan en güncel 12 aylık ortalamaları çeker ve kaydeder."""
    today = today or date.today()
    get = get or default_get
    errors: list[str] = []
    stored: list[str] = []
    source = None
    if evds_key:
        try:
            parsed_list = [p for p in fetch_evds(get, evds_key, evds_series, evds_url, today)
                           if announcement_for(p.period) <= today]
            if parsed_list:
                source = "evds"
                stored += [p.period for p in parsed_list if upsert_rate(db, p.period, p.avg12, "evds")]
            else:
                errors.append("EVDS: seri boş döndü (seri kodunu kontrol edin)")
        except Exception as e:  # noqa: BLE001 - bir kaynağın hatası diğerini denemeyi engellememeli
            errors.append(f"EVDS: {e}")
    if source is None:
        try:
            parsed = fetch_tuik(get, tuik_url)
            if parsed:
                source = "tuik"
                if upsert_rate(db, parsed.period, parsed.avg12, "tuik", annual=parsed.annual,
                               announced_on=min(today, announcement_for(parsed.period))):
                    stored.append(parsed.period)
            else:
                errors.append("TÜİK: bülten bulunamadı veya ayrıştırılamadı")
        except Exception as e:  # noqa: BLE001
            errors.append(f"TÜİK: {e}")
    if source:
        msg = f"{SOURCE_LABELS[source]}: {', '.join(stored) or 'yeni veri yok'}"
    else:
        msg = "; ".join(errors) or "Yeni veri yok"
    db.set_setting("tufe_last_attempt", now_iso())
    db.set_setting("tufe_last_result", msg[:300])
    (log.info if source else log.warning)("TÜFE güncelleme: %s", msg)
    return RefreshResult(ok=source is not None, source=source, periods=stored, message=msg)
