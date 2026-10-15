"""PWA kabuğu: manifest, servis çalışanı, çevrimdışı sayfa, Spotlight API ve Takip Merkezi."""
from __future__ import annotations

from flask import Flask, g, jsonify, render_template, request, url_for

from . import agenda, search


def register(app: Flask, ctx) -> None:
    cfg = ctx.cfg
    login_required = ctx.login_required

    # Spotlight'ta görünen komutlar (modüller kendi komutlarını ekler)
    ctx.add_action("Panel — kira takibi", "dashboard", "grid", "ana sayfa dashboard ofis portföy")
    ctx.add_action("Takip Merkezi — bugün yapılacaklar", "takip", "radar", "takip ajanda uyarı hatırlatma gecikmiş")
    ctx.add_action("İlan portföyü (Sahibinden)", "listings_view", "building", "ilan sahibinden kiralık satılık")
    ctx.add_action("Yeni mülk / kiracı ekle", "add", "plus", "ekle yeni mülk daire kiracı kayıt")
    ctx.add_action("Hesabım ve şifre", "account", "user", "hesap şifre profil çıkış")
    ctx.add_action("Toplu portföy girişi (Excel)", "bulk_import", "upload", "toplu excel csv içe aktar yükle", owner=True)
    ctx.add_action("Ekip yönetimi", "team_view", "users", "ekip danışman kullanıcı yetki şifre", owner=True)
    ctx.add_action("Aktivite geçmişi", "activity", "activity", "aktivite log geçmiş denetim", owner=True)
    ctx.add_action("Portföyü Excel'e aktar (CSV)", "export_portfolio_csv", "download", "excel csv dışa aktar indir",
                   owner=True)

    @app.get("/manifest.webmanifest")
    def manifest():
        def icon(path: str, size: str, purpose: str = "any", mime: str = "image/png") -> dict:
            return {"src": url_for("static", filename=path), "sizes": size, "type": mime, "purpose": purpose}

        data = {
            "id": "/",
            "name": f"{cfg.brand_name} OS",
            "short_name": cfg.brand_name,
            "description": "Gayrimenkul işletim sistemi: kira takibi, hukuki radar, sözleşme motoru ve müşteri eşleştirme.",
            "lang": "tr-TR",
            "dir": "ltr",
            "start_url": "/?source=pwa",
            "scope": "/",
            "display": "standalone",
            "display_override": ["standalone", "minimal-ui"],
            "orientation": "any",
            "background_color": "#060A13",
            "theme_color": "#070B14",
            "categories": ["business", "productivity", "finance"],
            "icons": [
                icon("icons/icon-192.png", "192x192"),
                icon("icons/icon-512.png", "512x512"),
                icon("icons/maskable-512.png", "512x512", "maskable"),
                icon("icons/icon.svg", "any", "any", "image/svg+xml"),
            ],
            "shortcuts": [
                {"name": "Takip Merkezi", "short_name": "Takip", "url": "/takip",
                 "icons": [icon("icons/icon-192.png", "192x192")]},
                {"name": "Yeni Mülk", "short_name": "Mülk Ekle", "url": "/ekle",
                 "icons": [icon("icons/icon-192.png", "192x192")]},
                {"name": "İlan Portföyü", "short_name": "İlanlar", "url": "/ilanlar",
                 "icons": [icon("icons/icon-192.png", "192x192")]},
            ] + [
                {"name": s["label"], "short_name": s["label"], "url": url_for(s["endpoint"], **s["params"]),
                 "icons": [icon("icons/icon-192.png", "192x192")]}
                for s in ctx.quick if not s["owner"]
            ][:1],
        }
        resp = jsonify(data)
        resp.mimetype = "application/manifest+json"
        resp.headers["Cache-Control"] = "no-cache"
        return resp

    @app.get("/sw.js")
    def service_worker():
        version = app.config["ASSET_VERSION"]
        precache = [
            url_for("offline"),
            f"{url_for('static', filename='app.css')}?v={version}",
            f"{url_for('static', filename='app.js')}?v={version}",
            url_for("static", filename="icons/icon.svg"),
            f"{url_for('static', filename='icons/icon.svg')}?v={version}",
            url_for("static", filename="icons/icon-192.png"),
            url_for("static", filename="icons/icon-512.png"),
            url_for("static", filename="icons/maskable-512.png"),
        ]
        body = render_template("sw.js", version=version, precache=precache, offline_url=url_for("offline"))
        resp = app.response_class(body, mimetype="application/javascript")
        resp.headers["Service-Worker-Allowed"] = "/"
        resp.headers["Cache-Control"] = "no-cache"
        return resp

    @app.get("/offline")
    def offline():
        return render_template("offline.html")

    @app.get("/api/spotlight")
    @login_required()
    def api_spotlight():
        q = request.args.get("q", "")
        groups = search.spotlight(ctx, g.user, q)
        return jsonify({
            "q": q,
            "groups": [{"title": grp.title, "items": [h.as_dict() for h in grp.hits]} for grp in groups],
        })

    @app.get("/takip")
    @login_required()
    def takip():
        items = agenda.build_cached(ctx, g.user)
        kind = request.args.get("tur") or ""
        kinds: dict[str, int] = {}
        for it in items:
            kinds[it.kind] = kinds.get(it.kind, 0) + 1
        shown = [i for i in items if i.kind == kind] if kind else items
        sections = []
        for sev in ("critical", "warning", "match", "info"):
            group = [i for i in shown if i.severity == sev]
            if group:
                sections.append((sev, agenda.SEVERITY_LABEL[sev], group))
        return render_template("takip.html", sections=sections, total=len(items), kinds=kinds, kind=kind,
                               counts={s: sum(1 for i in items if i.severity == s) for s in agenda.SEVERITY_RANK})
