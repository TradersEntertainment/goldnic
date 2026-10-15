"""Telegram komutları."""
from __future__ import annotations

import csv
import io
import logging
from datetime import date, datetime
from functools import wraps

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    CallbackQueryHandler, CommandHandler, ContextTypes, ConversationHandler,
    MessageHandler, filters,
)

from . import rent as R
from . import texts
from .reminders import build_owner_report, get_tufe, legal_cap_pct, run_reminders

log = logging.getLogger(__name__)
HTML = ParseMode.HTML


# ---------------------------------------------------------------- yardımcılar
def _db(ctx): return ctx.application.bot_data["db"]
def _cfg(ctx): return ctx.application.bot_data["cfg"]
def _msgr(ctx): return ctx.application.bot_data["messenger"]
def _today(ctx) -> date: return datetime.now(_cfg(ctx).tz).date()


async def reply(update: Update, text: str, **kw):
    """Telegram 4096 karakter sınırına göre böler."""
    msg = update.effective_message
    chunk = ""
    for line in text.split("\n"):
        if len(chunk) + len(line) + 1 > 3800:
            await msg.reply_text(chunk, parse_mode=HTML)
            chunk = ""
        chunk += line + "\n"
    if chunk.strip():
        await msg.reply_text(chunk, parse_mode=HTML, **kw)


def registered(owner_only: bool = False):
    """Sadece kayıtlı kullanıcılar (ve sadece sahip) kullanabilsin."""
    def deco(fn):
        @wraps(fn)
        async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
            db, cfg = _db(ctx), _cfg(ctx)
            tg = update.effective_user
            user = db.get_user_by_chat(tg.id)
            if user is None and tg.id in cfg.owner_ids:
                user = db.get_user(db.add_user(tg.id, tg.full_name, "owner"))
            if user is None or not user["active"]:
                await reply(update, "⛔ Yetkiniz yok. Ofis sahibinden davet kodu alıp "
                                    "<code>/katil KOD</code> yazın.")
                return ConversationHandler.END
            if owner_only and user["role"] != "owner":
                await reply(update, "⛔ Bu komut sadece ofis sahibine açık.")
                return ConversationHandler.END
            db.touch_user(user["id"])
            return await fn(update, ctx, user)
        return wrapper
    return deco


def _get_prop(update_ctx_db, user, pid_text):
    """(property, hata_mesajı) döndürür."""
    try:
        pid = int(str(pid_text).lstrip("#"))
    except (TypeError, ValueError):
        return None, "Mülk numarası gerekli. Örnek: /mulk 5"
    p = update_ctx_db.get_property(pid)
    if p is None or p["status"] != "active":
        return None, f"#{pid} numaralı mülk bulunamadı."
    if user["role"] != "owner" and p["agent_id"] != user["id"]:
        return None, "Bu mülk size ait değil."
    return p, None


async def notify_owners(ctx, text: str, exclude_user_id: int | None = None):
    for o in _db(ctx).list_users("owner"):
        if o["id"] != exclude_user_id:
            await _msgr(ctx).send(o["chat_id"], text)
    if _cfg(ctx).group_chat_id:
        await _msgr(ctx).send(_cfg(ctx).group_chat_id, text)


def _agent_name(db, p) -> str:
    u = db.get_user(p["agent_id"])
    return u["name"] if u else "?"


# ---------------------------------------------------------------- temel
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    db, cfg = _db(ctx), _cfg(ctx)
    tg = update.effective_user
    user = db.get_user_by_chat(tg.id)
    if user is None and tg.id in cfg.owner_ids:
        db.add_user(tg.id, tg.full_name, "owner")
        user = db.get_user_by_chat(tg.id)
    if user is None:
        await reply(update, f"Merhaba! Henüz kayıtlı değilsiniz.\nTelegram ID'niz: <code>{tg.id}</code>\n"
                            "Ofis sahibinden davet kodu alıp <code>/katil KOD</code> yazın.")
        return
    await cmd_help(update, ctx)


@registered()
async def cmd_help(update: Update, ctx, user):
    t = [
        "🏢 <b>Emlak Portföy Asistanı</b>",
        "",
        "<b>Portföy</b>",
        "/ekle — yeni mülk ekle (adım adım)",
        "/liste — portföyüm" + (" (ofisteki herkes)" if user["role"] == "owner" else ""),
        "/yaklasan [gün] — yenilemesi yaklaşanlar (varsayılan 60)",
        "/mulk 5 — mülk detayı ve kira geçmişi",
        "/duzenle 5 alan değer — alanlar: kira, tarih, kiraci, tel, evsahibi, evtel, baslik, not, gecenyil",
        "/sil 5 — mülkü arşivle",
        "",
        "<b>Kira artışı</b>",
        "/zam 5 %30 — bu sene artış oranını gir",
        "/zam 5 45000 — veya yeni kira tutarını gir",
        "/yenile 5 [yeni_kira] — sözleşme yenilendi, kaydı bir yıl ileri al",
        "/tufe — güncel TÜFE referansını göster",
        "/hukuk — hukuk radarı: tahliye taahhüdü, 5. yıl tespit, 10. yıl tahliye",
        "",
        "<b>Müşteri talepleri</b>",
        "/talep Mehmet Bey 0532… 3+1 kiralık 30-35 bin Yahya Kaptan acil — talep ekle, portföyle anında eşleştir",
        "/talepler — aktif talepler ve eşleşme sayıları",
        "",
        "<b>Web paneli</b>",
        "/sifre kullaniciadi — web girişi için kullanıcı adı + şifre al",
        "/web — web paneli adresi",
    ]
    if user["role"] == "owner":
        t += [
            "",
            "<b>Ofis sahibi</b>",
            "/ozet — emlakçı bazında durum ve sorunlar",
            "/emlakcilar — ekip listesi",
            "/davet — yeni emlakçı için davet kodu üret",
            "/cikar ID [yeni_ID] — emlakçıyı çıkar, portföyünü devret (varsayılan: sana)",
            "/devret MULK_ID KULLANICI_ID — tek mülkü devret",
            "/aktivite [KULLANICI_ID] — son işlemler",
            "/tufe guncelle — TÜİK/EVDS'den son oranı çek",
            "/tufe 31,49 veya /tufe 2026-09 31,49 — oranı elle gir",
            "/csv — tüm portföyü Excel için indir",
            "/test_hatirlat — bugünkü hatırlatma kontrolünü şimdi çalıştır",
        ]
    await reply(update, "\n".join(t))


async def cmd_setup(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/kurulum KOD → SETUP_CODE doğruysa kullanıcıyı ofis sahibi yapar (özel sohbette)."""
    import hmac

    db, cfg, tg = _db(ctx), _cfg(ctx), update.effective_user
    if update.effective_chat.type != "private":
        await reply(update, "🔒 Bu komutu botla özel sohbette yazın.")
        return
    if not cfg.setup_code:
        await reply(update, "Kurulum kodu tanımlı değil.")
        return
    if not ctx.args or not hmac.compare_digest(ctx.args[0].encode(), cfg.setup_code.encode()):
        await reply(update, "❌ Kurulum kodu hatalı.")
        return
    user = db.get_user_by_chat(tg.id)
    if user is None:
        uid = db.add_user(tg.id, tg.full_name, "owner")
    else:
        uid = user["id"]
        db.execute("UPDATE users SET role='owner', active=1 WHERE id=?", (uid,))
    db.log(uid, "kurulum_koduyla_sahip_oldu", detail=tg.full_name)
    await reply(update, "✅ Ofis sahibi olarak kaydoldunuz. /yardim\n\n"
                        "⚠️ Güvenlik için Railway'den <code>SETUP_CODE</code> değişkenini silin; "
                        "kod bilen herkes sahip olabilir.")


async def cmd_join(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    db, tg = _db(ctx), update.effective_user
    if db.get_user_by_chat(tg.id):
        await reply(update, "Zaten kayıtlısınız. /yardim")
        return
    if not ctx.args or not db.pop_invite(ctx.args[0]):
        await reply(update, "❌ Davet kodu geçersiz veya kullanılmış.")
        return
    uid = db.add_user(tg.id, tg.full_name, "agent")
    db.set_invite_user(ctx.args[0], uid)
    db.log(uid, "katildi", detail=tg.full_name)
    await reply(update, f"✅ Hoş geldiniz {texts.esc(tg.full_name)}! /yardim ile başlayın.")
    await notify_owners(ctx, f"👋 Yeni emlakçı katıldı: <b>{texts.esc(tg.full_name)}</b> (id {uid})")


# ---------------------------------------------------------------- mülk ekleme
TITLE, LANDLORD, TENANT, TPHONE, RENT, DATE, LASTINC = range(7)


@registered()
async def add_start(update: Update, ctx, user):
    ctx.user_data["new"] = {"agent_id": user["id"]}
    await reply(update, "🏠 Mülkün adı/adresi? (örn: <i>Yıldız Mah. 12/4</i>)\nİptal: /iptal")
    return TITLE


async def add_title(update: Update, ctx):
    ctx.user_data["new"]["title"] = update.message.text.strip()
    await reply(update, "🏷 Ev sahibinin adı? (telefonla birlikte yazabilirsiniz: <i>Ahmet Bey 0532...</i>)")
    return LANDLORD


async def add_landlord(update: Update, ctx):
    ctx.user_data["new"]["landlord_name"] = update.message.text.strip()
    await reply(update, "👤 Kiracının adı?")
    return TENANT


async def add_tenant(update: Update, ctx):
    ctx.user_data["new"]["tenant_name"] = update.message.text.strip()
    await reply(update, "📞 Kiracı telefonu? (yoksa <code>-</code>)")
    return TPHONE


async def add_tphone(update: Update, ctx):
    t = update.message.text.strip()
    ctx.user_data["new"]["tenant_phone"] = None if t == "-" else t
    await reply(update, "💰 Güncel aylık kira? (örn: 30000)")
    return RENT


async def add_rent(update: Update, ctx):
    try:
        ctx.user_data["new"]["rent"] = R.parse_amount(update.message.text)
    except ValueError as e:
        await reply(update, f"❌ {e}\nTekrar yazın:")
        return RENT
    await reply(update, "📅 Bir sonraki kira yenileme tarihi? (GG.AA.YYYY)")
    return DATE


async def add_date(update: Update, ctx):
    try:
        d = R.parse_date(update.message.text)
    except ValueError as e:
        await reply(update, f"❌ {e}\nTekrar yazın:")
        return DATE
    ctx.user_data["new"]["renewal_date"] = d.isoformat()
    await reply(update, "📉 Geçen sene yüzde kaç artış yapıldı? (bilmiyorsanız <code>-</code>)")
    return LASTINC


async def add_lastinc(update: Update, ctx):
    db = _db(ctx)
    new = ctx.user_data.pop("new")
    t = update.message.text.strip()
    if t != "-":
        try:
            new["last_increase_pct"] = R.parse_pct(t)
            new["last_increase_amount"] = round(
                new["rent"] - new["rent"] / (1 + new["last_increase_pct"] / 100), 2)
        except ValueError as e:
            ctx.user_data["new"] = new
            await reply(update, f"❌ {e}\nTekrar yazın:")
            return LASTINC
    pid = db.add_property(**new)
    db.log(new["agent_id"], "mulk_ekledi", pid, new["title"])
    p = db.get_property(pid)
    me = db.get_user(new["agent_id"])
    await reply(update, "✅ Kaydedildi!\n\n" + texts.property_card(p, me["name"], _today(ctx), legal_cap_pct(db, p)))
    await notify_owners(ctx, f"🆕 {texts.esc(me['name'])} yeni mülk ekledi: <b>{texts.esc(p['title'])}</b> (#{pid})",
                        exclude_user_id=me["id"])
    return ConversationHandler.END


async def add_cancel(update: Update, ctx):
    ctx.user_data.pop("new", None)
    await reply(update, "İptal edildi.")
    return ConversationHandler.END


def add_conversation() -> ConversationHandler:
    text = filters.TEXT & ~filters.COMMAND
    return ConversationHandler(
        entry_points=[CommandHandler("ekle", add_start)],
        states={
            TITLE: [MessageHandler(text, add_title)],
            LANDLORD: [MessageHandler(text, add_landlord)],
            TENANT: [MessageHandler(text, add_tenant)],
            TPHONE: [MessageHandler(text, add_tphone)],
            RENT: [MessageHandler(text, add_rent)],
            DATE: [MessageHandler(text, add_date)],
            LASTINC: [MessageHandler(text, add_lastinc)],
        },
        fallbacks=[CommandHandler("iptal", add_cancel)],
        conversation_timeout=900,
    )


# ---------------------------------------------------------------- görüntüleme
@registered()
async def cmd_list(update: Update, ctx, user):
    db, today = _db(ctx), _today(ctx)
    if user["role"] == "owner":
        agents = db.list_users()
        lines = []
        for a in agents:
            props = db.list_properties(a["id"])
            lines.append(f"\n👔 <b>{texts.esc(a['name'])}</b> (id {a['id']}) — {len(props)} mülk")
            lines += [texts.short_line(p, today) for p in props]
        body = "\n".join(lines)
    else:
        props = db.list_properties(user["id"])
        body = "\n".join(texts.short_line(p, today) for p in props)
    await reply(update, body.strip() or "Portföyünüz boş. /ekle ile başlayın.")


@registered()
async def cmd_upcoming(update: Update, ctx, user):
    db, today = _db(ctx), _today(ctx)
    try:
        horizon = int(ctx.args[0]) if ctx.args else 60
    except ValueError:
        horizon = 60
    props = db.list_properties(None if user["role"] == "owner" else user["id"])
    props = [p for p in props if texts.days_left(p, today) <= horizon]
    if not props:
        await reply(update, f"{horizon} gün içinde yenilenecek mülk yok. 🎉")
        return
    lines = [f"📅 <b>Önümüzdeki {horizon} gün</b>"]
    for p in props:
        extra = f" — 👔 {texts.esc(_agent_name(db, p))}" if user["role"] == "owner" else ""
        lines.append(texts.short_line(p, today) + extra)
    await reply(update, "\n".join(lines))


@registered()
async def cmd_property(update: Update, ctx, user):
    db = _db(ctx)
    p, err = _get_prop(db, user, ctx.args[0] if ctx.args else None)
    if err:
        await reply(update, err)
        return
    hist = db.history(p["id"])
    text = texts.property_card(p, _agent_name(db, p), _today(ctx), legal_cap_pct(db, p))
    if hist:
        text += "\n\n🗂 <b>Kira geçmişi</b>\n" + "\n".join(
            f"{R.fmt_date(h['effective_date'])}: {R.money(h['old_rent'])} → "
            f"{R.money(h['new_rent'])} ({R.pct_str(h['pct'])})" for h in hist)
    await reply(update, text)


# ---------------------------------------------------------------- kira artışı
@registered()
async def cmd_zam(update: Update, ctx, user):
    db = _db(ctx)
    if len(ctx.args) < 2:
        await reply(update, "Kullanım: <code>/zam 5 %30</code> veya <code>/zam 5 45000</code>")
        return
    p, err = _get_prop(db, user, ctx.args[0])
    if err:
        await reply(update, err)
        return
    val = " ".join(ctx.args[1:])
    try:
        if "%" in val:
            pct = R.parse_pct(val)
            new = R.apply_pct(p["rent"], pct)
        else:
            new = R.parse_amount(val)
            pct = R.pct_change(p["rent"], new)
    except ValueError as e:
        await reply(update, f"❌ {e}")
        return
    db.update_property(p["id"], planned_pct=pct, planned_rent=new)
    db.log(user["id"], "zam_girdi", p["id"], f"{R.pct_str(pct)} -> {R.money(new)}")
    tufe = legal_cap_pct(db, p)
    warn = "\n🚨 Yasal TÜFE sınırını aşıyor!" if tufe is not None and pct > tufe + 0.005 else ""
    await reply(update, f"✅ #{p['id']} {texts.esc(p['title'])}\nYeni kira planı: <b>{R.pct_str(pct)}</b> → "
                        f"<b>{R.money(new)}</b>{warn}")
    await notify_owners(ctx, f"📈 {texts.esc(user['name'])}, #{p['id']} {texts.esc(p['title'])} için zam girdi: "
                             f"{R.pct_str(pct)} → {R.money(new)}{warn}", exclude_user_id=user["id"])


@registered()
async def cmd_renew(update: Update, ctx, user):
    db = _db(ctx)
    p, err = _get_prop(db, user, ctx.args[0] if ctx.args else None)
    if err:
        await reply(update, err)
        return
    try:
        new = R.parse_amount(ctx.args[1]) if len(ctx.args) > 1 else p["planned_rent"]
    except ValueError as e:
        await reply(update, f"❌ {e}")
        return
    if new is None:
        await reply(update, "Önce /zam ile artışı girin veya yeni kirayı yazın: <code>/yenile 5 45000</code>")
        return
    pct = R.pct_change(p["rent"], new)
    db.add_history(p["id"], p["renewal_date"], p["rent"], new, pct, user["id"])
    next_date = R.add_year(date.fromisoformat(p["renewal_date"]))
    db.update_property(
        p["id"], rent=new, renewal_date=next_date.isoformat(), last_increase_pct=pct,
        last_increase_amount=round(new - p["rent"], 2), planned_pct=None, planned_rent=None)
    db.log(user["id"], "yeniledi", p["id"], f"{R.money(p['rent'])} -> {R.money(new)} ({R.pct_str(pct)})")
    await reply(update, f"✅ Yenilendi. Yeni kira <b>{R.money(new)}</b> ({R.pct_str(pct)}). "
                        f"Sonraki yenileme: <b>{R.fmt_date(next_date)}</b>")
    await notify_owners(ctx, f"🔁 {texts.esc(user['name'])} yeniledi: #{p['id']} {texts.esc(p['title'])} — "
                             f"{R.money(p['rent'])} → {R.money(new)} ({R.pct_str(pct)})", exclude_user_id=user["id"])


FIELD_MAP = {
    "kira": ("rent", R.parse_amount), "tarih": ("renewal_date", lambda s: R.parse_date(s).isoformat()),
    "kiraci": ("tenant_name", str), "tel": ("tenant_phone", str), "evsahibi": ("landlord_name", str),
    "evtel": ("landlord_phone", str), "baslik": ("title", str), "not": ("notes", str),
    "gecenyil": ("last_increase_pct", R.parse_pct),
}


@registered()
async def cmd_edit(update: Update, ctx, user):
    db = _db(ctx)
    if len(ctx.args) < 3 or ctx.args[1].lower() not in FIELD_MAP:
        await reply(update, "Kullanım: <code>/duzenle 5 kira 32000</code>\nAlanlar: " + ", ".join(FIELD_MAP))
        return
    p, err = _get_prop(db, user, ctx.args[0])
    if err:
        await reply(update, err)
        return
    col, conv = FIELD_MAP[ctx.args[1].lower()]
    try:
        value = conv(" ".join(ctx.args[2:]))
    except ValueError as e:
        await reply(update, f"❌ {e}")
        return
    extra = {}
    if col == "rent":  # kira değişince planlanan artış geçersizleşir
        extra = {"planned_pct": None, "planned_rent": None}
    db.update_property(p["id"], **{col: value}, **extra)
    db.log(user["id"], "duzenledi", p["id"], f"{col}={value}")
    await reply(update, f"✅ #{p['id']} güncellendi ({ctx.args[1]}).")


@registered()
async def cmd_delete(update: Update, ctx, user):
    db = _db(ctx)
    p, err = _get_prop(db, user, ctx.args[0] if ctx.args else None)
    if err:
        await reply(update, err)
        return
    db.update_property(p["id"], status="archived")
    db.log(user["id"], "arsivledi", p["id"], p["title"])
    await reply(update, f"🗄 #{p['id']} arşivlendi.")
    await notify_owners(ctx, f"🗄 {texts.esc(user['name'])}, #{p['id']} {texts.esc(p['title'])} mülkünü arşivledi.",
                        exclude_user_id=user["id"])


@registered()
async def cmd_tufe(update: Update, ctx, user):
    from . import law as L
    from . import tufe

    db, today, cfg = _db(ctx), _today(ctx), _cfg(ctx)
    args = list(ctx.args or [])
    if args:
        if user["role"] != "owner":
            await reply(update, "⛔ TÜFE oranını sadece ofis sahibi güncelleyebilir.")
            return
        if args[0].lower() in ("guncelle", "güncelle", "cek", "çek"):
            res = tufe.refresh(db, today, evds_key=getattr(cfg, "evds_api_key", ""),
                               evds_series=getattr(cfg, "evds_series", "TP.FG.J0"))
            await reply(update, ("✅ " if res.ok else "⚠️ ") + texts.esc(res.message))
            return
        period = tufe.expected_latest_period(today)
        if len(args) >= 2 and len(args[0]) == 7 and args[0][4] == "-":
            period, args = args[0], args[1:]
        try:
            v = R.parse_pct(args[0])
            tufe.upsert_rate(db, period, v, "manual")
        except ValueError as e:
            await reply(update, f"❌ {e}")
            return
        db.set_setting("tufe", str(v))
        db.log(user["id"], "tufe_guncelledi", detail=f"{period}: {v}")
        await reply(update, f"✅ {L.period_label(period)} TÜFE 12 aylık ortalama: {R.pct2(v)} (elle giriş)")
        return
    rates = tufe.load_rates(db)
    latest = L.latest_rate(rates, today)
    if latest is None:
        v = get_tufe(db)
        await reply(update, f"TÜFE 12 aylık ortalama (yasal artış sınırı): {R.pct_str(v)}" if v is not None
                    else "TÜFE oranı henüz girilmedi. Ofis sahibi <code>/tufe guncelle</code> ile çekebilir.")
        return
    cap = L.rent_cap(today, rates)
    lines = [
        f"📈 <b>TÜFE 12 aylık ortalama: {R.pct2(latest.avg12)}</b>",
        f"Veri ayı: {L.period_label(latest.period)} · Kaynak: {tufe.SOURCE_LABELS.get(latest.source, latest.source)}",
        f"Bugün yenilenen sözleşmeler için yasal tavan: <b>{R.pct2(cap.cap_pct)}</b> ({cap.label})",
    ]
    if cap.provisional:
        lines.append(f"⏳ {L.period_label(cap.period)} verisi {R.fmt_date(cap.expected_on)} tarihinde açıklanacak.")
    await reply(update, "\n".join(lines))


@registered()
async def cmd_hukuk(update: Update, ctx, user):
    from . import legal

    scope = None if user["role"] == "owner" or user["can_see_all"] else user["id"]
    await reply(update, legal.summary_text(_db(ctx), _today(ctx), scope))


# ---------------------------------------------------------------- müşteri talepleri (Matching CRM)
TALEP_USAGE = ("Kullanım: <code>/talep Mehmet Bey 0532 111 22 33 3+1 kiralık 30-35 bin Yahya Kaptan veya "
               "Alikahya acil</code>\nAd, telefon, oda, bütçe, bölge ve aciliyet cümleden otomatik anlaşılır; "
               "portföy anında taranır.")


@registered()
async def cmd_talep(update: Update, ctx, user):
    from . import crm
    from . import matching as M
    from .whatsapp import get_whatsapp_chat_url

    db, cfg = _db(ctx), _cfg(ctx)
    text = " ".join(ctx.args or []).strip()
    if not text:
        await reply(update, TALEP_USAGE)
        return
    p = M.parse(text)
    if not p.name:
        await reply(update, "❌ Müşterinin adını yazın (ör. <code>/talep Ayşe Hanım 0533… 2+1 kiralık 25 bin</code>).")
        return
    if p.criteria.is_vague():
        await reply(update, "❌ Müşterinin ne aradığını yazın: oda sayısı, bütçe veya bölge.\n" + TALEP_USAGE)
        return
    did = crm.create_demand(db, name=p.name, phone=p.phone, criteria=p.criteria, notes=text, source="Telegram",
                            agent_id=user["id"], created_by=user["id"])
    new = crm.refresh(db, _today(ctx), demand_ids=[did], silent=True)
    lines = [f"✅ <b>Talep kaydedildi</b> (#{did}) — {texts.esc(p.name)}"
             + (f" · {texts.esc(p.phone)}" if p.phone else ""),
             f"🎯 {texts.esc(M.summary(p.criteria))}"]
    if new:
        lines.append(f"\n🔔 <b>{len(new)} uygun seçenek bulundu:</b>")
        for nm in sorted(new, key=lambda x: -x.result.score)[:6]:
            x = nm.candidate
            line = f"• %{nm.result.score} — {texts.esc(x.title)} · {M.money_short(x.price)}"
            if p.phone:
                wa = get_whatsapp_chat_url(p.phone, M.whatsapp_text(p.name, x, user["name"], cfg.brand_name))
                line += f' · <a href="{texts.esc(wa)}">WhatsApp\'tan gönder</a>'
            lines.append(line)
    else:
        lines.append("Şu an uygun ilan yok. Yeni ilan veya boşalan daire geldiğinde anında haber vereceğim.")
    if not p.phone:
        lines.append("ℹ️ Telefon yazılmadı — tek tık WhatsApp için web panelinden ekleyin.")
    if cfg.web_url:
        lines.append(f'🔗 <a href="{cfg.web_url}/talep/{did}">Web panelinde aç</a>')
    await reply(update, "\n".join(lines))


@registered()
async def cmd_talepler(update: Update, ctx, user):
    from . import crm
    from . import matching as M

    db = _db(ctx)
    scope = None if user["role"] == "owner" or user["can_see_all"] else user["id"]
    rows = crm.list_demands(db, scope_agent=scope, status="active")
    if not rows:
        await reply(update, "Aktif müşteri talebi yok. Eklemek için:\n" + TALEP_USAGE)
        return
    lines = [f"🎯 <b>Aktif müşteri talepleri ({len(rows)})</b>"]
    for r in rows[:40]:
        urgent = "🔴 " if r["urgency"] == "acil" else ""
        matches = f"{r['match_count']} eşleşme" + (f" ({r['new_count']} yeni)" if r["new_count"] else "")
        lines.append(f"{urgent}#{r['id']} <b>{texts.esc(r['name'])}</b> — {texts.esc(M.summary(M.Criteria.from_row(r)))}"
                     f" — {matches}" + (f" · {texts.esc(r['agent_name'])}" if scope is None and r["agent_name"] else ""))
    if len(rows) > 40:
        lines.append(f"… ve {len(rows) - 40} talep daha (web panelinde).")
    await reply(update, "\n".join(lines))


# ---------------------------------------------------------------- onay butonu
async def on_ack(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    db = _db(ctx)
    user = db.get_user_by_chat(q.from_user.id)
    if user is None or not user["active"]:
        await q.answer("Yetkiniz yok", show_alert=True)
        return
    _, pid, due = q.data.split(":")
    db.ack_reminders(int(pid), due)
    db.touch_user(user["id"])
    db.log(user["id"], "hatirlatma_onayladi", int(pid), due)
    await q.answer("Kaydedildi ✅")
    await q.edit_message_reply_markup(reply_markup=None)
    await q.message.reply_text(f"✅ {user['name']} onayladı.")


# ---------------------------------------------------------------- web girişi
@registered()
async def cmd_password(update: Update, ctx, user):
    """/sifre kullaniciadi  → web paneli için kullanıcı adı + rastgele şifre üretir."""
    if update.effective_chat.type != "private":
        await reply(update, "🔒 Şifre grupta gösterilemez. Bu komutu botla özel sohbette yazın.")
        return
    import secrets
    from werkzeug.security import generate_password_hash
    from .web import validate_username

    db = _db(ctx)
    username = None
    try:
        if ctx.args:
            username = validate_username(ctx.args[0])
            other = db.get_user_by_username(username)
            if other and other["id"] != user["id"]:
                raise ValueError("Bu kullanıcı adı alınmış, başka bir tane deneyin.")
        elif not user["username"]:
            raise ValueError("Önce bir kullanıcı adı seçin: <code>/sifre kullaniciadi</code>")
    except ValueError as e:
        await reply(update, f"❌ {e}")
        return
    password = secrets.token_urlsafe(9)
    db.set_credentials(user["id"], username, generate_password_hash(password))
    db.log(user["id"], "web_sifre_olusturdu")
    shown = username or user["username"]
    url = _cfg(ctx).web_url or "(web adresi henüz ayarlanmadı)"
    await update.effective_message.reply_text(
        f"🔐 <b>Web paneli girişi</b>\n\nAdres: {url}\nKullanıcı adı: <code>{shown}</code>\n"
        f"Şifre: <code>{password}</code>\n\nGirişten sonra şifrenizi 'Hesabım' sayfasından değiştirin "
        f"ve bu mesajı silin.", parse_mode=HTML, disable_web_page_preview=True)


@registered()
async def cmd_web(update: Update, ctx, user):
    url = _cfg(ctx).web_url
    await reply(update, f"🌐 Web paneli: {url or 'henüz ayarlanmadı'}\nGiriş bilgisi için: <code>/sifre kullaniciadi</code>")


# ---------------------------------------------------------------- ofis sahibi
@registered(owner_only=True)
async def cmd_summary(update: Update, ctx, user):
    text = build_owner_report(_db(ctx), _today(ctx), _cfg(ctx).inactivity_days, full=True)
    await reply(update, text)


@registered(owner_only=True)
async def cmd_agents(update: Update, ctx, user):
    db = _db(ctx)
    lines = ["👥 <b>Ekip</b>"]
    for u in db.list_users(active_only=False):
        n = len(db.list_properties(u["id"]))
        status = "" if u["active"] else " ❌ ayrıldı"
        last = u["last_active"][:16].replace("T", " ") if u["last_active"] else "hiç"
        lines.append(f"• id {u['id']} — {texts.esc(u['name'])} ({u['role']}){status} — {n} mülk — son: {last}")
    await reply(update, "\n".join(lines))


@registered(owner_only=True)
async def cmd_invite(update: Update, ctx, user):
    if update.effective_chat.type != "private":
        await reply(update, "🔒 Bu komutu botla özel sohbette kullanın (kod grupta herkese görünür).")
        return
    code = _db(ctx).create_invite(user["id"])
    await reply(update, f"🎟 Davet kodu: <code>{code}</code>\nEmlakçı botu açıp şunu yazsın:\n<code>/katil {code}</code>")


@registered(owner_only=True)
async def cmd_remove_agent(update: Update, ctx, user):
    db = _db(ctx)
    try:
        uid = int(ctx.args[0])
        target = int(ctx.args[1]) if len(ctx.args) > 1 else user["id"]
    except (IndexError, ValueError):
        await reply(update, "Kullanım: <code>/cikar EMLAKCI_ID [YENI_ID]</code> (/emlakcilar ile id'lere bak)")
        return
    leaving, new_owner = db.get_user(uid), db.get_user(target)
    if not leaving or not new_owner or not new_owner["active"] or uid == target:
        await reply(update, "❌ Geçersiz kullanıcı id'si.")
        return
    if leaving["role"] == "owner":
        await reply(update, "❌ Ofis sahibi çıkarılamaz.")
        return
    n = db.transfer_properties(uid, target)
    db.deactivate_user(uid)
    db.log(user["id"], "emlakci_cikardi", detail=f"{leaving['name']} -> {n} mülk {new_owner['name']}'a")
    await reply(update, f"✅ {texts.esc(leaving['name'])} çıkarıldı. {n} mülk {texts.esc(new_owner['name'])} "
                        f"adına devredildi; hatırlatmalar artık ona gidecek.")
    if new_owner["id"] != user["id"]:
        await _msgr(ctx).send(new_owner["chat_id"],
                              f"📦 {n} mülk size devredildi (önceki: {texts.esc(leaving['name'])}). /liste")


@registered(owner_only=True)
async def cmd_transfer(update: Update, ctx, user):
    db = _db(ctx)
    try:
        pid, uid = int(ctx.args[0]), int(ctx.args[1])
    except (IndexError, ValueError):
        await reply(update, "Kullanım: <code>/devret MULK_ID KULLANICI_ID</code>")
        return
    p, target = db.get_property(pid), db.get_user(uid)
    if not p or not target or not target["active"]:
        await reply(update, "❌ Mülk veya kullanıcı bulunamadı.")
        return
    db.update_property(pid, agent_id=uid)
    db.log(user["id"], "devretti", pid, f"-> {target['name']}")
    await reply(update, f"✅ #{pid} {texts.esc(p['title'])} → {texts.esc(target['name'])}")
    if target["id"] != user["id"]:
        await _msgr(ctx).send(target["chat_id"], f"📦 Size yeni mülk devredildi: #{pid} {texts.esc(p['title'])}")


@registered(owner_only=True)
async def cmd_activity(update: Update, ctx, user):
    uid = int(ctx.args[0]) if ctx.args and ctx.args[0].isdigit() else None
    rows = _db(ctx).recent_activity(uid, 30)
    if not rows:
        await reply(update, "Kayıt yok.")
        return
    lines = ["🕒 <b>Son işlemler</b>"]
    for r in rows:
        mulk = f" #{r['property_id']}" if r["property_id"] else ""
        lines.append(f"{r['ts'][5:16].replace('T', ' ')} — {texts.esc(r['user_name'])}: {r['action']}{mulk} {texts.esc(r['detail'])}")
    await reply(update, "\n".join(lines))


@registered(owner_only=True)
async def cmd_csv(update: Update, ctx, user):
    db = _db(ctx)
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["No", "Emlakçı", "Mülk", "Ev sahibi", "Ev sahibi tel", "Kiracı", "Kiracı tel", "Kira",
                "Yenileme", "Geçen yıl %", "Planlanan %", "Planlanan kira"])
    for p in db.list_properties():
        w.writerow([p["id"], _agent_name(db, p), p["title"], p["landlord_name"], p["landlord_phone"],
                    p["tenant_name"], p["tenant_phone"], p["rent"], R.fmt_date(p["renewal_date"]),
                    p["last_increase_pct"], p["planned_pct"], p["planned_rent"]])
    data = io.BytesIO(("\ufeff" + buf.getvalue()).encode("utf-8"))
    data.name = f"portfoy_{_today(ctx).isoformat()}.csv"
    await update.effective_message.reply_document(data)


@registered(owner_only=True)
async def cmd_test_reminders(update: Update, ctx, user):
    n = await run_reminders(_db(ctx), _msgr(ctx), _today(ctx), _cfg(ctx).group_chat_id or None)
    await reply(update, f"Kontrol tamam: {n} yeni hatırlatma gönderildi.")


async def cmd_chatid(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Herhangi bir sohbette (grup dahil) o sohbetin ID'sini gösterir. Kayıt gerekmez."""
    chat = update.effective_chat
    await update.effective_message.reply_text(
        f"Sohbet ID: {chat.id}\nTür: {chat.type}\nSenin kullanıcı ID'n: {update.effective_user.id}")


@registered(owner_only=True)
async def cmd_test_group(update: Update, ctx, user):
    gid = _cfg(ctx).group_chat_id
    if not gid:
        await reply(update, "GROUP_CHAT_ID ayarlı değil. Gruba botu ekleyip grupta /chatid yazın, "
                            "çıkan ID'yi Railway'de GROUP_CHAT_ID olarak girin.")
        return
    ok = await _msgr(ctx).send(gid, "✅ Test: bot bu gruba mesaj gönderebiliyor.")
    await reply(update, "Gruba gönderildi ✅" if ok else
                "❌ Gruba gönderilemedi. Bot gruba ekli mi? ID doğru mu (-100... ile başlamalı)?")


def register(app) -> None:
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler(["yardim", "help"], cmd_help))
    app.add_handler(CommandHandler("katil", cmd_join))
    app.add_handler(CommandHandler("chatid", cmd_chatid))
    app.add_handler(CommandHandler("kurulum", cmd_setup))
    app.add_handler(add_conversation())
    for names, fn in [
        ("liste", cmd_list), ("yaklasan", cmd_upcoming), ("mulk", cmd_property), ("zam", cmd_zam),
        ("yenile", cmd_renew), ("duzenle", cmd_edit), ("sil", cmd_delete), ("tufe", cmd_tufe), ("hukuk", cmd_hukuk),
        ("talep", cmd_talep), ("talepler", cmd_talepler),
        ("sifre", cmd_password), ("web", cmd_web),
        ("ozet", cmd_summary), ("emlakcilar", cmd_agents), ("davet", cmd_invite),
        ("cikar", cmd_remove_agent), ("devret", cmd_transfer), ("aktivite", cmd_activity),
        ("csv", cmd_csv), ("test_hatirlat", cmd_test_reminders), ("test_grup", cmd_test_group),
    ]:
        app.add_handler(CommandHandler(names, fn))
    app.add_handler(CallbackQueryHandler(on_ack, pattern=r"^ack:\d+:\d{4}-\d{2}-\d{2}$"))
