"""Güvenli web paneli (kullanıcı adı + şifre ile giriş)."""
from __future__ import annotations

import hmac
import logging
import re
import secrets
import time
from datetime import date, datetime, timedelta
from functools import wraps

from flask import (
    Flask, abort, flash, g, redirect, render_template, request, session, url_for,
)
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


def validate_username(u: str) -> str:
    u = (u or "").strip().lower()
    if not USERNAME_RE.match(u):
        raise ValueError("Kullanıcı adı 3-32 karakter olmalı (harf, rakam, . _ -).")
    return u


def validate_password(p: str) -> str:
    if len(p or "") < 8:
        raise ValueError("Şifre en az 8 karakter olmalı.")
    return p


def create_app(db: Database, cfg: Config) -> Flask:
    from .scraper import seed_initial_listings, sync_listings_data

    # Başlangıçta kayıtlı ilan yoksa initial_listings.json dosyasını yükle
    try:
        seed_initial_listings(db)
    except Exception as e:
        log.warning("İlk ilan tohumlaması yapılamadı: %s", e)

    app = Flask(__name__)
    app.secret_key = cfg.secret_key
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=cfg.cookie_secure,
        PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
        MAX_CONTENT_LENGTH=16 * 1024 * 1024,
    )
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
    app.jinja_env.filters.update(money=R.money, pct=R.pct_str, tdate=R.fmt_date)
    app.jinja_env.globals["brand"] = cfg.brand_name
    fails: dict[str, list[float]] = {}

    # ------------------------------------------------------------ güvenlik
    def csrf_token() -> str:
        if "csrf" not in session:
            session["csrf"] = secrets.token_urlsafe(24)
        return session["csrf"]

    app.jinja_env.globals["csrf_token"] = csrf_token

    @app.before_request
    def load_user_and_check_csrf():
        g.user = None
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

            sent = request.form.get("csrf", "")
            expected = session.get("csrf", "")
            if not expected or not hmac.compare_digest(sent, expected):
                abort(400, "Geçersiz istek (CSRF). Sayfayı yenileyip tekrar deneyin.")

    @app.after_request
    def headers(resp):
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "same-origin"
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; style-src 'self' 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'"
        )
        resp.headers["Cache-Control"] = "no-store"
        if cfg.cookie_secure:
            resp.headers["Strict-Transport-Security"] = "max-age=31536000"
        return resp

    def login_required(owner_only: bool = False):
        def deco(fn):
            @wraps(fn)
            def wrapper(*a, **kw):
                if g.user is None:
                    return redirect(url_for("login"))
                if owner_only and g.user["role"] != "owner":
                    abort(403)
                db.touch_user(g.user["id"])
                return fn(*a, **kw)
            return wrapper
        return deco

    def too_many(ip: str) -> bool:
        now = time.time()
        fails[ip] = [t for t in fails.get(ip, []) if now - t < WINDOW]
        return len(fails[ip]) >= MAX_FAILS

    def notify_owners(text: str) -> None:
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

    def today() -> date:
        return datetime.now(cfg.tz).date()

    def get_prop(pid: int):
        p = db.get_property(pid)
        if p is None or p["status"] != "active":
            abort(404)
        if g.user["role"] != "owner" and p["agent_id"] != g.user["id"]:
            abort(403)
        return p

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
        return render_template("account.html")

    # ------------------------------------------------------------ paneller
    @app.get("/")
    @login_required()
    def dashboard():
        owner = g.user["role"] == "owner"
        only = request.args.get("agent", type=int) if owner else g.user["id"]
        props = db.list_properties(only)
        names = {u["id"]: u["name"] for u in db.list_users(active_only=False)}
        t = today()
        rows = [dict(p, days=texts.days_left(p, t), agent=names.get(p["agent_id"], "?")) for p in props]
        stats = {
            "total": len(rows),
            "soon": sum(1 for r in rows if 0 <= r["days"] <= 30),
            "overdue": sum(1 for r in rows if r["days"] < 0),
            "missing": sum(1 for r in rows if r["days"] <= 30 and r["planned_rent"] is None),
            "rent": sum(r["rent"] for r in rows),
        }
        team, inactive = [], []
        if owner:
            for u in db.list_users():
                mine = db.list_properties(u["id"])
                team.append(dict(u, count=len(mine), soon=sum(
                    1 for p in mine if 0 <= texts.days_left(p, t) <= 30)))
            inactive = collect_issues(db, t, cfg.inactivity_days)["inactive"]
        return render_template("dashboard.html", rows=rows, stats=stats, team=team,
                               inactive=inactive, owner=owner, only=only)

    @app.get("/mulk/<int:pid>")
    @login_required()
    def property_view(pid: int):
        p = get_prop(pid)
        tufe = get_tufe(db)
        cap = R.apply_pct(p["rent"], tufe) if tufe is not None else None
        agent = db.get_user(p["agent_id"])
        return render_template(
            "property.html", p=p, days=texts.days_left(p, today()), agent=agent,
            hist=db.history(pid), tufe=tufe, cap=cap,
            over_cap=tufe is not None and p["planned_pct"] is not None and p["planned_pct"] > tufe)

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

    @app.get("/aktivite")
    @login_required(owner_only=True)
    def activity():
        uid = request.args.get("user", type=int)
        return render_template("activity.html", rows=db.recent_activity(uid, 100))

    # ------------------------------------------------------------ sahibinden ilanları
    @app.get("/ilanlar")
    @login_required()
    def listings_view():
        consultant = request.args.get("danisman") or None
        listing_type = request.args.get("tur") or None
        search = request.args.get("q") or None

        items = db.list_sahibinden_listings(
            consultant=consultant,
            listing_type=listing_type,
            search=search,
        )
        stats = db.get_sahibinden_stats()
        return render_template(
            "listings.html",
            items=items,
            stats=stats,
            selected_consultant=consultant,
            selected_type=listing_type,
            search_query=search or "",
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
        renewal = (today() + timedelta(days=365)).isoformat()
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

    return app
