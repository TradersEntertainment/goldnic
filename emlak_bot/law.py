"""Türk kira hukuku motoru — saf fonksiyonlar (veritabanından bağımsız, test edilebilir).

Dayanaklar:
- TBK 344  : Yenilenen kira dönemlerinde artış, bir önceki kira yılının TÜFE 12 aylık
             ortalamalara göre değişim oranını geçemez. 5 yılı aşan / 5 yıldan sonra yenilenen
             sözleşmelerde ve her 5 yılda bir kira bedeli hâkimce hakkaniyete göre belirlenir.
- TBK 345  : Kira bedelinin belirlenmesi davası; yeni dönemden en geç 30 gün önce açılırsa ya da
             yeni dönemden önce yazılı artış bildirimi yapılıp dönem sonuna kadar açılırsa,
             belirlenen kira yeni dönemin başından itibaren kiracıyı bağlar.
- TBK 347  : Kiraya veren süre bitimine dayanarak fesih yapamaz; 10 yıllık uzama süresinin sonunda,
             bu süreyi izleyen her uzama yılının bitiminden en az 3 ay önce bildirimle gerekçesiz
             fesih hakkı doğar.
- TBK 352/1: Kiralananın tesliminden SONRA yazılı olarak verilen tahliye taahhüdüne dayanarak,
             taahhüt tarihinden itibaren 1 ay içinde icra takibi (İİK 272) veya dava.
- TBK 353  : Kiraya veren tahliye tarihinden önce kiracıya yazılı bildirimde bulunmuşsa bu süre
             1 yıl olur.
- 7409 s.K. ile TBK geçici m.1: 11.06.2022 – 01.07.2024 arasında yenilenen KONUT kiralarında artış
             %25'i geçemez (işyerleri kapsam dışı).

Bu modül hukuki tavsiye değildir; sürelerin takibi ve hatırlatılması içindir.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

KONUT_CAP_START = date(2022, 6, 11)
KONUT_CAP_END = date(2024, 7, 1)
KONUT_CAP_PCT = 25.0

TR_MONTH_NAMES = ("Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran", "Temmuz", "Ağustos", "Eylül",
                  "Ekim", "Kasım", "Aralık")

COMMITMENT_STATUSES = {
    "active": "Takipte",
    "executed": "İcra / dava başlatıldı",
    "evicted": "Tahliye edildi",
    "waived": "Vazgeçildi",
    "lapsed": "Süre geçti (hak düştü)",
}


# ---------------------------------------------------------------- tarih yardımcıları
def add_months(d: date, months: int) -> date:
    y, m = divmod(d.month - 1 + months, 12)
    year, month = d.year + y, m + 1
    for day in (d.day, 30, 29, 28):
        try:
            return date(year, month, min(d.day, day))
        except ValueError:
            continue
    raise ValueError(f"Geçersiz tarih: {d} + {months} ay")


def add_years(d: date, years: int) -> date:
    return add_months(d, 12 * years)


def period_of(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def period_date(period: str) -> date:
    y, m = period.split("-")
    return date(int(y), int(m), 1)


def shift_period(period: str, months: int) -> str:
    return period_of(add_months(period_date(period), months))


def period_label(period: str) -> str:
    d = period_date(period)
    return f"{TR_MONTH_NAMES[d.month - 1]} {d.year}"


def announcement_date(period: str) -> date:
    """TÜİK TÜFE bültenini izleyen ayın 3'ünde yayımlar; hafta sonuna denk gelirse pazartesi."""
    d = add_months(period_date(period), 1).replace(day=3)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def applicable_period(renewal: date) -> str:
    """Yeni kira dönemine uygulanacak TÜFE: yenileme ayından bir önceki ayın 12 aylık ortalaması."""
    return shift_period(period_of(renewal), -1)


def to_kurus(x: float | Decimal) -> Decimal:
    return Decimal(str(x)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def max_rent(rent: float, cap_pct: float) -> Decimal:
    """Yasal azami yeni kira (kuruş hassasiyetinde, yarım kuruş yukarı yuvarlanır)."""
    pct = Decimal(str(max(cap_pct, 0.0)))
    return to_kurus(Decimal(str(rent)) * (Decimal(1) + pct / Decimal(100)))


# ---------------------------------------------------------------- TÜFE tavanı
@dataclass(frozen=True)
class CpiRate:
    period: str          # verinin ait olduğu ay (YYYY-MM)
    avg12: float         # 12 aylık ortalamalara göre değişim (%)
    announced_on: date
    source: str          # tuik | evds | manual | builtin
    annual: float | None = None


@dataclass
class RentCap:
    renewal_date: date
    period: str                      # yenilemeye uygulanacak veri ayı
    cpi: float | None                # o ayın (veya en son bilinen ayın) 12 aylık ortalaması
    cap_pct: float | None            # yasal azami artış oranı
    rule: str                        # "tufe" | "konut25"
    provisional: bool                # uygulanacak ay henüz açıklanmadı; en son bilinen oran kullanıldı
    source: str | None
    used_period: str | None          # hesapta kullanılan veri ayı
    expected_on: date                # uygulanacak ayın açıklanma tarihi
    announced_on: date | None = None

    def max_rent(self, rent: float) -> Decimal | None:
        return None if self.cap_pct is None else max_rent(rent, self.cap_pct)

    @property
    def label(self) -> str:
        """Örn. 'Eylül 2026 TÜFE 12 aylık ort.' veya '%25 konut sınırı (7409 s.K.)'."""
        if self.cap_pct is None or self.used_period is None:
            return "TÜFE verisi yok"
        if self.rule == "konut25":
            return "%25 konut sınırı (7409 s.K.)"
        return f"{period_label(self.used_period)} TÜFE 12 aylık ort."


def rent_cap(renewal: date, rates: dict[str, CpiRate], kind: str = "konut") -> RentCap:
    period = applicable_period(renewal)
    rate = rates.get(period)
    provisional = False
    if rate is None:
        older = sorted((p for p in rates if p < period), reverse=True)
        rate = rates[older[0]] if older else None
        provisional = True
    cpi = rate.avg12 if rate else None
    cap, rule = cpi, "tufe"
    if (cpi is not None and kind == "konut" and KONUT_CAP_START <= renewal <= KONUT_CAP_END
            and cpi > KONUT_CAP_PCT):
        cap, rule = KONUT_CAP_PCT, "konut25"
    return RentCap(
        renewal_date=renewal, period=period, cpi=cpi, cap_pct=None if cap is None else max(cap, 0.0),
        rule=rule, provisional=provisional, source=rate.source if rate else None,
        used_period=rate.period if rate else None, expected_on=announcement_date(period),
        announced_on=rate.announced_on if rate else None,
    )


def latest_rate(rates: dict[str, CpiRate], on: date) -> CpiRate | None:
    """Belirli bir gün itibarıyla açıklanmış en güncel oran."""
    known = [r for r in rates.values() if r.announced_on <= on]
    return max(known, key=lambda r: r.period) if known else None


# ---------------------------------------------------------------- sözleşme zaman çizelgesi
@dataclass
class FiveYear:
    mark: date                 # 5., 10., 15. … yılın dolduğu gün (yeni kira yılının başı)
    years: int
    suit_deadline: date        # TBK 345/2: yeni dönemden en geç 30 gün önce dava
    notice_deadline: date      # yeni dönem başlamadan yazılı artış bildirimi için son gün
    period_end: date           # bildirim yapıldıysa davanın açılabileceği son gün
    days_to_mark: int


def five_year_status(start: date, today: date) -> FiveYear:
    k = 1
    while add_years(add_years(start, 5 * k), 1) <= today:
        k += 1
    mark = add_years(start, 5 * k)
    return FiveYear(mark=mark, years=5 * k, suit_deadline=mark - timedelta(days=30),
                    notice_deadline=mark - timedelta(days=1), period_end=add_years(mark, 1),
                    days_to_mark=(mark - today).days)


@dataclass
class TenYear:
    initial_end: date          # ilk sözleşme süresinin bitişi
    right_from: date           # 10 yıllık uzama süresinin sonu: gerekçesiz fesih hakkı doğar
    termination: date          # ilk kullanılabilir fesih (uzama yılı sonu) tarihi
    notice_deadline: date      # ihtarnamenin kiracıya ulaşması gereken son gün (fesihten 3 ay önce)
    days_to_right: int
    days_to_deadline: int


def ten_year_status(start: date, term_months: int, today: date) -> TenYear:
    initial_end = add_months(start, max(term_months, 1))
    right_from = add_years(initial_end, 10)
    k = 1
    while add_months(add_years(right_from, k), -3) < today:
        k += 1
    termination = add_years(right_from, k)
    deadline = add_months(termination, -3)
    return TenYear(initial_end=initial_end, right_from=right_from, termination=termination,
                   notice_deadline=deadline, days_to_right=(right_from - today).days,
                   days_to_deadline=(deadline - today).days)


# ---------------------------------------------------------------- tahliye taahhüdü
@dataclass
class CommitmentCheck:
    phase: str                       # before | window | lapsed | closed
    deadline: date                   # icra/dava için son gün
    notice_ok: bool                  # TBK 353 bildirimi tahliye tarihinden önce yapılmış mı
    days_to_eviction: int
    days_to_deadline: int
    issues: list[str] = field(default_factory=list)   # geçerlilik riskleri


def check_commitment(signed: date, eviction: date, today: date, delivery: date | None = None,
                     notice: date | None = None, status: str = "active") -> CommitmentCheck:
    issues: list[str] = []
    if delivery is None:
        issues.append("Kiralananın teslim tarihi girilmemiş: taahhütnamenin teslimden sonra "
                      "imzalandığı doğrulanamıyor.")
    elif signed <= delivery:
        issues.append("Taahhütname kiralananın tesliminden önce veya aynı gün imzalanmış. TBK 352/1 "
                      "teslimden SONRA verilmesini arar; aynı gün/önce verilen taahhüt Yargıtay "
                      "uygulamasında baskı altında verilmiş sayılabilir — geçersizlik riski.")
    if eviction <= signed:
        issues.append("Tahliye tarihi imza tarihinden sonra olmalı.")
    notice_ok = notice is not None and notice < eviction
    deadline = add_years(eviction, 1) if notice_ok else add_months(eviction, 1)
    if status != "active":
        phase = "closed"
    elif today < eviction:
        phase = "before"
    elif today <= deadline:
        phase = "window"
    else:
        phase = "lapsed"
    return CommitmentCheck(phase=phase, deadline=deadline, notice_ok=notice_ok,
                           days_to_eviction=(eviction - today).days, days_to_deadline=(deadline - today).days,
                           issues=issues)


# ---------------------------------------------------------------- radar kalemleri
@dataclass
class LegalAlert:
    kind: str                  # tufe | taahhut | tespit5 | tahliye10 | veri
    severity: str              # critical | warning | info
    title: str
    detail: str
    due: date | None
    ref: str                   # tekrar bildirim/onay anahtarı (ör. tarih ya da taahhüt no)
    law_ref: str
    stage: str = ""            # bildirim aşaması (aynı aşama iki kez gönderilmez)


def fmt(d: date | None) -> str:
    return d.strftime("%d.%m.%Y") if d else "-"


def five_year_alert(start: date, today: date) -> LegalAlert | None:
    f = five_year_status(start, today)
    ref = f.mark.isoformat()
    title_base = f"{f.years}. yıl kira tespiti (rayiç yenileme) fırsatı"
    if 75 < f.days_to_mark <= 180:
        return LegalAlert("tespit5", "info", title_base,
                          f"{fmt(f.mark)} tarihinde {f.years + 1}. kira yılı başlıyor. Emsal kiralarla rayiç "
                          f"değerleme yapın; tespit davası için ideal son gün {fmt(f.suit_deadline)}.",
                          f.mark, ref, "TBK 344/3, 345", stage="180")
    if 30 < f.days_to_mark <= 75:
        return LegalAlert("tespit5", "warning", f"{title_base} — dava süresi işliyor",
                          f"Belirlenecek kiranın {fmt(f.mark)}'den itibaren bağlaması için davayı en geç "
                          f"{fmt(f.suit_deadline)} tarihine kadar açın ({f.days_to_mark - 30} gün kaldı).",
                          f.suit_deadline, ref, "TBK 345/2", stage="75")
    if 0 <= f.days_to_mark <= 30:
        return LegalAlert("tespit5", "critical", f"{title_base} — yazılı bildirim şart",
                          f"30 günlük dava süresi kaçtı/kaçmak üzere. Yeni dönem başlamadan ({fmt(f.notice_deadline)} "
                          f"son gün) kiracıya yazılı artış bildirimi gönderin; dava {fmt(f.period_end)} tarihine "
                          f"kadar açılabilir.", f.notice_deadline, ref, "TBK 345/2", stage="30")
    if f.days_to_mark < 0:
        return LegalAlert("tespit5", "info", f"{f.years + 1}. kira yılı başladı — tespit davası penceresi",
                          f"Yeni dönemden önce yazılı bildirim yaptıysanız kira tespit davasını "
                          f"{fmt(f.period_end)} tarihine kadar açabilirsiniz.", f.period_end, ref, "TBK 345/2",
                          stage="open")
    return None


def ten_year_alert(start: date, term_months: int, today: date) -> LegalAlert | None:
    t = ten_year_status(start, term_months, today)
    ref = t.termination.isoformat()
    if t.days_to_right > 180:
        return None
    if t.days_to_right > 90:
        return LegalAlert("tahliye10", "info", "10 yıllık uzama süresi doluyor",
                          f"{fmt(t.right_from)} tarihinde 10 yıllık uzama süresi sona eriyor. Gerekçesiz tahliye "
                          f"için ilk fesih tarihi {fmt(t.termination)}; ihtarname son günü {fmt(t.notice_deadline)}.",
                          t.right_from, ref, "TBK 347/1", stage="180")
    if t.days_to_right > 0:
        return LegalAlert("tahliye10", "critical", "KIRMIZI ALARM: 10. yıl gerekçesiz tahliye hakkı 3 ay içinde doğuyor",
                          f"Hak {fmt(t.right_from)} tarihinde doğar. Fesih {fmt(t.termination)} tarihinde hüküm "
                          f"doğurur; ihtarname en geç {fmt(t.notice_deadline)} tarihinde kiracıya ulaşmalı.",
                          t.right_from, ref, "TBK 347/1", stage="90")
    if t.days_to_deadline <= 30:
        severity, stage = "critical", "30"
    elif t.days_to_deadline <= 90:
        severity, stage = "warning", "90-deadline"
    else:
        severity, stage = "info", "open"
    return LegalAlert("tahliye10", severity, "10. yıl gerekçesiz tahliye hakkı kullanılabilir",
                      f"İhtarname son günü {fmt(t.notice_deadline)} ({t.days_to_deadline} gün). Tebliğ edilirse "
                      f"sözleşme {fmt(t.termination)} tarihinde sona erer. Kullanılmazsa hak bir sonraki uzama "
                      f"yılına devreder.", t.notice_deadline, ref, "TBK 347/1", stage=stage)


def commitment_alert(c: CommitmentCheck, eviction: date) -> LegalAlert | None:
    ref = eviction.isoformat()
    risk = (" ⚠️ " + c.issues[0]) if c.issues else ""
    if c.phase == "closed":
        return None
    if c.phase == "before":
        d = c.days_to_eviction
        if c.notice_ok:
            if d > 30:
                return None
            return LegalAlert("taahhut", "info", "Tahliye taahhüdü: tahliye tarihi yaklaşıyor (ihtar yapıldı)",
                              f"Tahliye tarihi {fmt(eviction)}. İhtar yapıldığı için icra takibi/dava için "
                              f"{fmt(c.deadline)} tarihine kadar süreniz var.{risk}", eviction, ref,
                              "TBK 352/1, 353", stage="notice-ok")
        if d > 60:
            return None
        severity = "critical" if d <= 7 else "warning"
        return LegalAlert("taahhut", severity, f"Tahliye taahhüdü: {d} gün kaldı — ihtarname gönderin",
                          f"Tahliye tarihi {fmt(eviction)}. Bu tarihten önce noter ihtarı çekerseniz icra/dava "
                          f"süreniz 1 aydan 1 yıla çıkar (TBK 353). İhtar yoksa son gün {fmt(c.deadline)}.{risk}",
                          eviction, ref, "TBK 352/1, 353", stage="7" if d <= 7 else ("30" if d <= 30 else "60"))
    if c.phase == "window":
        return LegalAlert("taahhut", "critical",
                          f"Tahliye taahhüdü: icra takibi / dava için son {max(c.days_to_deadline, 0)} gün",
                          f"Taahhüt edilen tahliye tarihi {fmt(eviction)} geçti. İİK 272 uyarınca icra takibi veya "
                          f"tahliye davası için son gün {fmt(c.deadline)}.{risk}", c.deadline, ref,
                          "TBK 352/1, İİK 272", stage="window")
    return LegalAlert("taahhut", "warning", "Tahliye taahhüdüne dayalı süre geçti",
                      f"{fmt(c.deadline)} tarihine kadar takip yapılmadı; bu taahhüde dayalı tahliye hakkı düştü "
                      f"ve sözleşme bir yıl uzamış sayılır. Kaydı 'süre geçti' olarak kapatın.", c.deadline, ref,
                      "TBK 353", stage="lapsed")
