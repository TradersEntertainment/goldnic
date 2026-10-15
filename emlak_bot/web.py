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


def parse_tabular_input(raw_text: str) -> list[dict]:
    """Excel'den kopyalanan (tab-separated) veya CSV formatındaki metni mülk sözlüklerine çevirir."""
    import csv
    import io

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
        resp.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "img-src 'self' data: https://*.shbdn.com https://*.sahibinden.com https:; "
            "style-src 'self' 'unsafe-inline'; "
            "form-action 'self'; "
            "frame-ancestors 'none'"
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
        if p is None or p["status"] not in ("active", "vacant"):
            abort(404)
        can_view_all = g.user["role"] == "owner" or bool(dict(g.user).get("can_see_all"))
        if not can_view_all and p["agent_id"] != g.user["id"]:
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
        can_view_all = owner or bool(dict(g.user).get("can_see_all"))
        only = request.args.get("agent", type=int) if can_view_all else g.user["id"]
        props = db.list_properties(only)
        vacant_props = db.list_properties(only, status="vacant")
        names = {u["id"]: u["name"] for u in db.list_users(active_only=False)}
        t = today()

        from .whatsapp import build_tenant_message, get_whatsapp_chat_url

        rows = []
        for p in props:
            days = texts.days_left(p, t)
            agent_name = names.get(p["agent_id"], "?")
            is_acked = db.is_reminder_acked(p["id"], p["renewal_date"])
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
            "rent": sum(r["rent"] for r in rows),
            "vacant": len(vacant_props),
        }
        team, inactive = [], []
        if owner:
            for u in db.list_users():
                mine = db.list_properties(u["id"])
                team.append(dict(u, count=len(mine), soon=sum(
                    1 for p in mine if 0 <= texts.days_left(p, t) <= 30)))
            inactive = collect_issues(db, t, cfg.inactivity_days)["inactive"]
        return render_template(
            "dashboard.html",
            rows=rows,
            stats=stats,
            team=team,
            inactive=inactive,
            owner=owner,
            can_view_all=can_view_all,
            only=only,
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
        )

    @app.post("/mulk/<int:pid>/aradim")
    @login_required()
    def property_aradim(pid: int):
        p = get_prop(pid)
        notes = request.form.get("notes", "").strip() or "Müşteri arandı / görüşüldü."
        db.ack_reminders(pid, p["renewal_date"], notes=notes)
        db.log(g.user["id"], "aradim", pid, f"{p['renewal_date']} dönemi için arandı. Not: {notes}")
        flash("📞 Müşteri arandı olarak kaydedildi. Bu yenileme dönemi için bildirim takibi tamamlandı.", "ok")
        next_url = request.form.get("next") or request.referrer or url_for("property_view", pid=pid)
        return redirect(next_url)

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

    # ------------------------------------------------------------ ekip / kullanıcı yönetimi
    @app.get("/ekip")
    @login_required(owner_only=True)
    def team_view():
        users = db.list_users(active_only=False)
        user_list = []
        for u in users:
            props = db.list_properties(u["id"])
            sh_listings = db.list_sahibinden_listings(consultant=u["name"], status="all")
            user_list.append(dict(u, prop_count=len(props), listing_count=len(sh_listings)))
        return render_template("team.html", users=user_list)

    @app.post("/ekip/ekle")
    @login_required(owner_only=True)
    def team_add():
        name = request.form.get("name", "").strip()
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        role = request.form.get("role", "agent").strip()
        can_see_all = 1 if request.form.get("can_see_all") else 0

        try:
            if not name:
                raise ValueError("İsim alanı boş bırakılamaz.")
            u_clean = validate_username(username)
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
        can_see_all = 1 if request.form.get("can_see_all") else 0
        role = request.form.get("role", target["role"])
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
        to_user = request.form.get("to_user", type=int) or g.user["id"]
        count = db.transfer_properties(uid, to_user)
        db.deactivate_user(uid)
        to_u = db.get_user(to_user)
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
                try:
                    content = uploaded_file.read().decode("utf-8-sig")
                except UnicodeDecodeError:
                    uploaded_file.seek(0)
                    content = uploaded_file.read().decode("cp1254", errors="replace")
                items = parse_tabular_input(content)
            elif mode == "quick_add":
                title = request.form.get("title", "").strip()
                rent = request.form.get("rent", "").strip()
                renewal_date = request.form.get("renewal_date", "").strip()
                if not title or not rent or not renewal_date:
                    flash("Daire başlığı, kira tutarı ve yenileme tarihi zorunludur.", "err")
                    return redirect(url_for("bulk_import"))
                items = [{
                    "title": title,
                    "rent": rent,
                    "renewal_date": renewal_date,
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
            success_count, errors = db.bulk_add_properties(items, default_agent_id=assigned_agent_id)
            if success_count > 0:
                db.log(g.user["id"], "toplu_giris", None, f"{success_count} adet mülk toplu eklendi.")
                notify_owners(f"📥 {texts.esc(g.user['name'])}, {success_count} adet kiralık mülkü sisteme toplu aktardı.")
                flash(f"🎉 Tebrikler! {success_count} adet kiralık portföy kaydı başarıyla oluşturuldu.", "ok")
            if errors:
                err_preview = "<br>• ".join(errors[:10])
                if len(errors) > 10:
                    err_preview += f"<br>...ve {len(errors) - 10} hata daha."
                flash(f"⚠️ {len(errors)} satırda hata oluştu:<br>• {err_preview}", "err")

            return redirect(url_for("bulk_import"))

        # GET
        agents = db.list_users(active_only=True)
        total_props = len(db.list_properties())
        return render_template("bulk_import.html", agents=agents, total_props=total_props)

    @app.get("/toplu-giris/sablon-indir")
    @login_required(owner_only=True)
    def download_bulk_template():
        from flask import Response
        csv_content = (
            "\ufeff"
            "Daire / Başlık,Kira Tutarı,Yenileme Tarihi,Kiracı Adı,Kiracı Telefon,Ev Sahibi,Ev Sahibi Telefon,Danışman,Notlar\n"
            "Yahya Kaptan 3+1 Daire,32000,15.11.2024,Ahmet Yılmaz,05321112233,Mehmet Demir,05423334455,Diren Doğan,1 yıllık standart kontrat\n"
            "İzmit Merkez 2+1 Ara Kat,24000,01.12.2024,Ayşe Kaya,05051234567,Ali Veli,05559876543,Diren Doğan,Kira her ayın 1'i\n"
            "Kartepe Müstakil Villa,75000,20.01.2025,Can Özkan,05339998877,Hasan Çelik,05301119988,Diren Doğan,Yıllık peşin ödendi\n"
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
        can_view_all = g.user["role"] == "owner" or bool(dict(g.user).get("can_see_all"))
        if can_view_all:
            consultant = request.args.get("danisman") or None
        else:
            consultant = g.user["name"]

        listing_type = request.args.get("tur") or None
        search = request.args.get("q") or None
        status = request.args.get("durum") or "active"

        items = db.list_sahibinden_listings(
            consultant=consultant,
            listing_type=listing_type,
            search=search,
            status=status,
        )
        stats = db.get_sahibinden_stats(consultant=consultant if not can_view_all else None)
        last_sync = db.get_setting("last_auto_sync")
        return render_template(
            "listings.html",
            items=items,
            stats=stats,
            selected_consultant=consultant,
            selected_type=listing_type,
            selected_status=status,
            search_query=search or "",
            last_auto_sync=last_sync,
            can_view_all=can_view_all,
            today_str=today().isoformat(),
            next_year_str=(today() + timedelta(days=365)).isoformat(),
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

    @app.post("/ilanlar/<int:lid>/kiralandi")
    @login_required()
    def listing_mark_rented(lid: int):
        item = db.get_sahibinden_listing(lid)
        if not item:
            abort(404)
        rent_val = float(request.form.get("rent") or item["price"] or 0)
        start_date = request.form.get("start_date", "").strip() or today().isoformat()
        renewal_date = request.form.get("renewal_date", "").strip() or (today() + timedelta(days=365)).isoformat()
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
            f"<b>{texts.esc(item['title'])}</b> — Kira: {R.money(rent_val)}, Kiracı: {tenant_name}. "
            f"Gelecek seneki artış tarihi: {renewal_date}"
        )
        flash(
            f"🎉 Tebrikler! '{item['title']}' kiralandı olarak kaydedildi ve kira artış takvimine (# {prop_id}) eklendi.",
            "ok",
        )
        return redirect(url_for("property_view", pid=prop_id))

    @app.post("/ilanlar/<int:lid>/kapat")
    @login_required()
    def listing_close(lid: int):
        item = db.get_sahibinden_listing(lid)
        if not item:
            abort(404)
        action = request.form.get("action", "archive")  # archive or delete
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
        exit_date = request.form.get("exit_date", "").strip() or today().isoformat()
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
        flash("Kiracı çıkışı kaydedildi. Mülk durumu güncellendi.", "ok")
        return redirect(url_for("property_view", pid=pid))

    @app.post("/mulk/<int:pid>/yeni-kiraci")
    @login_required()
    def property_new_tenant(pid: int):
        p = get_prop(pid)
        tenant_name = request.form.get("tenant_name", "").strip()
        tenant_phone = request.form.get("tenant_phone", "").strip()
        rent_val = float(request.form.get("rent", 0) or p["rent"] or 0)
        start_date = request.form.get("start_date", "").strip() or today().isoformat()
        renewal_date = request.form.get("renewal_date", "").strip() or (today() + timedelta(days=365)).isoformat()
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
            f"{tenant_name} (Kira: {R.money(rent_val)})."
        )
        flash("Yeni kiracı başarıyla kaydedildi! Kira artış takvimi güncellendi.", "ok")
        return redirect(url_for("property_view", pid=pid))

    return app
