"""Yedekleme & Güvenlik: şifreli gece yedeği paneli ve denetim izi (kim kimi ne zaman aradı)."""
from __future__ import annotations

import re
import time
from datetime import date, datetime

from flask import Flask, abort, flash, g, jsonify, redirect, render_template, request, send_file, url_for

from . import agenda, backup
from .agenda import AgendaItem

NAME_RE = re.compile(r"^gn-yedek-\d{4}-\d{2}-\d{2}-\d{4}\.(gnbak|db\.gz)$")
AUDIT_GROUPS: dict[str, tuple[str, tuple[str, ...]]] = {
    "iletisim": ("Aramalar & WhatsApp", ("telefon_aradi", "whatsapp_acti", "eslesme_sent")),
    "guvenlik": ("Güvenlik", ("giris_basarisiz", "sifre_degistirdi", "sifre_sifirladi", "yetki_guncelledi",
                              "kullanici_durum_degisti", "web_sifre_olusturdu")),
    "veri": ("Veri & yedek", ("disa_aktardi", "yedek_indirdi", "yedek_aldi", "yedek_hatasi", "belge_olusturdu")),
    "giris": ("Girişler", ("web_giris", "giris_basarisiz")),
}
AUDIT_ACTIONS = tuple(dict.fromkeys(a for k, (_, acts) in AUDIT_GROUPS.items() if k != "giris" for a in acts))
BEACON_LIMIT = 120          # kullanıcı başına saatlik denetim kaydı sınırı (sel / kötüye kullanım önlemi)


def mask_phone(digits: str) -> str:
    d = re.sub(r"\D", "", digits or "")
    if d.startswith("90") and len(d) == 12:
        d = "0" + d[2:]
    if len(d) < 7:
        return "***"
    return f"{d[:4]} *** ** {d[-2:]}"


def register(app: Flask, ctx) -> None:
    db, cfg = ctx.db, ctx.cfg
    login_required = ctx.login_required
    beacons: dict[int, list[float]] = {}

    ctx.add_nav("yedek", "shield", "Yedekleme & Güvenlik", owner=True)
    ctx.owner_endpoints = ctx.owner_endpoints + ("yedek",)
    ctx.add_action("Yedekleme & güvenlik — şifreli gece yedeği", "yedek", "shield",
                   "yedek backup sifreli guvenlik geri yukle denetim izi audit", owner=True)

    @app.get("/yedek")
    @login_required(owner_only=True)
    def yedek():
        now = datetime.now(cfg.tz)
        group = request.args.get("iz", "")
        actions = AUDIT_GROUPS[group][1] if group in AUDIT_GROUPS else AUDIT_ACTIONS
        audit = db.query(
            "SELECT a.*, u.name AS user_name FROM activity a JOIN users u ON u.id=a.user_id"
            f" WHERE a.action IN ({','.join('?' for _ in actions)}) ORDER BY a.id DESC LIMIT 80", actions)
        return render_template("yedek.html", h=backup.health(db, cfg, now), files=backup.list_local(cfg),
                               human=backup.human_size, audit=audit, hour=cfg.backup_hour, keep=cfg.backup_keep,
                               labels=backup.TABLE_LABELS, groups=AUDIT_GROUPS, group=group)

    @app.post("/yedek/simdi")
    @login_required(owner_only=True)
    def yedek_now():
        res = backup.run_backup(db, cfg, reason=f"elle ({g.user['name']})")
        if res.ok:
            sent = ", ".join(f"{k}: {v}" for k, v in res.destinations.items())
            flash(f"Yedek alındı: {res.file} ({backup.human_size(res.size)}) — {sent}", "ok")
        else:
            flash(f"Yedek alınamadı: {res.error}", "err")
        return redirect(url_for("yedek"))

    @app.get("/yedek/indir/<name>")
    @login_required(owner_only=True)
    def yedek_download(name: str):
        if not NAME_RE.match(name):
            abort(404)
        folder = backup.backup_dir(cfg).resolve()
        path = (folder / name).resolve()
        if path.parent != folder or not path.is_file():
            abort(404)
        db.log(g.user["id"], "yedek_indirdi", None, name)
        return send_file(path, as_attachment=True, download_name=name, mimetype="application/octet-stream")

    # ------------------------------------------------------------ denetim izi: arama / WhatsApp tıklamaları
    @app.post("/api/iz")
    @login_required()
    def audit_beacon():
        uid = g.user["id"]
        now = time.time()
        recent = [t for t in beacons.get(uid, []) if now - t < 3600]
        if len(recent) >= BEACON_LIMIT:
            return jsonify({"ok": False}), 429
        recent.append(now)
        beacons[uid] = recent
        kind = request.form.get("kind", "")
        if kind not in ("tel", "wa"):
            abort(400)
        page = (request.form.get("page", "") or "")[:80]
        if not page.startswith("/"):
            page = ""
        db.log(uid, "telefon_aradi" if kind == "tel" else "whatsapp_acti", None,
               f"{mask_phone(request.form.get('target', ''))} · {page or '-'}")
        return jsonify({"ok": True})

    # ------------------------------------------------------------ Takip Merkezi (ofis sahibi)
    @agenda.provider("backup")
    def backup_items(ctx_, user, today: date) -> list[AgendaItem]:
        if user["role"] != "owner":
            return []
        now = datetime.now(cfg.tz)
        h = backup.health(ctx_.db, cfg, now)
        url = url_for("yedek")
        act = [{"label": "Yedekleme", "icon": "shield", "url": url}]
        last = h["last"] or {}
        if last and not last.get("ok"):
            return [AgendaItem(severity="critical", kind="system", title="Son yedekleme başarısız oldu",
                               subtitle=f"Hata: {last.get('error', '')[:160]} — veri kaybı riskine karşı elle yedek alın.",
                               url=url, icon="shield", actions=act)]
        if h["hours"] is None:
            started = ctx_.db.get_setting("sched_last_backup")
            if started or ctx_.db.get_setting("backup_last_attempt"):
                return [AgendaItem(severity="critical", kind="system", title="Henüz başarılı bir yedek yok",
                                   subtitle="Yedekleme sayfasından şimdi yedek alın.", url=url, icon="shield",
                                   actions=act)]
            return []
        items = []
        if h["hours"] > 26:
            items.append(AgendaItem(severity="critical", kind="system",
                                    title=f"Son başarılı yedek {int(h['hours'])} saat önce alındı",
                                    subtitle="Gece yedeği çalışmamış görünüyor. Sunucu kayıtlarını kontrol edin ve "
                                             "elle yedek alın.", url=url, icon="shield", actions=act))
        if not h["encrypted"]:
            items.append(AgendaItem(severity="warning", kind="system", title="Yedekler şifrelenemiyor",
                                    subtitle="BACKUP_PASSPHRASE tanımlayın; şifresiz yedek sunucudan dışarı "
                                             "gönderilmez (sadece yerel kopya tutuluyor).",
                                    url=url, icon="lock", actions=act))
        elif not h["targets"] and not h["s3"]:
            items.append(AgendaItem(severity="warning", kind="system", title="Yedekler sadece sunucuda duruyor",
                                    subtitle="Telegram bot token'ı ve OWNER_TELEGRAM_IDS (veya BACKUP_CHAT_ID / S3) "
                                             "tanımlayın; gece yedeği Diren Doğan'a ulaşsın.",
                                    url=url, icon="shield", actions=act))
        return items
