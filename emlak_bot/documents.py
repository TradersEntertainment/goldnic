"""Resmi belge motoru (PDF): Kira Sözleşmesi, Tahliye Taahhütnamesi, Kiralanan Teslim / İade Tutanağı.

- Türkçe karakterler için Liberation Serif/Sans gömülür (SIL OFL; assets/fonts).
- Her sayfada Gold Nicomedia monogramı, filigran, belge no, doğrulama kodu ve "Sayfa X / Y".
- Genel şartlar 6098 sayılı TBK'nın emredici hükümleriyle uyumludur: kiracı aleyhine ceza koşulu
  veya muacceliyet maddesi yoktur (TBK 346); kefalet için el yazısı alanları boş bırakılır (TBK 583).
"""
from __future__ import annotations

import io
import threading
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from xml.sax.saxutils import escape as _xesc

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas as rl_canvas
from reportlab.platypus import BaseDocTemplate, Frame, KeepTogether, PageTemplate, Paragraph, Spacer, Table, TableStyle

from .law import add_months
from .validators import amount_words, iban_format, money_exact, number_words

FONT_DIR = Path(__file__).parent / "assets" / "fonts"
GOLD = colors.HexColor("#B8902F")
GOLD_SOFT = colors.HexColor("#F6EFD9")
NAVY = colors.HexColor("#0E1A33")
INK = colors.HexColor("#1B1F27")
MUTED = colors.HexColor("#5B6475")
LINE = colors.HexColor("#C8CDD6")
PAGE_W, PAGE_H = A4
MARGIN_X = 18 * mm

DOC_KINDS = {
    "kira_sozlesmesi": "Kira Sözleşmesi",
    "tahliye_taahhutnamesi": "Tahliye Taahhütnamesi",
    "teslim_tutanagi": "Teslim Tutanağı",
}
PROPERTY_KIND_LABELS = {"konut": "Konut", "isyeri": "Çatılı işyeri"}
STAMP_DUTY_RATE = Decimal("0.00189")   # 488 s. Damga Vergisi Kanunu (1) sayılı tablo: kira mukavelenameleri, binde 1,89

_font_lock = threading.Lock()
_fonts_ready = False


def register_fonts() -> None:
    global _fonts_ready
    with _font_lock:
        if _fonts_ready:
            return
        for name, file in (("GN-Serif", "LiberationSerif-Regular.ttf"), ("GN-Serif-Bold", "LiberationSerif-Bold.ttf"),
                           ("GN-Serif-Italic", "LiberationSerif-Italic.ttf"), ("GN-Sans", "LiberationSans-Regular.ttf"),
                           ("GN-Sans-Bold", "LiberationSans-Bold.ttf")):
            pdfmetrics.registerFont(TTFont(name, str(FONT_DIR / file)))
        pdfmetrics.registerFontFamily("GN-Serif", normal="GN-Serif", bold="GN-Serif-Bold", italic="GN-Serif-Italic",
                                      boldItalic="GN-Serif-Bold")
        pdfmetrics.registerFontFamily("GN-Sans", normal="GN-Sans", bold="GN-Sans-Bold", italic="GN-Sans",
                                      boldItalic="GN-Sans-Bold")
        _fonts_ready = True


# ---------------------------------------------------------------- veri yapıları
@dataclass
class Party:
    name: str = ""
    tc: str = ""
    address: str = ""
    phone: str = ""
    iban: str = ""


@dataclass
class DocMeta:
    doc_no: str
    verify_code: str
    created: datetime
    brand: str = "Gold Nicomedia"
    office_line: str = "İzmit / Kocaeli"
    license_no: str = ""
    agent_name: str = ""


@dataclass
class LeaseData:
    landlord: Party
    tenant: Party
    address: str
    rent: float
    start: date
    term_months: int = 12
    contract_date: date | None = None
    kind: str = "konut"
    unit_desc: str = ""
    usage_purpose: str = "Mesken"
    payment_day: int = 1
    deposit: float | None = None
    condition: str = "Boyalı, temiz ve kullanıma hazır"
    fixtures: list[str] = field(default_factory=list)
    special_terms: list[str] = field(default_factory=list)
    guarantor: Party | None = None
    guarantor_limit: float | None = None


@dataclass
class EvictionData:
    landlord: Party
    tenant: Party
    address: str
    contract_date: date
    delivery_date: date
    eviction_date: date
    signed_date: date


@dataclass
class HandoverItem:
    name: str
    qty: str = "1"
    state: str = "Sağlam"


@dataclass
class HandoverData:
    landlord: Party
    tenant: Party
    address: str
    handover_date: date
    direction: str = "teslim"          # teslim: kiracıya teslim · iade: kiracıdan geri alım (tahliye)
    unit_desc: str = ""
    keys: str = ""
    meter_electric: str = ""
    meter_water: str = ""
    meter_gas: str = ""
    condition: str = ""
    items: list[HandoverItem] = field(default_factory=list)
    notes: str = ""


# ---------------------------------------------------------------- yardımcılar
def x(text) -> str:
    """Paragraph işaretleme dili için kaçış."""
    return _xesc(str(text if text not in (None, "") else "-"))


def d(value: date | None) -> str:
    return value.strftime("%d.%m.%Y") if value else "…../…../……."


def term_text(months: int) -> str:
    if months % 12 == 0:
        years = months // 12
        return f"{years} ({number_words(years)}) yıl"
    return f"{months} ({number_words(months)}) ay"


def styles() -> dict[str, ParagraphStyle]:
    register_fonts()  # paragraf ayrıştırıcısı yazı tipi ailesini stil oluşturulurken arar
    base = ParagraphStyle("base", fontName="GN-Serif", fontSize=10.2, leading=14.1, textColor=INK, alignment=TA_JUSTIFY)
    return {
        "title": ParagraphStyle("title", parent=base, fontName="GN-Serif-Bold", fontSize=17.5, leading=22,
                                alignment=TA_CENTER, textColor=NAVY, spaceAfter=2),
        "subtitle": ParagraphStyle("subtitle", parent=base, fontName="GN-Serif-Italic", fontSize=9.4, leading=12,
                                   alignment=TA_CENTER, textColor=MUTED, spaceAfter=12),
        "h": ParagraphStyle("h", parent=base, fontName="GN-Sans-Bold", fontSize=10.2, leading=13, textColor=NAVY,
                            spaceBefore=11, spaceAfter=5, alignment=TA_LEFT),
        "body": base,
        "clause": ParagraphStyle("clause", parent=base, spaceAfter=4.2),
        "small": ParagraphStyle("small", parent=base, fontSize=8.4, leading=11.2, textColor=MUTED),
        "cell": ParagraphStyle("cell", parent=base, fontSize=9.6, leading=12.6, alignment=TA_LEFT),
        "label": ParagraphStyle("label", parent=base, fontName="GN-Sans-Bold", fontSize=8.2, leading=10.8,
                                textColor=NAVY, alignment=TA_LEFT),
        "sig": ParagraphStyle("sig", parent=base, fontName="GN-Sans-Bold", fontSize=8.6, leading=11,
                              alignment=TA_CENTER, textColor=NAVY),
        "signame": ParagraphStyle("signame", parent=base, fontSize=9.2, leading=12, alignment=TA_CENTER),
    }


def party_lines(p: Party | None, with_iban: bool = False) -> str:
    if p is None:
        return "Yoktur"
    lines = [f"<b>{x(p.name)}</b>", f"T.C. Kimlik No: {x(p.tc) if p.tc else '……………………………'}"]
    lines.append(f"Adres: {x(p.address) if p.address else '……………………………………………………'}")
    if p.phone:
        lines.append(f"Telefon: {x(p.phone)}")
    if with_iban and p.iban:
        lines.append(f"IBAN: {x(iban_format(p.iban))}")
    return "<br/>".join(lines)


def info_table(rows: list[tuple[str, str]], st, label_w: float = 52 * mm) -> Table:
    data = [[Paragraph(x(k).upper() if False else _xesc(k), st["label"]), Paragraph(v, st["cell"])] for k, v in rows]
    t = Table(data, colWidths=[label_w, PAGE_W - 2 * MARGIN_X - label_w], hAlign="LEFT")
    t.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.45, LINE),
        ("BACKGROUND", (0, 0), (0, -1), GOLD_SOFT),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 4.2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4.2),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
    ]))
    return t


def signature_table(roles: list[tuple[str, str]], st, height: float = 26 * mm) -> Table:
    width = (PAGE_W - 2 * MARGIN_X) / len(roles)
    head = [Paragraph(_xesc(r), st["sig"]) for r, _ in roles]
    names = [Paragraph(x(n) if n else "Ad Soyad: ………………………", st["signame"]) for _, n in roles]
    sign = [Paragraph("İmza", st["small"]) for _ in roles]
    t = Table([head, names, sign], colWidths=[width] * len(roles), rowHeights=[None, None, height])
    t.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), 0.6, LINE),
        ("INNERGRID", (0, 0), (-1, -1), 0.45, LINE),
        ("BACKGROUND", (0, 0), (-1, 0), GOLD_SOFT),
        ("VALIGN", (0, 2), (-1, 2), "BOTTOM"),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    return t


# ---------------------------------------------------------------- sayfa süsleri
def draw_monogram(c, x0: float, y0: float, size: float, alpha: float = 1.0, background: bool = True) -> None:
    """Uygulama ikonundaki GN + çatı monogramı (vektörel)."""
    s = size / 512

    def p(px: float, py: float) -> tuple[float, float]:
        return x0 + px * s, y0 + (512 - py) * s

    c.saveState()
    if background:
        c.setFillColor(NAVY)
        c.roundRect(x0, y0, size, size, 112 * s, stroke=0, fill=1)
    c.setStrokeColor(GOLD)
    if alpha < 1:  # setStrokeColor saydamlığı sıfırladığı için renkten SONRA ayarlanmalı
        c.setStrokeAlpha(alpha)
    c.setLineWidth(22 * s)
    c.setLineCap(1)
    c.setLineJoin(1)
    path = c.beginPath()
    path.moveTo(*p(128, 236))
    path.lineTo(*p(256, 140))
    path.lineTo(*p(384, 236))
    c.drawPath(path, stroke=1, fill=0)
    cx, cy = p(200, 332)
    r = 60 * s
    path = c.beginPath()
    path.arc(cx - r, cy - r, cx + r, cy + r, startAng=45, extent=315)
    path.lineTo(*p(214, 332))
    c.drawPath(path, stroke=1, fill=0)
    path = c.beginPath()
    path.moveTo(*p(300, 392))
    path.lineTo(*p(300, 272))
    path.lineTo(*p(372, 392))
    path.lineTo(*p(372, 272))
    c.drawPath(path, stroke=1, fill=0)
    c.restoreState()


def _decorate_page(c, meta: DocMeta, title: str) -> None:
    """Filigran + üst bilgi (içerikten önce çizilir)."""
    c.saveState()
    # filigran
    draw_monogram(c, PAGE_W / 2 - 75 * mm, PAGE_H / 2 - 75 * mm, 150 * mm, alpha=0.045, background=False)
    c.setFillColor(GOLD)
    c.setFillAlpha(0.06)
    c.translate(PAGE_W / 2, PAGE_H / 2)
    c.rotate(35)
    c.setFont("GN-Sans-Bold", 46)
    c.drawCentredString(0, -8, meta.brand.upper())
    c.restoreState()
    # üst bilgi
    c.saveState()
    top = PAGE_H - 11 * mm
    draw_monogram(c, MARGIN_X, top - 11.5 * mm, 11.5 * mm)
    c.setFillColor(NAVY)
    c.setFont("GN-Sans-Bold", 11.5)
    c.drawString(MARGIN_X + 14.5 * mm, top - 5 * mm, meta.brand.upper(), charSpace=1.4)
    c.setFillColor(MUTED)
    c.setFont("GN-Sans", 7.4)
    c.drawString(MARGIN_X + 14.5 * mm, top - 9.3 * mm, "GAYRİMENKUL DANIŞMANLIK · " + meta.office_line.upper()[:80])
    c.setFillColor(GOLD)
    c.setFont("GN-Sans-Bold", 8)
    c.drawRightString(PAGE_W - MARGIN_X, top - 5 * mm, title.upper())
    c.setFillColor(MUTED)
    c.setFont("GN-Sans", 7.4)
    c.drawRightString(PAGE_W - MARGIN_X, top - 9.3 * mm, f"Belge No: {meta.doc_no}")
    c.setStrokeColor(GOLD)
    c.setLineWidth(0.9)
    c.line(MARGIN_X, top - 13.5 * mm, PAGE_W - MARGIN_X, top - 13.5 * mm)
    c.restoreState()


def _canvas_class(meta: DocMeta):
    """Toplam sayfa sayısını bilmek için sayfaları biriktiren tuval (Sayfa X / Y)."""

    class NumberedCanvas(rl_canvas.Canvas):
        def __init__(self, *args, **kwargs):
            kwargs.setdefault("initialFontName", "GN-Sans")  # gömülmemiş Helvetica referansı oluşmasın
            super().__init__(*args, **kwargs)
            self._pages: list[dict] = []

        def showPage(self):
            self._pages.append(dict(self.__dict__))
            self._startPage()

        def save(self):
            total = len(self._pages)
            for state in self._pages:
                self.__dict__.update(state)
                self._footer(total)
                super().showPage()
            super().save()

        def _footer(self, total: int) -> None:
            self.saveState()
            y = 11.5 * mm
            self.setStrokeColor(LINE)
            self.setLineWidth(0.5)
            self.line(MARGIN_X, y + 4 * mm, PAGE_W - MARGIN_X, y + 4 * mm)
            self.setFont("GN-Sans", 7)
            self.setFillColor(MUTED)
            self.drawString(MARGIN_X, y, f"Belge No: {meta.doc_no}  ·  Doğrulama: {meta.verify_code}")
            self.drawCentredString(PAGE_W / 2, y, f"Düzenleme: {meta.created:%d.%m.%Y %H:%M}")
            self.drawRightString(PAGE_W - MARGIN_X, y, f"Sayfa {self._pageNumber} / {total}")
            self.restoreState()

    return NumberedCanvas


def _render(story: list, meta: DocMeta, title: str) -> bytes:
    register_fonts()
    buf = io.BytesIO()
    doc = BaseDocTemplate(
        buf, pagesize=A4, leftMargin=MARGIN_X, rightMargin=MARGIN_X, topMargin=30 * mm, bottomMargin=20 * mm,
        title=f"{title} — {meta.doc_no}", author=meta.brand, subject=title, creator=f"{meta.brand} Real Estate OS",
    )
    frame = Frame(doc.leftMargin, doc.bottomMargin, doc.width, doc.height, id="body", leftPadding=0, rightPadding=0,
                  topPadding=0, bottomPadding=0)
    doc.addPageTemplates([PageTemplate(id="page", frames=[frame],
                                       onPage=lambda c, _doc: _decorate_page(c, meta, title))])
    doc.build(story, canvasmaker=_canvas_class(meta))
    return buf.getvalue()


# ---------------------------------------------------------------- KİRA SÖZLEŞMESİ
def lease_terms(data: LeaseData) -> list[tuple[str, str]]:
    terms = [
        ("Konu", "İşbu sözleşme ile kiraya veren, yukarıda nitelikleri belirtilen kiralananın kullanımını kira süresince "
                 "kiracıya bırakmayı; kiracı da buna karşılık kararlaştırılan kira bedelini ödemeyi üstlenmiştir "
                 "(TBK m. 299)."),
        ("Kira bedelinin ödenmesi", "Kira bedeli, yukarıda belirtilen gün ve şekilde peşin olarak, kiraya verenin hesabına "
                                    "banka aracılığıyla ödenir; ödemeyi gösteren banka dekontu makbuz yerine geçer."),
        ("Kira artışı", "Yenilenen kira dönemlerinde uygulanacak kira bedeli, bir önceki kira yılında tüketici fiyat "
                        "endeksindeki on iki aylık ortalamalara göre değişim oranını geçmemek koşuluyla belirlenir "
                        "(TBK m. 344/1). Beş yıldan uzun süreli veya beş yıldan sonra yenilenen sözleşmelerde ve her beş "
                        "yılda bir, yeni kira yılında uygulanacak kira bedeli TBK m. 344/3 uyarınca belirlenebilir."),
        ("Teslim", "Kiraya veren, kiralananı kararlaştırılan tarihte, sözleşmeye uygun kullanıma elverişli bir durumda "
                   "teslim etmek ve sözleşme süresince bu durumda bulundurmakla yükümlüdür (TBK m. 301). Teslim, taraflarca "
                   "imzalanan teslim tutanağı ile belgelenir."),
        ("Özenli kullanım", "Kiracı, kiralananı sözleşmeye uygun olarak özenle kullanmak ve kiralananın bulunduğu taşınmazda "
                            "oturan kişilere ve komşulara gerekli saygıyı göstermekle yükümlüdür (TBK m. 316)."),
        ("Giderler", "Elektrik, su, doğalgaz ve benzeri kullanıma bağlı tüketim giderleri ile kiralananın olağan kullanımı "
                     "için gerekli temizlik ve bakım giderleri kiracıya; kiralananla ilgili zorunlu sigorta, vergi ve benzeri "
                     "yükümlülükler kiraya verene aittir. Ortak giderlerden kullanıma ilişkin olanlar kiracı tarafından "
                     "ödenir."),
        ("Değişiklik ve alt kira", "Kiracı, kiraya verenin yazılı rızası olmadıkça kiralananda yenilik ve değişiklik "
                                   "yapamaz (TBK m. 321); kiralananı başkasına kiraya veremez ve kullanım hakkını başkasına "
                                   "devredemez (TBK m. 322)."),
        ("Ayıp bildirimi ve gezdirme", "Kiracı, kendisinin gidermekle yükümlü olmadığı ayıpları kiraya verene derhal "
                                       "bildirir; kiralananın bakımı, satımı veya sözleşme sonrası yeniden kiralanması için "
                                       "gerekli olduğu ölçüde, makul bir süre önceden haber verilmek kaydıyla kiralananın "
                                       "gezilmesine izin verir."),
        ("Ödeme gecikmesi", "Kiracının muaccel kira bedelini ödememesi hâlinde kiraya veren, en az otuz günlük süre vererek "
                            "yazılı olarak ihtarda bulunabilir; bu sürede ödeme yapılmazsa sözleşmeyi feshedebilir "
                            "(TBK m. 315). Kiracı aleyhine ceza koşulu veya sonraki kira bedellerinin muaccel olacağına "
                            "ilişkin anlaşmalar geçersizdir (TBK m. 346)."),
        ("Süre ve fesih", "Kiracı, belirli süreli sözleşmenin bitiminden en az on beş gün önce bildirimde bulunmadıkça "
                          "sözleşme aynı koşullarla bir yıl uzamış sayılır. Kiraya veren, süre bitimine dayanarak sözleşmeyi "
                          "sona erdiremez; ancak on yıllık uzama süresinin sonunda, bu süreyi izleyen her uzama yılının "
                          "bitiminden en az üç ay önce bildirimde bulunarak sona erdirebilir (TBK m. 347)."),
        ("Kiralananın geri verilmesi", "Kiracı, sözleşmenin sona ermesiyle kiralananı teslim aldığı durumda geri verir; "
                                       "sözleşmeye uygun kullanımdan doğan eskime ve bozulmalardan sorumlu değildir "
                                       "(TBK m. 334)."),
    ]
    if data.deposit:
        terms.append(("Güvence bedeli", "Güvence bedeli üç aylık kira bedelini aşamaz. Para olarak verilen güvence, kiraya "
                                        "veren tarafından kiracının onayı olmaksızın çekilmemek üzere vadeli bir tasarruf "
                                        "hesabına yatırılır; banka güvenceyi ancak tarafların rızasıyla veya kesinleşmiş icra "
                                        "takibi ya da mahkeme kararına dayanarak geri verir (TBK m. 342)."))
    terms.append(("Tebligat", "Tarafların bu sözleşmede yazılı adresleri tebligat adresleridir. Adres değişikliği karşı "
                              "tarafa yazılı olarak bildirilmedikçe bu adreslere yapılan tebligatlar geçerli sayılır."))
    if data.kind == "konut":
        terms.append(("Adres beyanı", "Kiracı, yerleşim yeri değişikliğini 5490 sayılı Nüfus Hizmetleri Kanunu uyarınca "
                                      "yirmi iş günü içinde e-Devlet üzerinden veya nüfus müdürlüğüne bildirir."))
    terms.append(("Uyuşmazlıklar", "Kira ilişkisinden doğan uyuşmazlıklarda dava açılmadan önce arabulucuya başvurulması "
                                   "zorunludur. Yetkili mahkeme ve icra daireleri, 6100 sayılı Hukuk Muhakemeleri Kanunu ve "
                                   "2004 sayılı İcra ve İflas Kanunu hükümlerine göre belirlenir."))
    return terms


def build_lease_pdf(data: LeaseData, meta: DocMeta) -> bytes:
    st = styles()
    end = add_months(data.start, data.term_months)
    rent = Decimal(str(data.rent))
    deposit_txt = "Alınmamıştır"
    if data.deposit:
        deposit_txt = f"{money_exact(data.deposit)} ({amount_words(data.deposit)})"
    iban = iban_format(data.landlord.iban) if data.landlord.iban else "…………………………………………"
    rows = [
        ("Sözleşme tarihi", d(data.contract_date or data.start)),
        ("Kiralananın adresi", x(data.address)),
        ("Kiralananın cinsi / niteliği", x(f"{PROPERTY_KIND_LABELS.get(data.kind, 'Konut')}"
                                           f"{' — ' + data.unit_desc if data.unit_desc else ''}")),
        ("Kullanım amacı", x(data.usage_purpose)),
        ("Kiraya veren", party_lines(data.landlord, with_iban=True)),
        ("Kiracı", party_lines(data.tenant)),
        ("Kefil", party_lines(data.guarantor) if data.guarantor else "Yoktur"),
        ("Aylık kira bedeli", f"<b>{money_exact(rent)}</b> ({x(amount_words(rent))})"),
        ("Yıllık kira bedeli", f"{money_exact(rent * 12)} ({x(amount_words(rent * 12))})"),
        ("Ödeme zamanı ve şekli", f"Her ayın {data.payment_day}. günü peşin olarak, kiraya verenin "
                                  f"<b>{x(iban)}</b> IBAN numaralı hesabına banka aracılığıyla"),
        ("Kira süresi", f"{term_text(data.term_months)} ({d(data.start)} – {d(end)})"),
        ("Kira başlangıç tarihi", d(data.start)),
        ("Güvence bedeli (depozito)", deposit_txt),
        ("Kiralananın şimdiki durumu", x(data.condition)),
        ("Demirbaşlar", x(", ".join(data.fixtures)) if data.fixtures else "Teslim tutanağında belirtilmiştir."),
    ]
    story: list = [
        Paragraph("KİRA SÖZLEŞMESİ", st["title"]),
        Paragraph("6098 sayılı Türk Borçlar Kanunu'nun konut ve çatılı işyeri kiralarına ilişkin hükümleri uyarınca "
                  "düzenlenmiştir", st["subtitle"]),
        info_table(rows, st),
        Paragraph("GENEL ŞARTLAR", st["h"]),
    ]
    terms = lease_terms(data)
    for i, (title, text) in enumerate(terms, 1):
        story.append(Paragraph(f"<b>{i}. {_xesc(title)}.</b> {_xesc(text)}", st["clause"]))
    story.append(Paragraph("ÖZEL ŞARTLAR", st["h"]))
    specials = [s for s in (t.strip() for t in data.special_terms) if s]
    if specials:
        for i, t in enumerate(specials, 1):
            story.append(Paragraph(f"<b>{i}.</b> {x(t)}", st["clause"]))
    else:
        story.append(Paragraph("Yoktur.", st["clause"]))

    if data.guarantor:
        limit = (f"Sözleşmede öngörülen azami kefalet tutarı: <b>{money_exact(data.guarantor_limit)}</b> "
                 f"({x(amount_words(data.guarantor_limit))})." if data.guarantor_limit else "")
        hand = Table([
            [Paragraph("Kefilin sorumlu olduğu azami tutar (kefilin el yazısıyla, rakam ve yazı ile):", st["label"]), ""],
            [Paragraph("Kefalet tarihi (kefilin el yazısıyla):", st["label"]), ""],
            [Paragraph("“Müteselsil kefil olarak sorumluyum.” (kefilin el yazısıyla):", st["label"]), ""],
            [Paragraph("Kefil evli ise eşinin rızası — eşin adı soyadı ve imzası (TBK m. 584):", st["label"]), ""],
        ], colWidths=[78 * mm, PAGE_W - 2 * MARGIN_X - 78 * mm], rowHeights=[13 * mm] * 4)
        hand.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.45, LINE), ("BACKGROUND", (0, 0), (0, -1), GOLD_SOFT),
                                  ("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("LEFTPADDING", (0, 0), (-1, -1), 6)]))
        story.append(KeepTogether([
            Paragraph("MÜTESELSİL KEFALET (TBK m. 583-584)", st["h"]),
            Paragraph("Kefil, kiracının bu sözleşmeden doğan borçlarından, aşağıda kendi el yazısıyla belirttiği azami tutar "
                      "ve kefalet tarihi ile sınırlı olarak müteselsil kefil sıfatıyla sorumludur. Kanun gereği bu alanlar "
                      f"kefil tarafından elle doldurulmalıdır; aksi hâlde kefalet geçersizdir. {limit}", st["clause"]),
            hand,
        ]))

    total_rent = rent * Decimal(data.term_months)
    stamp = (total_rent * STAMP_DUTY_RATE).quantize(Decimal("0.01"))
    info = [f"Damga vergisi (bilgi amaçlı): sözleşme süresine ait toplam kira bedeli {money_exact(total_rent)} üzerinden "
            f"binde 1,89 oranında {money_exact(stamp)}.",
            "Taraflar, kira bedelinin vergi beyanı ve bankacılık kanalıyla ödenmesine ilişkin yükümlülüklerinden kendileri "
            "sorumludur."]
    if meta.license_no:
        info.append(f"Aracı emlak ofisi: {meta.brand} — Taşınmaz Ticareti Yetki Belgesi No: {meta.license_no}.")
    story.append(Paragraph("BİLGİLENDİRME", st["h"]))
    for line in info:
        story.append(Paragraph(_xesc(line), st["small"]))
    roles = [("KİRAYA VEREN", data.landlord.name), ("KİRACI", data.tenant.name)]
    if data.guarantor:
        roles.append(("MÜTESELSİL KEFİL", data.guarantor.name))
    roles.append(("ARACI EMLAK OFİSİ", f"{meta.brand}" + (f" — {meta.agent_name}" if meta.agent_name else "")))
    story.append(KeepTogether([
        Spacer(1, 8),
        Paragraph(f"İşbu sözleşme {len(terms)} genel şart ve özel şartlardan ibaret olup taraflarca okunarak "
                  f"{d(data.contract_date or data.start)} tarihinde iki nüsha olarak imza altına alınmıştır.", st["body"]),
        Spacer(1, 8),
        signature_table(roles, st),
    ]))
    return _render(story, meta, DOC_KINDS["kira_sozlesmesi"])


# ---------------------------------------------------------------- TAHLİYE TAAHHÜTNAMESİ
def build_eviction_pdf(data: EvictionData, meta: DocMeta) -> bytes:
    if data.signed_date <= data.delivery_date:
        raise ValueError("Tahliye taahhütnamesi kiralananın tesliminden SONRAKİ bir tarihte düzenlenmelidir (TBK m. 352/1).")
    if data.eviction_date <= data.signed_date:
        raise ValueError("Tahliye tarihi, taahhütnamenin düzenlenme tarihinden sonra olmalıdır.")
    st = styles()
    rows = [
        ("Taahhüt alan (kiraya veren)", party_lines(data.landlord)),
        ("Taahhüt veren (kiracı)", party_lines(data.tenant)),
        ("Kiralananın adresi", x(data.address)),
        ("Kira sözleşmesinin tarihi", d(data.contract_date)),
        ("Kiralananın teslim tarihi", d(data.delivery_date)),
        ("Taahhüt edilen tahliye tarihi", f"<b>{d(data.eviction_date)}</b>"),
        ("Düzenleme tarihi", d(data.signed_date)),
    ]
    body = (f"Yukarıda adresi yazılı taşınmazı, {d(data.contract_date)} tarihli kira sözleşmesi ile kiracı sıfatıyla "
            f"kullanmaktayım. Kiralananı {d(data.delivery_date)} tarihinde teslim aldım. İşbu taahhütnameyi kiralananın "
            f"tesliminden sonra, hiçbir baskı altında kalmaksızın kendi özgür irademle imzaladığımı; kiralananı "
            f"<b>{d(data.eviction_date)}</b> tarihinde boş olarak ve teslim aldığım durumda kiraya verene teslim "
            f"edeceğimi kabul, beyan ve taahhüt ederim. Bu tarihte tahliye etmediğim takdirde kiraya verenin, 6098 sayılı "
            f"Türk Borçlar Kanunu'nun 352/1. maddesi ve 2004 sayılı İcra ve İflas Kanunu'nun 272. maddesi uyarınca "
            f"icra yoluyla tahliye isteminde bulunabileceğini kabul ederim.")
    story = [
        Paragraph("TAHLİYE TAAHHÜTNAMESİ", st["title"]),
        Paragraph("6098 sayılı Türk Borçlar Kanunu m. 352/1", st["subtitle"]),
        info_table(rows, st),
        Spacer(1, 12),
        Paragraph(body, st["body"]),
        Spacer(1, 18),
        signature_table([("TAAHHÜT EDEN (KİRACI)", data.tenant.name), ("TEBELLÜĞ EDEN (KİRAYA VEREN)", data.landlord.name)],
                        st, height=30 * mm),
    ]
    return _render(story, meta, DOC_KINDS["tahliye_taahhutnamesi"])


# ---------------------------------------------------------------- TESLİM / İADE TUTANAĞI
def build_handover_pdf(data: HandoverData, meta: DocMeta) -> bytes:
    st = styles()
    returning = data.direction == "iade"
    giver, taker = (data.tenant, data.landlord) if returning else (data.landlord, data.tenant)
    title = "KİRALANAN İADE (TAHLİYE) TUTANAĞI" if returning else "KİRALANAN TESLİM TUTANAĞI VE DEMİRBAŞ LİSTESİ"
    meters = " · ".join(f"{k}: {x(v)}" for k, v in (("Elektrik", data.meter_electric), ("Su", data.meter_water),
                                                    ("Doğalgaz", data.meter_gas)) if v) or "Kaydedilmedi"
    rows = [
        ("Kiralananın adresi", x(data.address) + (f"<br/>{x(data.unit_desc)}" if data.unit_desc else "")),
        ("Teslim eden", party_lines(giver)),
        ("Teslim alan", party_lines(taker)),
        ("Teslim tarihi", f"<b>{d(data.handover_date)}</b>"),
        ("Anahtar", x(data.keys or "Belirtilmedi")),
        ("Sayaç endeksleri", meters),
        ("Genel durum", x(data.condition or "Belirtilmedi")),
    ]
    items = data.items or [HandoverItem("Demirbaş bulunmamaktadır", "-", "-")]
    table = Table([["No", "Demirbaş / eşya", "Adet", "Durum"]] +
                  [[str(i), Paragraph(x(it.name), st["cell"]), x(it.qty), Paragraph(x(it.state), st["cell"])]
                   for i, it in enumerate(items, 1)],
                  colWidths=[12 * mm, 92 * mm, 18 * mm, PAGE_W - 2 * MARGIN_X - 122 * mm], repeatRows=1)
    table.setStyle(TableStyle([
        ("FONT", (0, 0), (-1, 0), "GN-Sans-Bold", 8.4), ("TEXTCOLOR", (0, 0), (-1, 0), NAVY),
        ("BACKGROUND", (0, 0), (-1, 0), GOLD_SOFT), ("GRID", (0, 0), (-1, -1), 0.45, LINE),
        ("FONT", (0, 1), (-1, -1), "GN-Serif", 9.4), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (0, 0), (0, -1), "CENTER"), ("ALIGN", (2, 0), (2, -1), "CENTER"),
    ]))
    statement = ("Yukarıda belirtilen kiralanan, listelenen demirbaşlarla birlikte belirtilen durumda teslim edilmiş ve "
                 "teslim alınmıştır. Kiracı, sözleşmenin sona ermesinde kiralananı bu tutanakta belirtilen durumda, "
                 "olağan kullanımdan doğan eskimeler hariç, geri verecektir (TBK m. 334).")
    if returning:
        statement = ("Kiralanan, yukarıda belirtilen tarih ve durumda kiracı tarafından tahliye edilerek kiraya verene geri "
                     "verilmiştir. Depozito iadesi ve varsa mahsuplar aşağıdaki notlarda gösterilmiştir.")
    story = [
        Paragraph(title, st["title"]),
        Paragraph("Taraflarca kiralananın fiili durumunun tespiti amacıyla düzenlenmiştir", st["subtitle"]),
        info_table(rows, st),
        Paragraph("DEMİRBAŞ LİSTESİ", st["h"]),
        table,
        Spacer(1, 10),
        Paragraph(_xesc(statement), st["body"]),
    ]
    if data.notes:
        story += [Paragraph("NOTLAR", st["h"]), Paragraph(x(data.notes).replace("\n", "<br/>"), st["body"])]
    story += [Spacer(1, 14), KeepTogether(signature_table(
        [("TESLİM EDEN", giver.name), ("TESLİM ALAN", taker.name), ("EMLAK OFİSİ", meta.brand)], st))]
    return _render(story, meta, DOC_KINDS["teslim_tutanagi"])
