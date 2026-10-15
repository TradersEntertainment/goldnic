"""Güvenli web paneli (kullanıcı adı + şifre ile giriş) — Gold Nicomedia OS çekirdeği.

Modüller (hukuk, belgeler, CRM, yedekleme, PWA) kendi rotalarını
``WebContext`` üzerinden kaydeder; uç nokta adları düz kalır (url_for('takip') gibi).
"""
from __future__ import annotations

import csv
import hashlib
import hmac
import io
import logging
import mimetypes
import re
import secrets
import time
from datetime import date, datetime, timedelta
from functools import wraps
from pathlib import Path
from typing import Any, Callable

from flask import (
    Flask, Response, abort, flash, g, jsonify, redirect, render_template, request, session, url_for,
)
from markupsafe import Markup, escape
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

from . import rent as R
from . import services, texts
from .config import Config
from .db import Database
from .messenger import send_telegram_sync
from .reminders import collect_issues, get_tufe

log = logging.getLogger(__name__)

USERNAME_RE = re.compile(r"^[a-z0-9_.-]{3,32}$")
MAX_FAILS, WINDOW = 5, 600  # 10 dakikada 5 hatalı deneme -> kilit
_DUMMY_HASH = generate_password_hash("dummy-password-for-timing")
STATIC_DIR = Path(__file__).parent / "static"

# Aktivite kodlarının okunur karşılıkları: (etiket, ikon, property_id alanı neyi gösteriyor)
ACTION_LABELS: dict[str, tuple[str, str, str | None]] = {
    "web_giris": ("Panele giriş yaptı", "lock", None),
    "sifre_degistirdi": ("Şifresini değiştirdi", "key", None),
    "web_sifre_olusturdu": ("Web şifresi oluşturdu", "key", None),
    "aradim": ("Müşteriyi aradı", "phone", "property"),
    "hatirlatma_onayladi": ("Hatırlatmayı onayladı", "check", "property"),
    "zam_girdi": ("Zam oranı girdi", "percent", "property"),
    "yeniledi": ("Sözleşmeyi yeniledi", "refresh", "property"),
    "arsivledi": ("Mülkü arşivledi", "archive", "property"),
    "mulk_ekledi": ("Mülk ekledi", "home", "property"),
    "duzenledi": ("Mülkü düzenledi", "edit", "property"),
    "devretti": ("Mülkü devretti", "swap", "property"),
    "kiraci_cikti": ("Kiracı çıkışı işledi", "door", "property"),
    "yeni_kiraci_girdi": ("Yeni kiracı kaydetti", "user-plus", "property"),
    "sahibinden_kira_takibine_aldi": ("İlanı kira takibine aldı", "building", "property"),
    "ilan_kiralandi": ("İlanı kiraladı", "sparkles", "property"),
    "ilan_satildi": ("İlanı sattı", "sparkles", "listing"),
    "ilan_archive": ("İlanı yayından kaldırdı", "archive", "listing"),
    "ilan_delete": ("İlanı sildi", "x", "listing"),
    "toplu_giris": ("Toplu portföy aktardı", "upload", None),
    "kullanici_ekledi": ("Kullanıcı ekledi", "user-plus", "user"),
    "yetki_guncelledi": ("Yetki güncelledi", "shield", "user"),
    "sifre_sifirladi": ("Şifre sıfırladı", "key", "user"),
    "kullanici_durum_degisti": ("Kullanıcı durumunu değiştirdi", "users", "user"),
    "mulk_devretti": ("Portföy devretti", "swap", "user"),
    "emlakci_cikardi": ("Emlakçıyı çıkardı", "users", None),
    "katildi": ("Ekibe katıldı", "user-plus", None),
    "kurulum_koduyla_sahip_oldu": ("Kurulum koduyla sahip oldu", "crown", None),
    "tufe_guncelledi": ("TÜFE oranını güncelledi", "percent", None),
    "disa_aktardi": ("Portföyü dışa aktardı", "download", None),
}


def validate_username(u: str) -> str:
    u = (u or "").strip().lower()
    if not USERNAME_RE.match(u):
        raise ValueError("Kullanıcı adı 3-32 karakter olmalı (harf, rakam, . _ -).")
    return u


def validate_password(p: str) -> str:
    if len(p or "") < 8:
        raise ValueError("Şifre en az 8 karakter olmalı.")
    return p


def initials(name: str | None) -> str:
    parts = [w for w in (name or "").split() if w]
    if not parts:
        return "?"
    letters = parts[0][0] + (parts[1][0] if len(parts) > 1 else "")
    return letters.translate(str.maketrans("iı", "İI")).upper()


def format_phone(phone: str | None) -> str:
    """05321112233 -> 0532 111 22 33 (gösterim amaçlı)."""
    d = re.sub(r"\D", "", phone or "")
    if d.startswith("90") and len(d) == 12:
        d = "0" + d[2:]
    elif len(d) == 10:
        d = "0" + d
    if len(d) == 11 and d.startswith("0"):
        return f"{d[:4]} {d[4:7]} {d[7:9]} {d[9:]}"
    return phone or ""


def format_dt(value: str | None) -> str:
    """ISO tarih-saat -> 15.10.2026 14:30."""
    if not value:
        return "-"
    try:
        d = datetime.fromisoformat(value)
    except ValueError:
        return value
    return d.strftime("%d.%m.%Y %H:%M")


def plain_number(x: float | None) -> str:
    """Form alanları için sade sayı: 32500.0 -> '32500', 32500.5 -> '32500.5'."""
    if x is None:
        return ""
    return f"{x:.2f}".rstrip("0").rstrip(".")


def parse_tabular_input(raw_text: str) -> list[dict]:
    """Excel'den kopyalanan (tab-separated) veya CSV formatındaki metni mülk sözlüklerine çevirir."""
    lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
    if not lines:
        return []

    first_line = lines[0]
    if "\t" in first_line:
        delimiter = "\t"
    elif ";" in first_line:
        delimiter = ";"
    elif "," in first_line:
        delimiter = ","
    else:
        delimiter = "\t"

    reader = csv.reader(io.StringIO(raw_text.strip()), delimiter=delimiter)
    raw_rows = [r for r in reader if any(cell.strip() for cell in r)]
    if not raw_rows:
        return []

    first_row = [c.strip().lower() for c in raw_rows[0]]
    header_keywords = {
        "başlık", "baslik", "daire no", "daire adı", "daire adi", "kira", "tutar", "tarih",
        "yenileme", "kiracı", "kiraci", "ev sahibi", "telefon", "danışman", "danisman", "notlar", "açıklama",
        "başlangıç", "baslangic",
    }
    is_second_cell_number = False
    if len(first_row) >= 2:
        clean_num = first_row[1].replace(".", "").replace(",", "").replace(" ", "").replace("tl", "").replace("₺", "")
        if clean_num.isdigit():
            is_second_cell_number = True

    match_count = sum(1 for c in first_row if any(k in c for k in header_keywords))
    has_header = (not is_second_cell_number) and (
        match_count >= 2 or (match_count >= 1 and first_row[0] in ("başlık", "baslik", "daire", "mülk", "adres"))
    )
    headers = first_row if has_header else []
    data_rows = raw_rows[1:] if has_header else raw_rows

    results = []
    for r in data_rows:
        cells = [c.strip() for c in r]
        if not cells or not any(cells):
            continue

        item: dict[str, str] = {}
        if has_header:
            for h, val in zip(headers, cells):
                if any(x in h for x in ("kiracı tel", "kiraci tel", "kiracı telefon", "kiraci telefon", "kiraci_tel")):
                    item["tenant_phone"] = val
                elif any(x in h for x in ("ev sahibi tel", "sahip tel", "malik tel", "ev sahibi telefon", "ev_sahibi_tel")):
                    item["landlord_phone"] = val
                elif any(x in h for x in ("kiracı", "kiraci", "tenant")):
                    item["tenant_name"] = val
                elif any(x in h for x in ("ev sahibi", "sahip", "malik", "landlord")):
                    item["landlord_name"] = val
                elif any(x in h for x in ("başlangıç", "baslangic", "giriş tarihi", "giris tarihi", "start")):
                    item["lease_start_date"] = val
                elif any(x in h for x in ("başlık", "baslik", "daire", "mülk", "adres", "ilan", "title")):
                    item["title"] = val
                elif any(x in h for x in ("tarih", "yenileme", "kontrat", "vade", "donem", "dönem", "date")):
                    item["renewal_date"] = val
                elif any(x in h for x in ("kira", "tutar", "bedel", "fiyat", "rent")):
                    item["rent"] = val
                elif any(x in h for x in ("danışman", "danisman", "emlakçı", "temsilci", "consultant")):
                    item["consultant"] = val
                elif any(x in h for x in ("not", "açıklama", "aciklama", "bilgi", "notes")):
                    item["notes"] = val

        if "title" not in item and len(cells) >= 1:
            item["title"] = cells[0]
        if "rent" not in item and len(cells) >= 2:
            item["rent"] = cells[1]
        if "renewal_date" not in item and len(cells) >= 3:
            item["renewal_date"] = cells[2]
        if "tenant_name" not in item and len(cells) >= 4:
            item["tenant_name"] = cells[3]
        if "tenant_phone" not in item and len(cells) >= 5:
            item["tenant_phone"] = cells[4]
        if "landlord_name" not in item and len(cells) >= 6:
            item["landlord_name"] = cells[5]
        if "landlord_phone" not in item and len(cells) >= 7:
            item["landlord_phone"] = cells[6]
        if "consultant" not in item and len(cells) >= 8:
            item["consultant"] = cells[7]
        if "notes" not in item and len(cells) >= 9:
            item["notes"] = cells[8]

        results.append(item)

    return results


def _static_version() -> str:
    """Statik dosyaların içerik özetinden sürüm üretir (önbellek kırma + servis çalışanı sürümü)."""
    h = hashlib.sha256()
    for p in sorted(STATIC_DIR.rglob("*")):
        if p.is_file():
            h.update(p.name.encode())
            h.update(p.read_bytes())
    sw = Path(__file__).parent / "templates" / "sw.js"
    if sw.exists():
        h.update(sw.read_bytes())
    return h.hexdigest()[:10]


class WebContext:
    """Çekirdek ve modül rotalarının ortak kullandığı yardımcılar."""

    def __init__(self, app: Flask, db: Database, cfg: Config):
        self.app, self.db, self.cfg = app, db, cfg
        self.fails: dict[str, list[float]] = {}
        # Modüllerin menüye eklediği öğeler
        self.nav_extra: list[dict[str, Any]] = []
        self.nav_owner: list[dict[str, Any]] = []
        self.quick: list[dict[str, Any]] = []
        self.takip_endpoints: tuple[str, ...] = ()
        self.owner_endpoints: tuple[str, ...] = ()
        self.agent_last: dict[str, Any] | None = None
        # Spotlight sağlayıcıları ve komutları
        self.spotlight_providers: list[Callable[..., Any]] = []
        self.spotlight_actions: list[dict[str, Any]] = []
        # Panel kartlarına modüllerin eklediği ek içerik
        self.nav_counters: list[Callable[[Any], int]] = []

    # -------------------------------------------------------------- zaman & yetki
    def today(self) -> date:
        return datetime.now(self.cfg.tz).date()

    @staticmethod
    def can_view_all(user) -> bool:
        return bool(user) and (user["role"] == "owner" or bool(dict(user).get("can_see_all")))

    def scope_agent(self, user) -> int | None:
        """Görebileceği mülkler için agent filtresi: None = herkes."""
        return None if self.can_view_all(user) else user["id"]

    def login_required(self, owner_only: bool = False):
        db = self.db

        def deco(fn):
            @wraps(fn)
            def wrapper(*a, **kw):
                if g.user is None:
                    if request.path.startswith("/api/"):
                        return jsonify({"error": "Giriş yapmalısınız."}), 401
                    return redirect(url_for("login"))
                if owner_only and g.user["role"] != "owner":
                    abort(403)
                db.touch_user(g.user["id"])
                return fn(*a, **kw)
            return wrapper
        return deco

    def get_prop(self, pid: int):
        p = self.db.get_property(pid)
        if p is None or p["status"] not in ("active", "vacant"):
            abort(404)
        if not self.can_view_all(g.user) and p["agent_id"] != g.user["id"]:
            abort(403)
        return p

    def notify_owners(self, text: str) -> None:
        cfg, db = self.cfg, self.db
        if not cfg.bot_token:
            return
        target_ids = set()
        for o in db.list_users("owner"):
            if o["id"] != (g.user["id"] if g.user else None) and o["channel"] == "telegram":
                target_ids.add(o["chat_id"])
        if cfg.chat_id:
            target_ids.add(cfg.chat_id)
        elif cfg.group_chat_id:
            target_ids.add(cfg.group_chat_id)
        for cid in target_ids:
            send_telegram_sync(cfg.bot_token, cid, text)

    def safe_next(self, fallback: str) -> str:
        """Açık yönlendirmeyi önler: sadece site içi yol kabul edilir."""
        nxt = request.form.get("next") or request.args.get("next") or ""
        if nxt.startswith("/") and not nxt.startswith("//"):
            return nxt
        ref = request.referrer or ""
        host = request.host_url.rstrip("/")
        if ref.startswith(host):
            return ref
        return fallback

    # -------------------------------------------------------------- modül kayıt yardımcıları
    def add_nav(self, endpoint: str, icon: str, label: str, *, owner: bool = False, endpoints=None,
                count: Callable[[Any], int] | None = None, count_cls: str = "") -> None:
        item = {"endpoint": endpoint, "icon": icon, "label": label, "endpoints": endpoints or [endpoint],
                "count_fn": count, "count_cls": count_cls}
        (self.nav_owner if owner else self.nav_extra).append(item)

    def add_quick(self, endpoint: str, icon: str, label: str, hint: str, owner: bool = False, **params) -> None:
        self.quick.append({"endpoint": endpoint, "icon": icon, "label": label, "hint": hint,
                           "owner": owner, "params": params})

    def add_action(self, title: str, endpoint: str, icon: str, keywords: str = "", owner: bool = False,
                   **params) -> None:
        self.spotlight_actions.append({"title": title, "endpoint": endpoint, "icon": icon,
                                       "keywords": keywords, "owner": owner, "params": params})


class _Nav:
    """Şablonlara verilen menü bilgisi; uyarı sayısı ilk erişimde hesaplanır."""

    def __init__(self, ctx: WebContext, user):
        self._ctx, self._user = ctx, user
        is_owner = user["role"] == "owner"
        self.takip_endpoints = ctx.takip_endpoints
        self.owner_endpoints = ctx.owner_endpoints
        self.agent_last = ctx.agent_last
        self.extra = [self._resolve(i) for i in ctx.nav_extra]
        self.owner_extra = [self._resolve(i) for i in ctx.nav_owner] if is_owner else []
        self.quick = [
            {"url": url_for(q["endpoint"], **q["params"]), "icon": q["icon"], "label": q["label"], "hint": q["hint"]}
            for q in ctx.quick if is_owner or not q["owner"]
        ]

    def _resolve(self, item: dict[str, Any]) -> dict[str, Any]:
        out = dict(item)
        fn = item.get("count_fn")
        try:
            out["count"] = fn(self._user) if fn else 0
        except Exception:  # noqa: BLE001 - menü sayacı sayfayı asla düşürmemeli
            log.exception("Menü sayacı hesaplanamadı: %s", item.get("endpoint"))
            out["count"] = 0
        return out

    @property
    def alerts(self) -> int:
        if "_agenda_alerts" not in g:
            from . import agenda
            items = agenda.build_cached(self._ctx, self._user)
            g._agenda_alerts = sum(1 for i in items if i.severity in ("critical", "warning"))
        return g._agenda_alerts


def create_app(db: Database, cfg: Config) -> Flask:
    from .scraper import seed_initial_listings, sync_listings_data

    # Başlangıçta kayıtlı ilan yoksa initial_listings.json dosyasını yükle
    try:
        seed_initial_listings(db)
    except Exception as e:
        log.warning("İlk ilan tohumlaması yapılamadı: %s", e)

    mimetypes.add_type("application/manifest+json", ".webmanifest")
    app = Flask(__name__)
    app.secret_key = cfg.secret_key
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=cfg.cookie_secure,
        PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
        MAX_CONTENT_LENGTH=16 * 1024 * 1024,
        SEND_FILE_MAX_AGE_DEFAULT=timedelta(days=7),
    )
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
    ctx = WebContext(app, db, cfg)
    app.extensions["gn"] = ctx
    version = _static_version()
    app.config["ASSET_VERSION"] = version

    def asset(path: str) -> str:
        return f"{url_for('static', filename=path)}?v={version}"

    app.jinja_env.filters.update(
        money=R.money, pct=R.pct_str, tdate=R.fmt_date, initials=initials, phone=format_phone, dt=format_dt,
        plainnum=plain_number,
    )
    app.jinja_env.globals.update(brand=cfg.brand_name, asset=asset, action_labels=ACTION_LABELS)
    login_required = ctx.login_required
    get_prop = ctx.get_prop
    notify_owners = ctx.notify_owners
    today = ctx.today
    fails = ctx.fails

    # ------------------------------------------------------------ güvenlik
    def csrf_token() -> str:
        if "csrf" not in session:
            session["csrf"] = secrets.token_urlsafe(24)
        return session["csrf"]

    app.jinja_env.globals["csrf_token"] = csrf_token

    @app.context_processor
    def inject_nav():
        if getattr(g, "user", None):
            return {"nav": _Nav(ctx, g.user)}
        return {}

    @app.before_request
    def load_user_and_check_csrf():
        g.user = None
        if request.endpoint == "static":
            return
        uid = session.get("uid")
        if uid:
            u = db.get_user(uid)
            if u and u["active"]:  # ayrılan emlakçının oturumu anında geçersiz olur
                g.user = u
            else:
                session.clear()
        if request.method == "POST":
            # API senkronizasyon rotası için oturum kontrolü veya CSRF kontrolü
            if request.path.startswith("/api/sahibinden/sync"):
                header_csrf = request.headers.get("X-CSRF-Token", "")
                form_csrf = request.form.get("csrf", "")
                expected = session.get("csrf", "")
                if expected and (hmac.compare_digest(header_csrf, expected) or hmac.compare_digest(form_csrf, expected)):
                    return
                if not g.user:
                    abort(401, "Giriş yapmalısınız.")
                return

            sent = request.form.get("csrf", "") or request.headers.get("X-CSRF-Token", "")
            expected = session.get("csrf", "")
            if not expected or not hmac.compare_digest(sent, expected):
                abort(400, "Geçersiz istek (CSRF). Sayfayı yenileyip tekrar deneyin.")

    @app.after_request
    def headers(resp):
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        resp.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=(), payment=()"
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self'; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: blob: https://*.shbdn.com https://*.sahibinden.com https:; "
            "connect-src 'self'; "
            "manifest-src 'self'; "
            "worker-src 'self'; "
            "font-src 'self'; "
            "object-src 'none'; "
            "base-uri 'self'; "
            "form-action 'self'; "
            "frame-ancestors 'none'"
        )
        if request.path.startswith("/static/"):
            # sürümlü (?v=) dosyalar değişmez; sürümsüzler (manifest ikonları) bir gün önbellekte kalır
            resp.headers["Cache-Control"] = (
                "public, max-age=31536000, immutable" if request.args.get("v") else "public, max-age=86400"
            )
        elif "Cache-Control" not in resp.headers or request.endpoint not in ("service_worker", "manifest"):
            resp.headers["Cache-Control"] = "no-store"
        if cfg.cookie_secure:
            resp.headers["Strict-Transport-Security"] = "max-age=31536000"
        return resp

    @app.errorhandler(HTTPException)
    def http_error(e: HTTPException):
        if request.path.startswith("/api/"):
            return jsonify({"error": e.description, "code": e.code}), e.code
        titles = {400: "Geçersiz istek", 401: "Giriş gerekli", 403: "Yetkiniz yok", 404: "Bulunamadı",
                  405: "İzin verilmeyen işlem", 413: "Dosya çok büyük", 429: "Çok fazla deneme"}
        return render_template("error.html", code=e.code, title=titles.get(e.code or 500, e.name),
                               message=e.description), e.code

    def too_many(ip: str) -> bool:
        now = time.time()
        fails[ip] = [t for t in fails.get(ip, []) if now - t < WINDOW]
        return len(fails[ip]) >= MAX_FAILS

    def parse_money_field(raw: str | None, fallback: float | None = None) -> float:
        raw = (raw or "").strip()
        if not raw:
            if fallback is None:
                raise ValueError("Kira tutarı gerekli.")
            return float(fallback)
        return R.parse_amount(raw)

    def parse_date_field(raw: str | None, fallback: date) -> str:
        raw = (raw or "").strip()
        return R.parse_date(raw).isoformat() if raw else fallback.isoformat()

    # ------------------------------------------------------------ oturum
    @app.get("/health")
    def health():
        return "ok"

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if g.user:
            return redirect(url_for("dashboard"))
        if request.method == "POST":
            ip = request.remote_addr or "?"
            if too_many(ip):
                flash("Çok fazla hatalı deneme. 10 dakika sonra tekrar deneyin.", "err")
                return render_template("login.html"), 429
            u = db.get_user_by_username(request.form.get("username", "").strip())
            pw = request.form.get("password", "")
            ok = check_password_hash(u["password_hash"] if u and u["password_hash"] else _DUMMY_HASH, pw)
            if u and u["password_hash"] and u["active"] and ok:
                session.clear()
                session.permanent = True
                session["uid"] = u["id"]
                db.touch_user(u["id"])
                db.log(u["id"], "web_giris")
                fails.pop(ip, None)
                return redirect(url_for("dashboard"))
            fails.setdefault(ip, []).append(time.time())
            flash("Kullanıcı adı veya şifre hatalı.", "err")
        return render_template("login.html")

    @app.post("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.route("/hesap", methods=["GET", "POST"])
    @login_required()
    def account():
        if request.method == "POST":
            try:
                if not check_password_hash(g.user["password_hash"], request.form.get("old", "")):
                    raise ValueError("Mevcut şifre hatalı.")
                new = validate_password(request.form.get("new", ""))
                if new != request.form.get("new2"):
                    raise ValueError("Yeni şifreler eşleşmiyor.")
                db.set_credentials(g.user["id"], None, generate_password_hash(new))
                db.log(g.user["id"], "sifre_degistirdi")
                flash("Şifre güncellendi.", "ok")
            except ValueError as e:
                flash(str(e), "err")
        mine = db.list_properties(g.user["id"])
        return render_template("account.html", my_count=len(mine),
                               recent=db.recent_activity(g.user["id"], 8))

    # ------------------------------------------------------------ paneller
    @app.get("/")
    @login_required()
    def dashboard():
        from . import agenda
        from .whatsapp import build_tenant_message, get_whatsapp_chat_url

        owner = g.user["role"] == "owner"
        can_view_all = ctx.can_view_all(g.user)
        only = request.args.get("agent", type=int) if can_view_all else g.user["id"]
        flt = request.args.get("f", "all")
        props = db.list_properties(only)
        vacant_props = db.list_properties(only, status="vacant")
        names = {u["id"]: u["name"] for u in db.list_users(active_only=False)}
        acked_pairs = db.acked_pairs()
        t = today()

        rows = []
        for p in props:
            days = texts.days_left(p, t)
            agent_name = names.get(p["agent_id"], "?")
            is_acked = (p["id"], p["renewal_date"]) in acked_pairs
            wa_url = ""
            if p["tenant_phone"]:
                wa_msg = build_tenant_message(
                    title=p["title"],
                    renewal_date_str=R.fmt_date(p["renewal_date"]),
                    days_left=days,
                    agent_name=agent_name,
                    brand=cfg.brand_name,
                )
                wa_url = get_whatsapp_chat_url(p["tenant_phone"], wa_msg)
            rows.append(dict(p, days=days, agent=agent_name, is_acked=is_acked, wa_url=wa_url))

        stats = {
            "total": len(rows),
            "soon": sum(1 for r in rows if 0 <= r["days"] <= 30),
            "overdue": sum(1 for r in rows if r["days"] < 0),
            "missing": sum(1 for r in rows if r["days"] <= 30 and r["planned_rent"] is None),
            "uncalled": sum(1 for r in rows if r["days"] <= 30 and not r["is_acked"]),
            "rent": sum(r["rent"] for r in rows),
            "vacant": len(vacant_props),
        }
        filters = {
            "soon": lambda r: 0 <= r["days"] <= 30,
            "overdue": lambda r: r["days"] < 0,
            "missing": lambda r: r["days"] <= 30 and r["planned_rent"] is None,
            "uncalled": lambda r: r["days"] <= 30 and not r["is_acked"],
        }
        shown = [r for r in rows if filters[flt](r)] if flt in filters else rows
        team, inactive = [], []
        if owner:
            for u in db.list_users():
                mine = db.list_properties(u["id"])
                team.append(dict(u, count=len(mine), soon=sum(
                    1 for p in mine if 0 <= texts.days_left(p, t) <= 30),
                    rent=sum(p["rent"] for p in mine)))
            inactive = collect_issues(db, t, cfg.inactivity_days)["inactive"]
        focus = agenda.build_cached(ctx, g.user)[:6]
        hour = datetime.now(cfg.tz).hour
        greeting = "Günaydın" if 5 <= hour < 12 else ("İyi günler" if hour < 18 else "İyi akşamlar")
        return render_template(
            "dashboard.html",
            rows=shown,
            all_count=len(rows),
            stats=stats,
            team=team,
            inactive=inactive,
            owner=owner,
            can_view_all=can_view_all,
            only=only,
            only_name=names.get(only) if only and can_view_all else None,
            flt=flt,
            vacant=[dict(p, agent=names.get(p["agent_id"], "?")) for p in vacant_props],
            focus=focus,
            greeting=greeting,
            today=t,
        )

    @app.get("/mulk/<int:pid>")
    @login_required()
    def property_view(pid: int):
        p = get_prop(pid)
        tufe = get_tufe(db)
        cap = R.apply_pct(p["rent"], tufe) if tufe is not None else None
        agent = db.get_user(p["agent_id"])
        days = texts.days_left(p, today())

        from .whatsapp import build_tenant_message, get_whatsapp_chat_url

        wa_tenant_url = ""
        if p["tenant_phone"]:
            t_msg = build_tenant_message(
                title=p["title"],
                renewal_date_str=R.fmt_date(p["renewal_date"]),
                days_left=days,
                agent_name=agent["name"] if agent else cfg.brand_name,
                brand=cfg.brand_name,
            )
            wa_tenant_url = get_whatsapp_chat_url(p["tenant_phone"], t_msg)

        wa_landlord_url = ""
        if p["landlord_phone"]:
            l_msg = (
                f"Sayın Mülk Sahibimiz,\n\n"
                f"{cfg.brand_name} Gayrimenkul'den {agent['name'] if agent else ''}. "
                f"'{p['title']}' mülkünüzün kira yenileme tarihi {R.fmt_date(p['renewal_date'])} tarihinde dolmaktadır. "
                f"Yeni dönem kira artış oranı ve güncel piyasa değerlendirmesi için sizinle görüşmek isteriz.\n\n"
                f"İyi çalışmalar dileriz."
            )
            wa_landlord_url = get_whatsapp_chat_url(p["landlord_phone"], l_msg)

        is_acked = db.is_reminder_acked(pid, p["renewal_date"])
        panels = []
        for hook in app.extensions.get("gn_property_panels", []):
            try:
                panel = hook(p)
            except Exception:  # noqa: BLE001 - bir modül paneli sayfayı düşürmemeli
                log.exception("Mülk paneli oluşturulamadı (#%s)", pid)
                panel = None
            if panel:
                panels.append(panel)

        return render_template(
            "property.html",
            p=p,
            days=days,
            agent=agent,
            hist=db.history(pid),
            tufe=tufe,
            cap=cap,
            wa_tenant_url=wa_tenant_url,
            wa_landlord_url=wa_landlord_url,
            is_acked=is_acked,
            over_cap=tufe is not None and p["planned_pct"] is not None and p["planned_pct"] > tufe,
            panels=panels,
            agents=db.list_users() if g.user["role"] == "owner" else [],
            today_iso=today().isoformat(),
        )

    @app.route("/mulk/<int:pid>/duzenle", methods=["GET", "POST"])
    @login_required()
    def property_edit(pid: int):
        p = get_prop(pid)
        if request.method == "POST":
            try:
                fields = services.parse_property_form(request.form, partial=True)
                if fields.get("rent") is not None and fields["rent"] != p["rent"]:
                    # kira değişince planlanan artış geçersizleşir
                    fields.update(planned_pct=None, planned_rent=None)
                if g.user["role"] == "owner":
                    new_agent = request.form.get("agent_id", type=int)
                    if new_agent and new_agent != p["agent_id"] and db.get_user(new_agent):
                        fields["agent_id"] = new_agent
                db.update_property(pid, **fields)
                db.log(g.user["id"], "duzenledi", pid, ", ".join(sorted(fields)))
                flash("Mülk bilgileri güncellendi.", "ok")
                return redirect(url_for("property_view", pid=pid))
            except ValueError as e:
                flash(str(e), "err")
        agents = db.list_users() if g.user["role"] == "owner" else []
        return render_template("property_edit.html", p=p, f=request.form, agents=agents)

    @app.post("/mulk/<int:pid>/devret")
    @login_required(owner_only=True)
    def property_transfer(pid: int):
        p = get_prop(pid)
        target = db.get_user(request.form.get("to_user", type=int) or 0)
        if not target or not target["active"]:
            flash("Geçerli bir danışman seçin.", "err")
            return redirect(url_for("property_view", pid=pid))
        db.update_property(pid, agent_id=target["id"])
        db.log(g.user["id"], "devretti", pid, f"-> {target['name']}")
        flash(f"Mülk {target['name']} adına devredildi.", "ok")
        return redirect(url_for("property_view", pid=pid))

    @app.post("/mulk/<int:pid>/aradim")
    @login_required()
    def property_aradim(pid: int):
        p = get_prop(pid)
        notes = request.form.get("notes", "").strip() or "Müşteri arandı / görüşüldü."
        db.ack_reminders(pid, p["renewal_date"], notes=notes)
        db.log(g.user["id"], "aradim", pid, f"{p['renewal_date']} dönemi için arandı. Not: {notes}")
        flash("📞 Müşteri arandı olarak kaydedildi. Bu yenileme dönemi için bildirim takibi tamamlandı.", "ok")
        return redirect(ctx.safe_next(url_for("property_view", pid=pid)))

    @app.post("/mulk/<int:pid>/zam")
    @login_required()
    def property_zam(pid: int):
        p = get_prop(pid)
        try:
            pct, new = services.set_planned_increase(db, g.user, p, request.form.get("value", ""))
            flash(f"Kaydedildi: {R.pct_str(pct)} → {R.money(new)}", "ok")
            notify_owners(f"📈 {texts.esc(g.user['name'])} (web), #{pid} {texts.esc(p['title'])} için zam girdi: "
                          f"{R.pct_str(pct)} → {R.money(new)}")
        except ValueError as e:
            flash(str(e), "err")
        return redirect(url_for("property_view", pid=pid))

    @app.post("/mulk/<int:pid>/yenile")
    @login_required()
    def property_renew(pid: int):
        p = get_prop(pid)
        try:
            new, pct, nxt = services.renew(db, g.user, p, request.form.get("value"))
            flash(f"Yenilendi: {R.money(new)} ({R.pct_str(pct)}). Sonraki: {R.fmt_date(nxt)}", "ok")
            notify_owners(f"🔁 {texts.esc(g.user['name'])} (web) yeniledi: #{pid} {texts.esc(p['title'])} — "
                          f"{R.money(p['rent'])} → {R.money(new)} ({R.pct_str(pct)})")
        except ValueError as e:
            flash(str(e), "err")
        return redirect(url_for("property_view", pid=pid))

    @app.post("/mulk/<int:pid>/sil")
    @login_required()
    def property_delete(pid: int):
        p = get_prop(pid)
        db.update_property(pid, status="archived")
        db.log(g.user["id"], "arsivledi", pid, p["title"])
        notify_owners(f"🗄 {texts.esc(g.user['name'])} (web), #{pid} {texts.esc(p['title'])} mülkünü arşivledi.")
        flash("Mülk arşivlendi.", "ok")
        return redirect(url_for("dashboard"))

    @app.route("/ekle", methods=["GET", "POST"])
    @login_required()
    def add():
        if request.method == "POST":
            try:
                form = request.form.to_dict()
                form["renewal_date"] = form.get("renewal_date", "")
                pid = services.create_property(db, g.user, form)
                notify_owners(f"🆕 {texts.esc(g.user['name'])} (web) yeni mülk ekledi: "
                              f"<b>{texts.esc(form['title'])}</b> (#{pid})")
                flash("Mülk eklendi.", "ok")
                return redirect(url_for("property_view", pid=pid))
            except ValueError as e:
                flash(str(e), "err")
        return render_template("add.html", f=request.form)

    @app.get("/disa-aktar/portfoy.csv")
    @login_required()
    def export_portfolio_csv():
        if not ctx.can_view_all(g.user):
            abort(403)
        names = {u["id"]: u["name"] for u in db.list_users(active_only=False)}
        buf = io.StringIO()
        w = csv.writer(buf, delimiter=";")
        w.writerow(["No", "Durum", "Danışman", "Mülk", "Ev sahibi", "Ev sahibi tel", "Kiracı", "Kiracı tel",
                    "Kira", "Yenileme", "Geçen yıl %", "Planlanan %", "Planlanan kira", "Not"])
        for status in ("active", "vacant"):
            for p in db.list_properties(status=status):
                w.writerow([p["id"], "Aktif" if status == "active" else "Boş", names.get(p["agent_id"], "?"),
                            p["title"], p["landlord_name"], p["landlord_phone"], p["tenant_name"], p["tenant_phone"],
                            p["rent"], R.fmt_date(p["renewal_date"]), p["last_increase_pct"], p["planned_pct"],
                            p["planned_rent"], p["notes"]])
        db.log(g.user["id"], "disa_aktardi", None, "portfoy.csv")
        return Response(
            "﻿" + buf.getvalue(),
            mimetype="text/csv; charset=utf-8",
            headers={"Content-Disposition": f"attachment; filename=portfoy_{today().isoformat()}.csv"},
        )

    @app.get("/aktivite")
    @login_required(owner_only=True)
    def activity():
        uid = request.args.get("user", type=int)
        return render_template("activity.html", rows=db.recent_activity(uid, 150),
                               users=db.list_users(active_only=False), selected_user=uid)

    # ------------------------------------------------------------ ekip / kullanıcı yönetimi
    @app.get("/ekip")
    @login_required(owner_only=True)
    def team_view():
        users = db.list_users(active_only=False)
        user_list = []
        t = today()
        for u in users:
            props = db.list_properties(u["id"])
            sh_listings = db.list_sahibinden_listings(consultant=u["name"], status="all")
            user_list.append(dict(u, prop_count=len(props), listing_count=len(sh_listings),
                                  soon=sum(1 for p in props if 0 <= texts.days_left(p, t) <= 30),
                                  rent=sum(p["rent"] for p in props)))
        return render_template("team.html", users=user_list, active_users=[u for u in users if u["active"]])

    @app.post("/ekip/ekle")
    @login_required(owner_only=True)
    def team_add():
        name = request.form.get("name", "").strip()
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        role = request.form.get("role", "agent").strip()
        if role not in ("agent", "owner"):
            role = "agent"
        can_see_all = 1 if request.form.get("can_see_all") else 0

        try:
            if not name:
                raise ValueError("İsim alanı boş bırakılamaz.")
            u_clean = validate_username(username)
            other = db.get_user_by_username(u_clean)
            if other and other["name"] != name:
                raise ValueError(f"'{u_clean}' kullanıcı adı başka bir kişiye ait.")
            p_clean = validate_password(password)
            pwd_hash = generate_password_hash(p_clean)
            uid = db.create_or_update_agent(name, u_clean, pwd_hash, role, can_see_all)
            db.log(
                g.user["id"],
                "kullanici_ekledi",
                uid,
                f"{name} ({u_clean}) - Yetki: {'Tüm portföy' if can_see_all else 'Sadece kendi'}",
            )
            flash(f"'{name}' ({u_clean}) başarıyla kaydedildi.", "ok")
        except ValueError as e:
            flash(str(e), "err")
        return redirect(url_for("team_view"))

    @app.post("/ekip/<int:uid>/yetki")
    @login_required(owner_only=True)
    def team_update_perm(uid: int):
        target = db.get_user(uid)
        if not target:
            abort(404)
        can_see_all = 1 if request.form.get("can_see_all") in ("1", "on", "true") else 0
        role = request.form.get("role", target["role"])
        if role not in ("agent", "owner"):
            role = target["role"]
        if target["id"] == g.user["id"] and role != "owner":
            flash("Kendi yönetici yetkinizi kaldıramazsınız.", "err")
            return redirect(url_for("team_view"))
        db.update_user_permission(uid, can_see_all, role)
        db.log(g.user["id"], "yetki_guncelledi", uid, f"Rol: {role}, Tümünü Görme: {can_see_all}")
        flash(f"'{target['name']}' yetkileri güncellendi.", "ok")
        return redirect(url_for("team_view"))

    @app.post("/ekip/<int:uid>/sifre")
    @login_required(owner_only=True)
    def team_reset_password(uid: int):
        target = db.get_user(uid)
        if not target:
            abort(404)
        new_pwd = request.form.get("new_password", "")
        try:
            p_clean = validate_password(new_pwd)
            db.set_credentials(uid, None, generate_password_hash(p_clean))
            db.log(g.user["id"], "sifre_sifirladi", uid, f"Kullanıcı: {target['name']}")
            flash(f"'{target['name']}' şifresi başarıyla güncellendi.", "ok")
        except ValueError as e:
            flash(str(e), "err")
        return redirect(url_for("team_view"))

    @app.post("/ekip/<int:uid>/durum")
    @login_required(owner_only=True)
    def team_toggle_status(uid: int):
        target = db.get_user(uid)
        if not target:
            abort(404)
        if target["id"] == g.user["id"]:
            flash("Kendi hesabınızı pasife alamazsınız.", "err")
            return redirect(url_for("team_view"))
        new_status = 0 if target["active"] else 1
        db.set_user_active(uid, new_status)
        db.log(g.user["id"], "kullanici_durum_degisti", uid, f"Yeni durum: {new_status}")
        flash(f"'{target['name']}' hesabı {'aktife' if new_status else 'pasife'} alındı.", "ok")
        return redirect(url_for("team_view"))

    @app.post("/ekip/<int:uid>/devret")
    @login_required(owner_only=True)
    def team_transfer_properties(uid: int):
        target = db.get_user(uid)
        if not target:
            abort(404)
        if target["id"] == g.user["id"]:
            flash("Kendi portföyünüzü bu ekrandan devredemezsiniz.", "err")
            return redirect(url_for("team_view"))
        to_user = request.form.get("to_user", type=int) or g.user["id"]
        to_u = db.get_user(to_user)
        if not to_u or not to_u["active"] or to_u["id"] == uid:
            flash("Devralacak geçerli bir kullanıcı seçin.", "err")
            return redirect(url_for("team_view"))
        count = db.transfer_properties(uid, to_user)
        db.deactivate_user(uid)
        db.log(g.user["id"], "mulk_devretti", uid, f"{count} mülk -> {to_u['name']}")
        flash(f"'{target['name']}' üzerindeki {count} mülk '{to_u['name']}' kullanıcısına devredildi ve kullanıcı pasife alındı.", "ok")
        return redirect(url_for("team_view"))

    # ------------------------------------------------------------ toplu giriş (sadece Diren Doğan)
    @app.route("/toplu-giris", methods=["GET", "POST"])
    @login_required(owner_only=True)
    def bulk_import():
        if request.method == "POST":
            mode = request.form.get("mode", "paste")
            items = []
            if mode == "paste":
                raw_text = request.form.get("paste_data", "").strip()
                if not raw_text:
                    flash("Lütfen yapıştırılacak veri girin.", "err")
                    return redirect(url_for("bulk_import"))
                items = parse_tabular_input(raw_text)
            elif mode == "file":
                uploaded_file = request.files.get("csv_file")
                if not uploaded_file or not uploaded_file.filename:
                    flash("Lütfen bir CSV dosyası seçin.", "err")
                    return redirect(url_for("bulk_import"))
                raw = uploaded_file.read()
                try:
                    content = raw.decode("utf-8-sig")
                except UnicodeDecodeError:
                    content = raw.decode("cp1254", errors="replace")
                items = parse_tabular_input(content)
            elif mode == "quick_add":
                title = request.form.get("title", "").strip()
                rent = request.form.get("rent", "").strip()
                renewal_date = request.form.get("renewal_date", "").strip()
                if not title or not rent or not renewal_date:
                    flash("Daire başlığı, kira tutarı ve yenileme tarihi zorunludur.", "err")
                    return redirect(url_for("bulk_import") + "#quick")
                items = [{
                    "title": title,
                    "rent": rent,
                    "renewal_date": renewal_date,
                    "lease_start_date": request.form.get("lease_start_date", "").strip(),
                    "tenant_name": request.form.get("tenant_name", "").strip(),
                    "tenant_phone": request.form.get("tenant_phone", "").strip(),
                    "landlord_name": request.form.get("landlord_name", "").strip(),
                    "landlord_phone": request.form.get("landlord_phone", "").strip(),
                    "consultant": request.form.get("consultant", "").strip(),
                    "notes": request.form.get("notes", "").strip(),
                }]

            if not items:
                flash("İçe aktarılacak satır bulunamadı. Lütfen formatı veya sütunları kontrol edin.", "err")
                return redirect(url_for("bulk_import"))

            assigned_agent_id = request.form.get("agent_id", type=int) or g.user["id"]
            skip_duplicates = request.form.get("allow_duplicates") != "1"
            success_count, errors = db.bulk_add_properties(
                items, default_agent_id=assigned_agent_id, skip_duplicates=skip_duplicates)
            if success_count > 0:
                db.log(g.user["id"], "toplu_giris", None, f"{success_count} adet mülk toplu eklendi.")
                notify_owners(f"📥 {texts.esc(g.user['name'])}, {success_count} adet kiralık mülkü sisteme toplu aktardı.")
                flash(f"🎉 Tebrikler! {success_count} adet kiralık portföy kaydı başarıyla oluşturuldu.", "ok")
            if errors:
                shown = Markup("<br>• ").join(escape(e) for e in errors[:10])
                more = Markup(f"<br>...ve {len(errors) - 10} satır daha.") if len(errors) > 10 else ""
                flash(Markup(f"⚠️ {len(errors)} satırda uyarı/hata var:<br>• ") + shown + more, "err")

            return redirect(url_for("bulk_import") + ("#quick" if mode == "quick_add" else ""))

        # GET
        agents = db.list_users(active_only=True)
        total_props = len(db.list_properties())
        return render_template("bulk_import.html", agents=agents, total_props=total_props)

    @app.get("/toplu-giris/sablon-indir")
    @login_required(owner_only=True)
    def download_bulk_template():
        csv_content = (
            "﻿"
            "Daire / Başlık,Kira Tutarı,Yenileme Tarihi,Kiracı Adı,Kiracı Telefon,Ev Sahibi,Ev Sahibi Telefon,Danışman,Notlar,Sözleşme Başlangıç\n"
            "Yahya Kaptan 3+1 Daire,32000,15.11.2024,Ahmet Yılmaz,05321112233,Mehmet Demir,05423334455,Diren Doğan,1 yıllık standart kontrat,15.11.2021\n"
            "İzmit Merkez 2+1 Ara Kat,24000,01.12.2024,Ayşe Kaya,05051234567,Ali Veli,05559876543,Diren Doğan,Kira her ayın 1'i,01.12.2019\n"
            "Kartepe Müstakil Villa,75000,20.01.2025,Can Özkan,05339998877,Hasan Çelik,05301119988,Diren Doğan,Yıllık peşin ödendi,20.01.2016\n"
        )
        return Response(
            csv_content,
            mimetype="text/csv; charset=utf-8",
            headers={"Content-Disposition": "attachment; filename=gold_nicomedia_kira_sablon.csv"},
        )

    # ------------------------------------------------------------ sahibinden ilanları
    @app.get("/ilanlar")
    @login_required()
    def listings_view():
        can_view_all = ctx.can_view_all(g.user)
        if can_view_all:
            consultant = request.args.get("danisman") or None
        else:
            consultant = g.user["name"]

        listing_type = request.args.get("tur") or None
        search = request.args.get("q") or None
        status = request.args.get("durum") or "active"
        sort = request.args.get("sirala") or "akilli"

        items = db.list_sahibinden_listings(
            consultant=consultant,
            listing_type=listing_type,
            search=search,
            status=status,
        )
        sorters = {
            "fiyat_artan": lambda it: (it["price"] or 0),
            "fiyat_azalan": lambda it: -(it["price"] or 0),
            "yeni": lambda it: -(it["id"] or 0),
        }
        if sort in sorters:
            items = sorted(items, key=sorters[sort])
        stats = db.get_sahibinden_stats(consultant=consultant if not can_view_all else None)
        last_sync = db.get_setting("last_auto_sync")
        badges: dict[int, list[tuple[str, str]]] = {}
        for hook in app.extensions.get("gn_listing_badges", []):
            try:
                for lid, badge in hook(items).items():
                    badges.setdefault(lid, []).append(badge)
            except Exception:  # noqa: BLE001
                log.exception("İlan rozetleri hesaplanamadı")
        return render_template(
            "listings.html",
            items=items,
            stats=stats,
            selected_consultant=consultant,
            selected_type=listing_type,
            selected_status=status,
            selected_sort=sort,
            search_query=search or "",
            last_auto_sync=last_sync,
            can_view_all=can_view_all,
            today_str=today().isoformat(),
            next_year_str=R.add_year(today()).isoformat(),
            consultants=[u for u in db.list_users() if u["name"]],
            badges=badges,
        )

    @app.post("/ilanlar/sync")
    @login_required()
    def listings_sync():
        action = request.form.get("action", "html")
        if action == "seed":
            res = seed_initial_listings(db, force=True)
            flash(
                f"İlanlar güncellendi: {res['total']} ilan ({res['created']} yeni, {res['updated']} güncel, {res['republished']} 30 günlük yenilenen).",
                "ok",
            )
        elif action == "auto":
            from .scraper import run_auto_sync
            res = run_auto_sync(db)
            flash(
                f"Otomatik senkronizasyon çalıştırıldı ({res.get('processed', 0)} ilan güncellendi).",
                "ok",
            )
        else:
            payload = request.form.get("content", "").strip()
            c_name = request.form.get("consultant_name", "Gold Nicomedia").strip()
            c_phone = request.form.get("consultant_phone", "0 (532) 060 28 02").strip()
            if not payload:
                flash("İçerik boş olamaz. Lütfen HTML veya JSON yapıştırın.", "err")
                return redirect(url_for("listings_view"))
            res = sync_listings_data(
                {"html": payload, "consultant": c_name, "phone": c_phone}, db
            )
            flash(
                f"Senkronizasyon tamamlandı: {res['processed']} ilan işlendi "
                f"({res['created']} yeni, {res['updated']} güncellendi, {res['republished']} 30 günlük yenilenen ilan tespit edildi).",
                "ok",
            )
        return redirect(url_for("listings_view"))

    @app.post("/api/sahibinden/sync")
    @login_required()
    def api_sync():
        data = request.get_json(silent=True) or request.form.to_dict()
        res = sync_listings_data(data, db)
        return {"status": "ok", "stats": res}

    @app.post("/ilanlar/<int:lid>/takibe-al")
    @login_required()
    def listing_track(lid: int):
        item = db.get_sahibinden_listing(lid)
        if not item:
            abort(404)
        if item["property_id"]:
            flash(f"Bu ilan zaten kira takibinde (#{item['property_id']}).", "ok")
            return redirect(url_for("property_view", pid=item["property_id"]))

        # 1 yıl sonrasını varsayılan yenileme tarihi yap
        renewal = R.add_year(today()).isoformat()
        agent_id = item["consultant_id"] if item["consultant_id"] else g.user["id"]
        title = item["title"]
        rent = float(item["price"] or 0.0)

        prop_id = db.add_property(
            agent_id=agent_id,
            title=title,
            rent=rent,
            renewal_date=renewal,
            notes=(
                f"Sahibinden ilanı #{item['classified_id']} üzerinden aktarıldı. "
                f"Danışman: {item['consultant_name']} ({item['consultant_phone']}). "
                f"Konum: {item['location']}"
            ),
            status="active",
        )
        db.link_sahibinden_to_property(lid, prop_id)
        db.log(g.user["id"], "sahibinden_kira_takibine_aldi", prop_id, title)
        notify_owners(
            f"📋 {texts.esc(g.user['name'])} (web), Sahibinden ilanını kira takibine aldı: "
            f"<b>{texts.esc(title)}</b> (#{prop_id}) — Kira: {R.money(rent)}"
        )
        flash(
            f"'{title}' başarıyla kira takibine alındı! Buradan kira artış tarihi ve planlanan zam oranını belirleyebilirsiniz.",
            "ok",
        )
        return redirect(url_for("property_view", pid=prop_id))

    @app.post("/ilanlar/<int:lid>/kiralandi")
    @login_required()
    def listing_mark_rented(lid: int):
        item = db.get_sahibinden_listing(lid)
        if not item:
            abort(404)
        try:
            rent_val = parse_money_field(request.form.get("rent"), item["price"] or None)
            start_date = parse_date_field(request.form.get("start_date"), today())
            renewal_date = parse_date_field(
                request.form.get("renewal_date"), R.add_year(date.fromisoformat(start_date)))
        except ValueError as e:
            flash(f"Kiralama kaydedilemedi: {e}", "err")
            return redirect(url_for("listings_view"))
        tenant_name = request.form.get("tenant_name", "").strip()
        tenant_phone = request.form.get("tenant_phone", "").strip()
        landlord_name = request.form.get("landlord_name", "").strip()
        landlord_phone = request.form.get("landlord_phone", "").strip()
        notes = request.form.get("notes", "").strip()
        agent_id = item["consultant_id"] if item["consultant_id"] else g.user["id"]

        prop_id = db.mark_listing_rented(
            lid=lid,
            agent_id=agent_id,
            title=item["title"],
            rent=rent_val,
            start_date=start_date,
            renewal_date=renewal_date,
            tenant_name=tenant_name,
            tenant_phone=tenant_phone,
            landlord_name=landlord_name,
            landlord_phone=landlord_phone,
            notes=notes,
        )
        db.log(g.user["id"], "ilan_kiralandi", prop_id, f"Kira: {rent_val} TL, Kiracı: {tenant_name}")
        notify_owners(
            f"🎉 {texts.esc(g.user['name'])} (web), #{lid} nolu ilanı kiraladı: "
            f"<b>{texts.esc(item['title'])}</b> — Kira: {R.money(rent_val)}, Kiracı: {texts.esc(tenant_name)}. "
            f"Gelecek seneki artış tarihi: {R.fmt_date(renewal_date)}"
        )
        flash(
            f"🎉 Tebrikler! '{item['title']}' kiralandı olarak kaydedildi ve kira artış takvimine (# {prop_id}) eklendi.",
            "ok",
        )
        return redirect(url_for("property_view", pid=prop_id))

    @app.post("/ilanlar/<int:lid>/satildi")
    @login_required()
    def listing_mark_sold(lid: int):
        item = db.get_sahibinden_listing(lid)
        if not item:
            abort(404)
        try:
            price = parse_money_field(request.form.get("price"), item["price"] or None)
            deal_date = parse_date_field(request.form.get("deal_date"), today())
        except ValueError as e:
            flash(f"Satış kaydedilemedi: {e}", "err")
            return redirect(url_for("listings_view"))
        buyer = request.form.get("buyer_name", "").strip()
        notes = request.form.get("notes", "").strip()
        db.mark_listing_sold(lid, price=price, deal_date=deal_date, buyer_name=buyer,
                             buyer_phone=request.form.get("buyer_phone", "").strip(), notes=notes)
        db.log(g.user["id"], "ilan_satildi", lid, f"{R.money(price)} — Alıcı: {buyer or '-'}")
        notify_owners(f"🏆 {texts.esc(g.user['name'])} (web) satış kapattı: <b>{texts.esc(item['title'])}</b> — "
                      f"{R.money(price)}")
        flash(f"🏆 Tebrikler! '{item['title']}' satıldı olarak kaydedildi.", "ok")
        return redirect(url_for("listings_view", durum="sold"))

    @app.post("/ilanlar/<int:lid>/kapat")
    @login_required()
    def listing_close(lid: int):
        item = db.get_sahibinden_listing(lid)
        if not item:
            abort(404)
        action = request.form.get("action", "archive")  # archive or delete
        if action not in ("archive", "delete"):
            action = "archive"
        if action == "delete" and g.user["role"] != "owner":
            flash("İlanı kalıcı silme yetkisi sadece ofis sahibindedir; ilan arşive alındı.", "err")
            action = "archive"
        reason = request.form.get("reason", "").strip()
        db.close_or_delete_listing(lid, action=action, reason=reason)
        db.log(g.user["id"], f"ilan_{action}", lid, f"Sebep: {reason}")
        if action == "delete":
            flash("İlan veritabanından kalıcı olarak silindi.", "ok")
        else:
            flash(f"İlan yayından kaldırıldı (Sebep: {reason or 'Belirtilmedi'}).", "ok")
        return redirect(url_for("listings_view"))

    @app.post("/mulk/<int:pid>/kiraci-cikti")
    @login_required()
    def property_kiraci_cikti(pid: int):
        p = get_prop(pid)
        try:
            exit_date = parse_date_field(request.form.get("exit_date"), today())
        except ValueError as e:
            flash(str(e), "err")
            return redirect(url_for("property_view", pid=pid))
        deposit_notes = request.form.get("deposit_notes", "").strip()
        handover_notes = request.form.get("handover_notes", "").strip()
        next_step = request.form.get("next_step", "vacant")

        db.mark_property_vacant(
            pid=pid,
            exit_date=exit_date,
            deposit_notes=deposit_notes,
            handover_notes=handover_notes,
            next_step=next_step,
        )
        db.log(g.user["id"], "kiraci_cikti", pid, f"Tahliye: {exit_date}, Depozito: {deposit_notes}")
        notify_owners(
            f"🚪 {texts.esc(g.user['name'])} (web), #{pid} <b>{texts.esc(p['title'])}</b> mülkünden kiracının çıktığını işledi."
        )
        for hook in app.extensions.get("gn_on_vacant", []):
            try:
                hook(pid)
            except Exception:  # noqa: BLE001
                log.exception("Boşalan mülk kancası çalışmadı (#%s)", pid)
        flash("Kiracı çıkışı kaydedildi. Mülk durumu güncellendi.", "ok")
        if next_step != "vacant":
            return redirect(url_for("dashboard"))
        return redirect(url_for("property_view", pid=pid))

    @app.post("/mulk/<int:pid>/yeni-kiraci")
    @login_required()
    def property_new_tenant(pid: int):
        p = get_prop(pid)
        tenant_name = request.form.get("tenant_name", "").strip()
        tenant_phone = request.form.get("tenant_phone", "").strip()
        try:
            if not tenant_name:
                raise ValueError("Kiracı adı gerekli.")
            rent_val = parse_money_field(request.form.get("rent"), p["rent"] or None)
            start_date = parse_date_field(request.form.get("start_date"), today())
            renewal_date = parse_date_field(
                request.form.get("renewal_date"), R.add_year(date.fromisoformat(start_date)))
        except ValueError as e:
            flash(f"Yeni kiracı kaydedilemedi: {e}", "err")
            return redirect(url_for("property_view", pid=pid))
        notes = request.form.get("notes", "").strip()

        db.reassign_tenant(
            pid=pid,
            tenant_name=tenant_name,
            tenant_phone=tenant_phone,
            rent=rent_val,
            start_date=start_date,
            renewal_date=renewal_date,
            notes=notes,
        )
        db.log(g.user["id"], "yeni_kiraci_girdi", pid, f"Kiracı: {tenant_name}, Kira: {rent_val} TL")
        notify_owners(
            f"🤝 {texts.esc(g.user['name'])} (web), #{pid} <b>{texts.esc(p['title'])}</b> için yeni kiracı kaydetti: "
            f"{texts.esc(tenant_name)} (Kira: {R.money(rent_val)})."
        )
        flash("Yeni kiracı başarıyla kaydedildi! Kira artış takvimi güncellendi.", "ok")
        return redirect(url_for("property_view", pid=pid))

    # ------------------------------------------------------------ modüller
    from . import web_pwa
    web_pwa.register(app, ctx)

    return app
