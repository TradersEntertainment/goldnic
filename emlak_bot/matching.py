"""Müşteri talebi ayrıştırma ve ilan / daire eşleştirme puanlaması (saf fonksiyonlar).

Danışmanın yazdığı serbest cümle ("Mehmet Bey 0532 111 22 33, 3+1 kiralık arıyor, bütçe 30-35 bin,
Yahya Kaptan veya Alikahya istiyor, acelesi var") yapılandırılmış ölçütlere çevrilir. Sahibinden
ilanları ve boşalan / boşalacak daireler bu ölçütlere göre 0–100 arası puanlanır: tür, oda, bütçe ve
konum zorunlu süzgeçtir; olanaklar ve hazır olma durumu puanı ince ayarlar. Her puanın gerekçesi
("3+1 ✓", "Bütçenin %6 üstü") danışmana gösterilir.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

from . import features as F
from .db import normalize_title as norm

MATCH_MIN = 70      # bu puanın altı eşleşme sayılmaz
STRONG = 85         # "tam uyum"
BUDGET_OVER = 0.10  # bütçenin %10 üstüne kadar pazarlık payı
BUDGET_UNDER = 0.50  # alt sınırın yarısından ucuzsa farklı segment sayılır

DEAL_LABELS = {"kiralik": "Kiralık", "satilik": "Satılık"}
URGENCY_LABELS = {"acil": "Acil", "normal": "Normal", "esnek": "Acelesi yok"}
CATEGORY_LABELS = {"konut": "Konut", "isyeri": "İşyeri", "arsa": "Arsa / tarla"}
# Birbirinin yerine geçebilen türler (daire arayan rezidansı, villa arayan müstakili de değerlendirir)
KIND_COMPAT = {"daire": {"rezidans"}, "rezidans": {"daire"}, "villa": {"mustakil"}, "mustakil": {"villa"},
               "ofis": {"dukkan"}, "dukkan": {"ofis"}, "arsa": {"tarla"}, "tarla": {"arsa"}}

_FOLD = str.maketrans({"İ": "i", "I": "i", "ı": "i", "Ğ": "g", "ğ": "g", "Ü": "u", "ü": "u", "Ş": "s", "ş": "s",
                       "Ö": "o", "ö": "o", "Ç": "c", "ç": "c", "Â": "a", "â": "a", "Î": "i", "î": "i",
                       "Û": "u", "û": "u"})

PHONE_RE = re.compile(r"(?<!\d)(?:\+?90[\s.-]*)?0?[\s.(-]*5\d{2}[\s.)-]*\d{3}[\s.-]*\d{2}[\s.-]*\d{2}(?!\d)")
NAME_RE = re.compile(r"\b([A-ZÇĞİÖŞÜ][a-zçğıöşü]+(?:\s+[A-ZÇĞİÖŞÜ][a-zçğıöşü]+)?)\s+(Bey|Hanım|Hanim|Bay|Bayan)\b")
LEAD_NAME_RE = re.compile(r"^\s*([A-ZÇĞİÖŞÜ][a-zçğıöşü]+(?:\s+[A-ZÇĞİÖŞÜ][a-zçğıöşü]+){0,2})(?=\s*(?:[,:;–-]|\+?\(?\d|$))")
NOT_NAMES = {"kiralik", "satilik", "daire", "villa", "dukkan", "ofis", "arsa", "musteri", "ogrenci", "acil", "aile"}
NUM = r"\d+(?:[.,]\d+)*"
UNIT = r"(milyon[a-z]*|mn\b|bin[a-z]*|k\b)"
RANGE_RE = re.compile(rf"({NUM})\s*{UNIT}?\s*(?:tl|₺|lira)?\s*(?:-|–|—|ile|ila|/)\s*({NUM})\s*{UNIT}?")
SINGLE_RE = re.compile(rf"({NUM})\s*{UNIT}?(\s*(?:tl|₺|lira))?")
ROOMS_TEXT_RE = re.compile(r"(?<!\d)([1-9])\s*oda\s*([0-9])\s*salon")
MIN_ROOMS_RE = re.compile(
    r"(?:en az|minimum|min\.?|asgari)\s*([1-9])\s*\+\s*[0-9]"
    r"|([1-9])\s*\+\s*[0-9]\s*(?:ve|veya)?\s*(?:uzeri|ustu|daha buyuk|yukarisi)")
MAX_HINT = ("en fazla", "en cok", "max", "maks", "ust limit", "en yuksek")
MAX_AFTER = ("kadar", "altinda", "alti ", "gecmesin", "gecmeyecek", "ustu olmasin", "uzeri olmasin")
MIN_HINT = ("en az", "minimum", "min ", "en dusuk", "alt limit")
MIN_AFTER = ("uzeri", "ustu", "den fazla", "dan fazla", "ten fazla", "tan fazla")
URGENT_RE = re.compile(r"\bacil\b|\bacilen\b|acelesi var|\bacele\b|\bhemen\b|en kisa sure|ivedi|bu hafta|bu ay icinde"
                       r"|derhal|cok acil")
RELAXED_RE = re.compile(r"acelesi yok|acele etmiyor|acil degil|esnek|zamani var|bakiniyor|acele yok")
SALE_RE = re.compile(r"\bsatilik\b|satin al|\balmak\b|\balim\b|\balici\b|yatirimlik|\bkredi|alacak\b|\bmulk alma")
RENT_RE = re.compile(r"\bkiralik\b|\bkiraci\b|\bkira\b|kiralamak|kiralayacak|kiralanacak")


def fold(text: str | None) -> str:
    """Türkçe karakterleri sadeleştirip küçük harfe çevirir; noktalama (+ - . ,) korunur."""
    return (text or "").translate(_FOLD).lower()


def _csv(value) -> set[str]:
    if not value:
        return set()
    if isinstance(value, (set, list, tuple)):
        return {str(v).strip() for v in value if str(v).strip()}
    return {v.strip() for v in str(value).split(",") if v.strip()}


def beds(rooms: str | None) -> int | None:
    try:
        return int(rooms.split("+")[0]) if rooms else None
    except (ValueError, AttributeError):
        return None


# ---------------------------------------------------------------- ölçütler
@dataclass
class Criteria:
    deal_type: str = "kiralik"
    category: str = "konut"
    kinds: set[str] = field(default_factory=set)
    rooms: set[str] = field(default_factory=set)
    rooms_min: int | None = None
    budget_min: float | None = None
    budget_max: float | None = None
    districts: set[str] = field(default_factory=set)
    hoods: set[str] = field(default_factory=set)
    amenities: set[str] = field(default_factory=set)
    avoid: set[str] = field(default_factory=set)
    urgency: str = "normal"

    @classmethod
    def from_row(cls, row) -> Criteria:
        r = dict(row)
        amen = _csv(r.get("amenities"))
        return cls(
            deal_type=r.get("deal_type") or "kiralik", category=r.get("category") or "konut",
            kinds=_csv(r.get("kinds")), rooms=_csv(r.get("rooms")), rooms_min=r.get("rooms_min"),
            budget_min=r.get("budget_min"), budget_max=r.get("budget_max"), districts=_csv(r.get("districts")),
            hoods=_csv(r.get("hoods")), amenities={a for a in amen if not a.startswith("!")},
            avoid={a[1:] for a in amen if a.startswith("!")}, urgency=r.get("urgency") or "normal",
        )

    def to_fields(self) -> dict:
        """client_demands sütunlarına yazılacak değerler."""
        j = lambda s: ",".join(sorted(s)) or None  # noqa: E731
        return {
            "deal_type": self.deal_type, "category": self.category, "kinds": j(self.kinds), "rooms": j(self.rooms),
            "rooms_min": self.rooms_min, "budget_min": self.budget_min, "budget_max": self.budget_max,
            "districts": j(self.districts), "hoods": j(self.hoods),
            "amenities": j(self.amenities | {"!" + a for a in self.avoid}), "urgency": self.urgency,
        }

    @property
    def all_districts(self) -> set[str]:
        """Açıkça istenen ilçeler + seçilen mahallelerin ilçeleri."""
        return self.districts | {F.NEIGHBORHOODS[h] for h in self.hoods if h in F.NEIGHBORHOODS}

    def is_vague(self) -> bool:
        return not (self.rooms or self.rooms_min or self.budget_max or self.budget_min or self.hoods
                    or self.districts or self.kinds)


def money_short(x: float | None) -> str:
    if not x:
        return "-"
    if x >= 1_000_000:
        s = f"{x / 1_000_000:.2f}".rstrip("0").rstrip(".").replace(".", ",")
        return f"{s} milyon ₺"
    return f"{x:,.0f}".replace(",", ".") + " ₺"


def budget_label(c: Criteria) -> str:
    lo, hi = c.budget_min, c.budget_max
    if lo and hi:
        return f"{money_short(lo)} – {money_short(hi)}"
    if hi:
        return f"en fazla {money_short(hi)}"
    if lo:
        return f"en az {money_short(lo)}"
    return ""


def rooms_label(c: Criteria) -> str:
    parts = sorted(c.rooms, key=lambda r: (beds(r) or 0, r))
    if c.rooms_min:
        parts.append(f"{c.rooms_min}+1 ve üzeri")
    return ", ".join(parts)


def chips(c: Criteria) -> list[tuple[str, str, str]]:
    """Ölçütlerin arayüz etiketleri: (ikon, metin, sınıf)."""
    out: list[tuple[str, str, str]] = [("key" if c.deal_type == "kiralik" else "coins", DEAL_LABELS[c.deal_type], "b-gold")]
    if c.kinds:
        out.append(("home", " / ".join(F.KIND_LABELS.get(k, k) for k in sorted(c.kinds)), ""))
    elif c.category != "konut":
        out.append(("building", CATEGORY_LABELS.get(c.category, c.category), ""))
    if c.rooms or c.rooms_min:
        out.append(("grid", rooms_label(c), ""))
    if c.budget_min or c.budget_max:
        out.append(("coins", budget_label(c), "b-info"))
    for h in sorted(c.hoods):
        out.append(("pin", F.hood_label(h), "b-ok"))
    for d in sorted(c.districts - {F.NEIGHBORHOODS.get(h) for h in c.hoods}):
        out.append(("pin", F.DISTRICTS.get(d, d), "b-ok"))
    for a in sorted(c.amenities):
        out.append(("check", F.AMENITY_LABELS.get(a, a), ""))
    for a in sorted(c.avoid):
        out.append(("x", F.AMENITY_LABELS.get(a, a) + " olmasın", ""))
    if c.urgency == "acil":
        out.append(("alert", "Acil", "b-err"))
    elif c.urgency == "esnek":
        out.append(("clock", "Acelesi yok", ""))
    return out


def summary(c: Criteria) -> str:
    """Tek satırlık düz metin özet (Telegram / WhatsApp)."""
    return " · ".join(t for _, t, _ in chips(c))


# ---------------------------------------------------------------- serbest metin ayrıştırma
@dataclass
class Parsed:
    criteria: Criteria
    name: str = ""
    phone: str = ""
    found: dict = field(default_factory=dict)   # hangi alanlar metinden çıkarıldı (arayüzde vurgu için)


def _amount(num: str, unit: str | None) -> float | None:
    unit = (unit or "").strip()
    mult = 1_000_000 if unit.startswith(("milyon", "mn")) else 1_000 if unit.startswith(("bin", "k")) else 1
    if mult > 1 and re.fullmatch(r"\d+[.,]\d{1,2}", num):
        v = float(num.replace(",", "."))                         # 3,5 milyon / 2.5 milyon
    elif re.fullmatch(r"\d{1,3}(?:[.,]\d{3})+", num):
        v = float(re.sub(r"[.,]", "", num))                      # 30.000 / 1.250.000
    elif num.isdigit():
        v = float(num)
    else:
        return None
    return v * mult


def _plausible(v: float | None, has_unit: bool, raw: str) -> bool:
    if not v:
        return False
    if not has_unit and (v < 5000 or (len(raw) == 4 and 1950 <= v <= 2100)):  # küçük sayılar ve yıllar
        return False
    return True


def parse_budget(t: str) -> tuple[float | None, float | None]:
    """Katlanmış (fold) metinden bütçe aralığı. (min, max) döner."""
    t = PHONE_RE.sub(" ", t)
    t = F.ROOM_RE.sub(" ", t)
    t = F.SQM_RE.sub(" ", t)
    t = re.sub(r"\b(?:19|20)\d{2}\b(?!\s*(?:tl|₺|lira))", " ", t)   # yıllar
    for m in RANGE_RE.finditer(t):
        n1, u1, n2, u2 = m.group(1), m.group(2), m.group(3), m.group(4)
        u1 = u1 or u2
        a, b = _amount(n1, u1), _amount(n2, u2)
        if a and b and not (u1 or u2) and a < 1000 and b < 1000:
            ctx = t[max(0, m.start() - 20): m.end() + 12]
            if "butce" in ctx or "tl" in ctx or "₺" in ctx:
                a, b = a * 1000, b * 1000
        if _plausible(a, bool(u1), n1) and _plausible(b, bool(u2), n2):
            return (min(a, b), max(a, b))
    lo = hi = None
    plain: list[float] = []
    for m in SINGLE_RE.finditer(t):
        num, unit, cur = m.group(1), m.group(2), m.group(3)
        v = _amount(num, unit)
        if not _plausible(v, bool(unit or cur), num):
            continue
        before, after = t[max(0, m.start() - 18): m.start()], t[m.end(): m.end() + 22]
        if re.match(r"\s*(?:civar|gibi|falan|dolay|cvr)", after):   # "5 milyon civarı" → yaklaşık aralık
            return (round(v * 0.8), v)
        if any(h in before for h in MAX_HINT) or any(after.lstrip().startswith(h) or f" {h}" in after[:14]
                                                      for h in MAX_AFTER):
            hi = hi or v
        elif any(h in before for h in MIN_HINT) or any(h in after[:16] for h in MIN_AFTER):
            lo = lo or v
        else:
            plain.append(v)
    if plain and not hi:
        if len(plain) >= 2 and not lo:
            lo, hi = min(plain[:2]), max(plain[:2])
        else:
            hi = plain[0]
    if lo and hi and lo > hi:
        lo, hi = hi, lo
    return lo, hi


def parse_rooms(t: str) -> tuple[set[str], int | None]:
    rooms: set[str] = set()
    rooms_min = None
    for m in MIN_ROOMS_RE.finditer(t):
        rooms_min = int(m.group(1) or m.group(2))
    clean = MIN_ROOMS_RE.sub(" ", t)
    for m in F.ROOM_RE.finditer(clean):
        rooms.add(f"{m.group(1)}+{m.group(2)}")
    for m in ROOMS_TEXT_RE.finditer(clean):
        rooms.add(f"{m.group(1)}+{m.group(2)}")
    if re.search(r"\bstudyo\b", clean):
        rooms.add("1+0")
    return rooms, rooms_min


def parse_kinds(n: str) -> set[str]:
    for hood in F.NEIGHBORHOODS:          # "Kent Konut", "Tepe Rezidence" gibi site adları tür sayılmaz
        if " " in hood or hood in ("toki",):
            n = re.sub(rf"(?<![a-z]){re.escape(hood)}", " ", n)
    found = set()
    for kind, words in F.KINDS:
        if any(F._has(n, w) for w in words):
            found.add(kind)
    if "daire" in found and found & {"villa", "mustakil", "rezidans"} and not re.search(r"\bdaire\b", n):
        found.discard("daire")   # "dubleks villa" gibi: dubleks kelimesi daireye çevrilmesin
    return found


def category_of(kinds: set[str], n: str = "") -> str:
    if kinds & F.COMMERCIAL:
        return "isyeri"
    if kinds & F.LAND:
        return "arsa"
    if not kinds and re.search(r"\b(?:ticari|isyeri|is yeri)\b", n):
        return "isyeri"
    return "konut"


def parse(text: str) -> Parsed:
    raw = (text or "").strip()
    t = fold(raw)
    n = norm(raw)
    found: dict[str, bool] = {}

    phone = ""
    pm = PHONE_RE.search(raw)
    if pm:
        d = re.sub(r"\D", "", pm.group(0))
        d = d[2:] if d.startswith("90") and len(d) == 12 else d
        d = d if d.startswith("0") else "0" + d
        phone = f"{d[:4]} {d[4:7]} {d[7:9]} {d[9:11]}" if len(d) == 11 else pm.group(0).strip()
        found["phone"] = True
    name = ""
    nm = NAME_RE.search(raw)
    if nm:
        name = f"{nm.group(1)} {nm.group(2).replace('Hanim', 'Hanım')}"
        found["name"] = True
    else:
        lm = LEAD_NAME_RE.match(raw)
        words = {norm(w) for w in (lm.group(1).split() if lm else [])}
        if lm and not (words & (NOT_NAMES | set(F.DISTRICTS))) and norm(lm.group(1)) not in F.NEIGHBORHOODS:
            name = lm.group(1)
            found["name"] = True

    c = Criteria()
    lo, hi = parse_budget(t)
    c.budget_min, c.budget_max = lo, hi
    if lo or hi:
        found["budget"] = True

    sale, rent = SALE_RE.search(t), RENT_RE.search(t)
    if sale and (not rent or sale.start() < rent.start()):
        c.deal_type = "satilik"
    elif rent:
        c.deal_type = "kiralik"
    elif (hi or lo or 0) >= 750_000:
        c.deal_type = "satilik"   # 750 bin üstü bütçe kira olamaz
    found["deal_type"] = bool(sale or rent)

    c.rooms, c.rooms_min = parse_rooms(t)
    found["rooms"] = bool(c.rooms or c.rooms_min)
    c.kinds = parse_kinds(n)
    c.category = category_of(c.kinds, n)
    c.hoods = {h.replace("yahyakaptan", "yahya kaptan") for h in F.hoods_of(n)}
    c.districts = {k for k in F.DISTRICTS if re.search(rf"\b{k}\b", n)}
    found["location"] = bool(c.hoods or c.districts)
    c.amenities = F.amenities_of(n)
    if "site" in c.amenities and not re.search(r"\bsite", n):
        c.amenities.discard("site")       # "rezidans" kelimesi tür olarak sayıldı; site şartı değil
    if re.search(r"esyasiz|bos daire|mobilyasiz", n):
        c.avoid.add("esyali")
        c.amenities.discard("esyali")
    if "dubleks" in c.amenities and "dubleks" not in n and "dublex" not in n:
        c.amenities.discard("dubleks")
    if RELAXED_RE.search(t):
        c.urgency = "esnek"
    elif URGENT_RE.search(t):
        c.urgency = "acil"
    found["urgency"] = c.urgency != "normal"
    return Parsed(criteria=c, name=name, phone=phone, found=found)


# ---------------------------------------------------------------- adaylar ve puan
@dataclass
class Candidate:
    type: str                      # listing | property
    id: int
    title: str
    price: float | None
    deal_type: str                 # kiralik | satilik
    feats: F.Features
    url: str = ""
    location: str = ""
    image_url: str = ""
    agent_id: int | None = None
    available_on: date | None = None   # boşalacak dairelerde boşalma tarihi
    note: str = ""                 # "Boş · 12 gündür", "Boşalacak: 01.12.2026"


@dataclass
class MatchResult:
    score: int
    reasons: list[tuple[str, str]]  # (metin, sınıf: ok | warn | mute)

    @property
    def strong(self) -> bool:
        return self.score >= STRONG


WEIGHTS = {"kind": 8, "rooms": 25, "budget": 27, "location": 25, "amenities": 10, "ready": 5}
UNSPECIFIED = 0.6


def score(c: Criteria, x: Candidate, today: date | None = None) -> MatchResult | None:
    """Adayı talebe göre puanlar; zorunlu ölçütlerden biri tutmazsa None."""
    if x.deal_type != c.deal_type:
        return None
    f = x.feats
    fit: dict[str, float] = {}
    why: list[tuple[str, str]] = []

    # tür / kategori
    if c.kinds:
        if f.kind in c.kinds:
            fit["kind"] = 1.0
        elif KIND_COMPAT.get(f.kind, set()) & c.kinds:
            fit["kind"] = 0.85
            why.append((F.KIND_LABELS.get(f.kind, f.kind), "mute"))
        else:
            return None
    else:
        if f.category != c.category:
            return None
        fit["kind"] = UNSPECIFIED

    # oda sayısı
    if c.rooms or c.rooms_min:
        b = beds(f.rooms)
        if f.rooms in c.rooms or (c.rooms_min and b is not None and b >= c.rooms_min):
            fit["rooms"] = 1.0
            why.append((f"{f.rooms} ✓", "ok"))
        elif f.rooms and b is not None and b in {beds(r) for r in c.rooms}:
            fit["rooms"] = 0.75
            why.append((f"{f.rooms} (oda sayısı uygun)", "warn"))
        elif f.rooms:
            return None
        else:
            fit["rooms"] = 0.35
            why.append(("Oda sayısı belirtilmemiş", "mute"))
    else:
        fit["rooms"] = UNSPECIFIED

    # bütçe
    price = x.price or 0
    lo, hi = c.budget_min, c.budget_max
    if lo or hi:
        if price <= 0:
            fit["budget"] = 0.3
            why.append(("Fiyat belirsiz", "mute"))
        elif hi and price > hi * (1 + BUDGET_OVER):
            return None
        elif lo and price < lo * BUDGET_UNDER:
            return None
        elif hi and price > hi:
            over = (price - hi) / hi
            fit["budget"] = 1 - 0.5 * over / BUDGET_OVER
            why.append((f"Bütçenin %{over * 100:.0f} üstü — pazarlık", "warn"))
        elif lo and price < lo:
            fit["budget"] = 0.7
            why.append(("Bütçenin altında", "ok"))
        else:
            fit["budget"] = 1.0
            why.append(("Bütçe içinde ✓", "ok"))
    else:
        fit["budget"] = UNSPECIFIED

    # konum
    if c.hoods or c.districts:
        cand_hoods = {h for h in f.hoods if not f.district or F.NEIGHBORHOODS.get(h) in (None, f.district)}
        hit = c.hoods & cand_hoods
        if hit:
            fit["location"] = 1.0
            why.append((", ".join(F.hood_label(h) for h in sorted(hit)) + " ✓", "ok"))
        elif f.district and f.district in c.districts:
            fit["location"] = 0.85 if not c.hoods else 0.6
            why.append((F.DISTRICTS.get(f.district, f.district) + " ✓", "ok" if not c.hoods else "warn"))
        elif f.district and f.district in c.all_districts:
            fit["location"] = 0.25
            why.append((f"Aynı ilçe, farklı mahalle ({F.DISTRICTS.get(f.district, f.district)})", "warn"))
        elif f.district:
            return None
        else:
            fit["location"] = 0.3
            why.append(("Konum belirsiz", "mute"))
    else:
        fit["location"] = UNSPECIFIED

    # olanaklar
    if c.amenities or c.avoid:
        if c.avoid & f.amenities:
            fit["amenities"] = 0.0
            why.extend((F.AMENITY_LABELS.get(a, a) + " (istenmiyor)", "warn") for a in sorted(c.avoid & f.amenities))
        elif c.amenities:
            got = c.amenities & f.amenities
            fit["amenities"] = len(got) / len(c.amenities)
            why.extend((F.AMENITY_LABELS.get(a, a) + " ✓", "ok") for a in sorted(got))
            why.extend((F.AMENITY_LABELS.get(a, a) + " belirtilmemiş", "mute") for a in sorted(c.amenities - got))
        else:
            fit["amenities"] = 1.0
    else:
        fit["amenities"] = UNSPECIFIED

    # hazır olma
    if x.available_on and today:
        days = (x.available_on - today).days
        fit["ready"] = 1.0 if days <= 0 else 0.8 if days <= 30 else (0.2 if c.urgency == "acil" else 0.5)
        if days > 0:
            why.append((f"{days} gün sonra boşalıyor", "warn" if c.urgency == "acil" and days > 30 else "mute"))
    else:
        fit["ready"] = 1.0

    total = sum(WEIGHTS[k] * v for k, v in fit.items()) / sum(WEIGHTS.values())
    return MatchResult(score=round(total * 100), reasons=why)


def whatsapp_text(client_name: str, cand: Candidate, agent_name: str, brand: str) -> str:
    """Müşteriye WhatsApp'tan gidecek ilan mesajı."""
    hello = f"Merhaba {client_name}," if client_name else "Merhaba,"
    period = " / ay" if cand.deal_type == "kiralik" else ""
    lines = [
        hello,
        f"{brand}'dan {agent_name}. Aradığınız kriterlere uygun bir {'kiralık' if cand.deal_type == 'kiralik' else 'satılık'} "
        f"seçeneğimiz var:",
        "",
        f"🏠 {cand.title}",
    ]
    if cand.price:
        lines.append(f"💰 {money_short(cand.price)}{period}")
    if cand.location:
        lines.append(f"📍 {cand.location}")
    if cand.available_on:
        lines.append(f"📅 Müsait olma tarihi: {cand.available_on.strftime('%d.%m.%Y')}")
    if cand.url.startswith("http"):
        lines.append(f"🔗 {cand.url}")
    lines += ["", "Görmek isterseniz size uygun bir saatte gösterim ayarlayalım."]
    return "\n".join(lines)
