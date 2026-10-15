"""İlan / mülk başlığından yapılandırılmış özellik çıkarımı (oda, tür, ilçe, mahalle, olanaklar).

Hem emsal kira (rayiç) tahmininde hem de müşteri talebi eşleştirmede kullanılır.
Tüm karşılaştırmalar Türkçe karakterleri sadeleştirilmiş küçük harfli metin üzerinden yapılır.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .db import normalize_title as norm

ROOM_RE = re.compile(r"(?<![\d.])([1-9])\s*\+\s*([0-9])(?!\d)")
SQM_RE = re.compile(r"(?<![\d.])(\d{2,5})\s*(?:m2|m²|metrekare)\b", re.I)

# İlçeler (sadeleştirilmiş ad → görünen ad)
DISTRICTS: dict[str, str] = {
    "izmit": "İzmit", "kartepe": "Kartepe", "basiskele": "Başiskele", "golcuk": "Gölcük", "korfez": "Körfez",
    "derince": "Derince", "dilovasi": "Dilovası", "gebze": "Gebze", "darica": "Darıca", "cayirova": "Çayırova",
    "kandira": "Kandıra", "karamursel": "Karamürsel", "adapazari": "Adapazarı", "sapanca": "Sapanca",
    "serdivan": "Serdivan", "arifiye": "Arifiye",
}

# Mahalle / semt / site adları → bulunduğu ilçe (eşleştirme ve emsal için)
NEIGHBORHOODS: dict[str, str] = {
    # İzmit
    "yahya kaptan": "izmit", "yahyakaptan": "izmit", "alikahya": "izmit", "tuysuzler": "izmit", "kabaoglu": "izmit",
    "bagcesme": "izmit", "topcular": "izmit", "erenler": "izmit", "gundogdu": "izmit", "yeni mahalle": "izmit",
    "orhan": "izmit", "sepetci": "izmit", "kozluk": "izmit", "cedit": "izmit", "kemalpasa": "izmit",
    "karabas": "izmit", "omeraga": "izmit", "serdar": "izmit", "turgut": "izmit", "yenidogan": "izmit",
    "yenisehir": "izmit", "akcekent": "izmit", "kurucesme": "izmit", "tavsantepe": "izmit", "tepekoy": "izmit",
    "akmese": "izmit", "arizli": "izmit", "gultepe": "izmit", "durhasan": "izmit", "malta": "izmit",
    "kocatepe": "izmit", "umuttepe": "izmit", "37 evler": "izmit", "fethiye caddesi": "izmit",
    "cumhuriyet parki": "izmit", "kent konut": "izmit", "tuana evleri": "izmit", "academic life": "izmit",
    "zafira": "izmit", "erenkaya": "izmit", "vadipark": "izmit", "aslantas": "izmit", "kavanium": "izmit",
    "tepe rezidence": "izmit", "toki": "izmit", "baglarbasi": "izmit", "hacihasan": "izmit", "mehmet ali pasa": "izmit",
    # Kartepe
    "masukiye": "kartepe", "uzunciftlik": "kartepe", "arslanbey": "kartepe", "sarimese": "kartepe",
    "kosekoy": "kartepe", "istasyon": "kartepe", "karatepe": "kartepe", "suadiye": "kartepe", "derbent": "kartepe",
    "acisu": "kartepe", "ataevler": "kartepe", "forum anatolia": "kartepe",
    # Başiskele
    "yuvacik": "basiskele", "sehitekrem": "basiskele", "bahcecik": "basiskele", "seymen": "basiskele",
    "kullar": "basiskele", "yenikoy": "basiskele", "barbaros": "basiskele", "manar city": "basiskele",
    "panorama": "basiskele", "bingo": "basiskele",
    # Körfez
    "yarimca": "korfez", "esentepe": "korfez", "kirazliyali": "korfez", "hereke": "korfez",
    "tutunciftlik": "korfez", "mimar sinan": "korfez",
    # Derince
    "tahtali": "derince", "cenedag": "derince", "dumlupinar": "derince", "cavuslu": "derince",
    # Gölcük
    "degirmendere": "golcuk", "ihsaniye": "golcuk", "halidere": "golcuk", "yazlik": "golcuk", "hisareyn": "golcuk",
    # Dilovası
    "tavsancil": "dilovasi", "diliskelesi": "dilovasi",
    # Gebze / Darıca
    "pelitli": "gebze", "mustafapasa": "gebze", "beylikbagi": "gebze", "bayramoglu": "darica",
    # Sapanca
    "akcay": "sapanca", "kirkpinar": "sapanca",
}

# Mahalle / site anahtarlarının görünen (Türkçe karakterli) adları
HOOD_LABELS: dict[str, str] = {
    "yahya kaptan": "Yahya Kaptan", "yahyakaptan": "Yahya Kaptan", "alikahya": "Alikahya", "tuysuzler": "Tüysüzler",
    "kabaoglu": "Kabaoğlu", "bagcesme": "Bağçeşme", "topcular": "Topçular", "erenler": "Erenler",
    "gundogdu": "Gündoğdu", "yeni mahalle": "Yeni Mahalle", "orhan": "Orhan", "sepetci": "Sepetçi",
    "kozluk": "Kozluk", "cedit": "Cedit", "kemalpasa": "Kemalpaşa", "karabas": "Karabaş", "omeraga": "Ömerağa",
    "serdar": "Serdar", "turgut": "Turgut", "yenidogan": "Yenidoğan", "yenisehir": "Yenişehir",
    "akcekent": "Akçekent", "kurucesme": "Kuruçeşme", "tavsantepe": "Tavşantepe", "tepekoy": "Tepeköy",
    "akmese": "Akmeşe", "arizli": "Arızlı", "gultepe": "Gültepe", "durhasan": "Durhasan", "malta": "Malta",
    "kocatepe": "Kocatepe", "umuttepe": "Umuttepe", "37 evler": "37 Evler", "fethiye caddesi": "Fethiye Caddesi",
    "cumhuriyet parki": "Cumhuriyet Parkı", "kent konut": "Kent Konut", "tuana evleri": "Tuana Evleri",
    "academic life": "Academic Life", "zafira": "Zafira", "erenkaya": "Erenkaya", "vadipark": "Vadipark",
    "aslantas": "Aslantaş", "kavanium": "Kavanium", "tepe rezidence": "Tepe Rezidence", "toki": "TOKİ",
    "baglarbasi": "Bağlarbaşı", "hacihasan": "Hacıhasan", "mehmet ali pasa": "Mehmet Ali Paşa",
    "masukiye": "Maşukiye", "uzunciftlik": "Uzunçiftlik", "arslanbey": "Arslanbey", "sarimese": "Sarımeşe",
    "kosekoy": "Köseköy", "istasyon": "İstasyon", "karatepe": "Karatepe", "suadiye": "Suadiye",
    "derbent": "Derbent", "acisu": "Acısu", "ataevler": "Ataevler", "forum anatolia": "Forum Anatolia",
    "yuvacik": "Yuvacık", "sehitekrem": "Şehitekrem", "bahcecik": "Bahçecik", "seymen": "Seymen",
    "kullar": "Kullar", "yenikoy": "Yeniköy", "barbaros": "Barbaros", "manar city": "Manar City",
    "panorama": "Panorama", "bingo": "Bingo", "yarimca": "Yarımca", "esentepe": "Esentepe",
    "kirazliyali": "Kirazlıyalı", "hereke": "Hereke", "tutunciftlik": "Tütünçiftlik", "mimar sinan": "Mimar Sinan",
    "tahtali": "Tahtalı", "cenedag": "Çenedağ", "dumlupinar": "Dumlupınar", "cavuslu": "Çavuşlu",
    "degirmendere": "Değirmendere", "ihsaniye": "İhsaniye", "halidere": "Halıdere", "yazlik": "Yazlık",
    "hisareyn": "Hisareyn", "tavsancil": "Tavşancıl", "diliskelesi": "Diliskelesi", "pelitli": "Pelitli",
    "mustafapasa": "Mustafapaşa", "beylikbagi": "Beylikbağı", "bayramoglu": "Bayramoğlu", "akcay": "Akçay",
    "kirkpinar": "Kırkpınar",
}


def hood_label(key: str) -> str:
    return HOOD_LABELS.get(key, key.title())


KINDS: list[tuple[str, tuple[str, ...]]] = [
    ("fabrika", ("fabrika", "uretim tesisi")),
    ("depo", ("depo", "antrepo")),
    ("dukkan", ("dukkan", "magaza")),
    ("ofis", ("ofis", "buro", "isyeri", "is yeri", "is hani")),
    ("bina", ("bina", "apartman komple")),
    ("arsa", ("arsa", "imarli")),
    ("tarla", ("tarla", "bahce arazi")),
    ("villa", ("villa",)),
    ("mustakil", ("mustakil",)),
    ("rezidans", ("rezidans", "residence", "rezidence")),
    ("daire", ("daire", "dubleks", "dublex", "arakat", "ara kat", "loft", "konut", "apart")),
]
RESIDENTIAL = {"daire", "villa", "mustakil", "rezidans"}
COMMERCIAL = {"dukkan", "ofis", "depo", "fabrika", "bina"}
LAND = {"arsa", "tarla"}

KIND_LABELS = {"daire": "Daire", "villa": "Villa", "mustakil": "Müstakil", "rezidans": "Rezidans", "dukkan": "Dükkan",
               "ofis": "Ofis / işyeri", "depo": "Depo", "fabrika": "Fabrika", "bina": "Bina", "arsa": "Arsa",
               "tarla": "Tarla"}

AMENITIES: dict[str, tuple[str, ...]] = {
    "esyali": ("esyali", "mobilyali"),
    "deniz": ("deniz manzara", "denize sifir", "deniz goren"),
    "manzara": ("manzarali", "onu acik"),
    "bahce": ("bahceli", "bahce kati", "bahce kullanim", "bahce dubleks"),
    "havuz": ("havuzlu", "havuz"),
    "site": ("site", "sitesi", "rezidans", "residence", "rezidence", "kent konut", "toki"),
    "otopark": ("otopark", "kapali otopark"),
    "asansor": ("asansor",),
    "dubleks": ("dubleks", "dublex", "cati dubleks"),
    "arakat": ("arakat", "ara kat"),
    "universite": ("universite", "kampus", "umuttepe"),
    "ulasim": ("tramvay", "metro", "marmaray", "istasyon", "d 100", "d100"),
}
AMENITY_LABELS = {"esyali": "Eşyalı", "deniz": "Deniz manzaralı", "manzara": "Manzaralı", "bahce": "Bahçeli",
                  "havuz": "Havuzlu", "site": "Site içi", "otopark": "Otopark", "asansor": "Asansörlü",
                  "dubleks": "Dubleks", "arakat": "Ara kat", "universite": "Üniversiteye yakın",
                  "ulasim": "Ulaşıma yakın"}


@dataclass
class Features:
    rooms: str | None = None
    kind: str = "daire"
    district: str | None = None
    hoods: set[str] = field(default_factory=set)
    amenities: set[str] = field(default_factory=set)
    sqm: int | None = None
    text: str = ""

    @property
    def category(self) -> str:
        if self.kind in COMMERCIAL:
            return "isyeri"
        if self.kind in LAND:
            return "arsa"
        return "konut"

    @property
    def room_count(self) -> int | None:
        if not self.rooms:
            return None
        a, b = self.rooms.split("+")
        return int(a) + int(b)


def rooms_of(text: str) -> str | None:
    t = text or ""
    if re.search(r"\bst[uü]dyo\b", t, re.I) or re.search(r"\b1\s*\+\s*0\b", t):
        return "1+0"
    m = ROOM_RE.search(t)
    return f"{m.group(1)}+{m.group(2)}" if m else None


def _has(n: str, word: str) -> bool:
    """Kelime başı eşleşmesi: soldaki karakter harf olmamalı ("universite" içinde "site" sayılmaz,
    "3 1daire" içinde "daire" sayılır)."""
    return re.search(rf"(?<![a-z]){re.escape(word.strip())}", n) is not None


def kind_of(n: str) -> str:
    """Türkçe ilan başlıkları sondan belirlenir ("… ARSA İÇERİSİNDE MÜSTAKİL VİLLA" → villa):
    başlıkta EN SON geçen tür kelimesi esas alınır."""
    best, best_pos = "daire", -1
    for kind, words in KINDS:
        for w in words:
            for m in re.finditer(rf"(?<![a-z]){re.escape(w.strip())}", n):
                if m.start() > best_pos:
                    best, best_pos = kind, m.start()
    return best


def district_of(n_location: str, n_title: str) -> str | None:
    for key in DISTRICTS:
        if re.search(rf"\b{key}\b", n_location):
            return key
    for key in DISTRICTS:
        if re.search(rf"\b{key}\b", n_title):
            return key
    for hood, dist in NEIGHBORHOODS.items():
        if hood in n_title:
            return dist
    return None


def hoods_of(n: str) -> set[str]:
    return {h.replace("yahyakaptan", "yahya kaptan") for h in NEIGHBORHOODS if _has(n, h)}


def amenities_of(n: str) -> set[str]:
    out = {k for k, words in AMENITIES.items() if any(_has(n, w) for w in words)}
    if _has(n, "esyasiz"):
        out.discard("esyali")
    return out


def extract(title: str, location: str = "") -> Features:
    n_title, n_loc = norm(title), norm(location)
    sqm = SQM_RE.search(title or "")
    return Features(
        rooms=rooms_of(title),
        kind=kind_of(n_title),
        district=district_of(n_loc, n_title),
        hoods=hoods_of(n_title),
        amenities=amenities_of(n_title),
        sqm=int(sqm.group(1)) if sqm else None,
        text=f"{n_title} {n_loc}".strip(),
    )


def median(values: list[float]) -> float | None:
    v = sorted(values)
    if not v:
        return None
    mid = len(v) // 2
    return v[mid] if len(v) % 2 else (v[mid - 1] + v[mid]) / 2
