"""Müşteri Talep Havuzu (Matching CRM) ve Performans Panosu web modülü."""
from __future__ import annotations

import copy
import re
from datetime import date

from flask import Flask, abort, flash, g, jsonify, redirect, render_template, request, url_for
from markupsafe import Markup

from . import agenda, crm, performance
from . import features as F
from . import matching as M
from . import rent as R
from .agenda import AgendaItem
from .search import Group, Hit, phone_matches, text_score
from .whatsapp import clean_phone_for_whatsapp, get_whatsapp_chat_url

ROOM_OPTIONS = ["1+0", "1+1", "2+1", "3+1", "3+2", "4+1", "5+1"]
POPULAR_PLACES = ["Yahya Kaptan", "Alikahya", "Kabaoğlu", "Tüysüzler", "Bağçeşme", "Topçular", "Kent Konut",
                  "Tuana Evleri", "Umuttepe", "Kartepe", "Başiskele", "Körfez", "Derince", "Gölcük"]
CRM_ENDPOINTS = ["talepler", "talep_new", "talep_view", "listing_clients", "property_clients"]


def money_loose(raw: str | None) -> float | None:
    """'30 bin', '3,5 milyon', '45.000' gibi tutarları okur; boşsa None."""
    raw = (raw or "").strip()
    if not raw:
        return None
    t = M.fold(raw).replace("₺", "").replace("tl", "").strip()
    m = re.fullmatch(rf"({M.NUM})\s*{M.UNIT}?", t)
    if m:
        v = M._amount(m.group(1), m.group(2))
        if v:
            return v
    return R.parse_amount(raw)


def places_from_text(raw: str | None) -> tuple[set[str], set[str], list[str]]:
    """'Yahya Kaptan, Alikahya, Kartepe' → (mahalleler, ilçeler, tanınmayanlar)."""
    hoods, districts, unknown = set(), set(), []
    for token in re.split(r"[,;/\n]+|\s+(?:veya|ya da|ve)\s+", raw or ""):
        n = M.norm(token)
        if not n:
            continue
        if n in F.DISTRICTS:
            districts.add(n)
            continue
        found = {h.replace("yahyakaptan", "yahya kaptan") for h in F.hoods_of(n)}
        dist = {k for k in F.DISTRICTS if re.search(rf"\b{k}\b", n)}
        if found or dist:
            hoods |= found
            districts |= dist
        else:
            unknown.append(token.strip())
    return hoods, districts, unknown


def places_text(c: M.Criteria) -> str:
    parts = [F.hood_label(h) for h in sorted(c.hoods)]
    parts += [F.DISTRICTS.get(d, d) for d in sorted(c.districts - {F.NEIGHBORHOODS.get(h) for h in c.hoods})]
    return ", ".join(parts)


def criteria_from_form(f, base: M.Criteria | None = None, structured: bool = False) -> tuple[M.Criteria, list[str]]:
    """Form alanlarından ölçüt üretir.

    structured=False (yeni talep): sadece doldurulan alanlar metinden çıkarılanı ezer.
    structured=True (düzenleme): form tek doğruluk kaynağıdır; boş bırakılan alan ölçütü kaldırır.
    """
    c = copy.deepcopy(base) if base else M.Criteria()
    warnings: list[str] = []
    if f.get("deal_type") in M.DEAL_LABELS:
        c.deal_type = f["deal_type"]
    kinds = set(f.getlist("kinds")) & set(F.KIND_LABELS)
    if kinds or structured:
        c.kinds = kinds
        c.category = M.category_of(kinds) if kinds else (f.get("category") if f.get("category") in M.CATEGORY_LABELS
                                                         else c.category)
    rooms = {r for r in f.getlist("rooms") if re.fullmatch(r"[1-9]\+[0-9]", r)}
    for extra in re.findall(r"[1-9]\s*\+\s*[0-9]", f.get("rooms_extra", "")):
        rooms.add(extra.replace(" ", ""))
    if rooms or structured:
        c.rooms = rooms
    rmin = f.get("rooms_min", "")
    if rmin.isdigit() or structured:
        c.rooms_min = int(rmin) if rmin.isdigit() and int(rmin) > 0 else None
    for key in ("budget_min", "budget_max"):
        raw = f.get(key, "")
        if raw.strip() or structured:
            try:
                setattr(c, key, money_loose(raw))
            except ValueError:
                warnings.append(f"Bütçe okunamadı: {raw}")
    if c.budget_min and c.budget_max and c.budget_min > c.budget_max:
        c.budget_min, c.budget_max = c.budget_max, c.budget_min
    if f.get("places", "").strip() or structured:
        hoods, districts, unknown = places_from_text(f.get("places", ""))
        c.hoods, c.districts = hoods, districts
        if unknown:
            warnings.append("Tanınmayan bölge: " + ", ".join(unknown) + " (not olarak saklandı, eşleştirmede kullanılmaz)")
    amen = set(f.getlist("amenities")) & set(F.AMENITY_LABELS)
    if amen or structured:
        c.amenities = amen
    if f.get("unfurnished"):
        c.avoid = {"esyali"}
        c.amenities.discard("esyali")
    elif structured:
        c.avoid = set()
    if f.get("urgency") in M.URGENCY_LABELS:
        c.urgency = f["urgency"]
    return c, warnings


MULTI_FIELDS = ("kinds", "rooms", "amenities")


def form_values(f) -> dict:
    """Hatalı gönderimde formu kullanıcının girdiğiyle yeniden doldurmak için."""
    out = {k: f.get(k, "") for k in f.keys() if k not in MULTI_FIELDS}
    out.update({k: set(f.getlist(k)) for k in MULTI_FIELDS})
    out["unfurnished"] = bool(f.get("unfurnished"))
    return out


def register(app: Flask, ctx) -> None:
    db, cfg = ctx.db, ctx.cfg
    login_required = ctx.login_required
    crm.install(db, cfg)
    app.jinja_env.filters.update(
        hood=F.hood_label, amenity=lambda a: F.AMENITY_LABELS.get(a, a),
        days1=lambda x: (f"{x:.1f}".rstrip("0").rstrip(".").replace(".", ",")) if x is not None else "—")

    def scope(user=None):
        return ctx.scope_agent(user or g.user)

    ctx.add_nav("talepler", "target", "Talep Havuzu", endpoints=CRM_ENDPOINTS,
                count=lambda u: crm.new_match_count(db, ctx.scope_agent(u)), count_cls="gold")
    ctx.add_nav("performans", "crown", "Performans")
    ctx.agent_last = {"endpoint": "talepler", "icon": "target", "short": "Talepler", "endpoints": CRM_ENDPOINTS}
    ctx.add_quick("talep_new", "target", "Müşteri Talebi", "Aradığı daireyi yaz; portföyle anında eşleşsin")
    ctx.add_action("Yeni müşteri talebi ekle", "talep_new", "target",
                   "talep musteri ariyor kiralik satilik esles yeni ekle crm")
    ctx.add_action("Müşteri talep havuzu", "talepler", "target", "talep havuzu musteriler eslesme crm")
    ctx.add_action("Performans & liderlik panosu", "performans", "crown",
                   "performans liderlik ciro komisyon danisman siralama pano")

    def get_demand(did: int):
        d = crm.get_demand(db, did)
        if d is None:
            abort(404)
        if not ctx.can_view_all(g.user) and d["agent_id"] != g.user["id"]:
            abort(403)
        return d

    def agents():
        return [u for u in db.list_users() if u["password_hash"] or u["channel"] == "telegram"]

    def form_context(**kw):
        return dict(kinds=F.KIND_LABELS, amenity_labels=F.AMENITY_LABELS, room_options=ROOM_OPTIONS,
                    popular=POPULAR_PLACES, sources=crm.SOURCES, urgency_labels=M.URGENCY_LABELS,
                    deal_labels=M.DEAL_LABELS, category_labels=M.CATEGORY_LABELS,
                    agents=agents() if g.user["role"] == "owner" else [], **kw)

    def demand_wa(d) -> str:
        if not d["phone"]:
            return ""
        agent = db.get_user(d["agent_id"]) if d["agent_id"] else None
        msg = (f"Merhaba {d['name']}, {cfg.brand_name}'dan {agent['name'] if agent else ''}. "
               f"Aradığınız {M.summary(M.Criteria.from_row(d)).lower()} için portföyümüzü takip ediyorum; "
               f"uygun bir seçenek çıktığında hemen haber vereceğim.")
        return get_whatsapp_chat_url(d["phone"], msg)

    # ------------------------------------------------------------ talep listesi
    @app.get("/talepler")
    @login_required()
    def talepler():
        status = request.args.get("durum", "active")
        status = status if status in crm.STATUSES or status == "all" else "active"
        sc = scope()
        only = request.args.get("danisman", type=int) if sc is None else None
        rows = crm.list_demands(db, scope_agent=only if only else sc, status=status, q=request.args.get("q", ""))
        for r in rows:
            r["crit"] = M.Criteria.from_row(r)
            r["chips"] = M.chips(r["crit"])
            r["wa_url"] = demand_wa(r)
        counts = {s: len(crm.list_demands(db, scope_agent=only if only else sc, status=s)) for s in crm.STATUSES}
        return render_template("talepler.html", rows=rows, status=status, counts=counts,
                               stats=crm.stats(db, only if only else sc, ctx.today()), statuses=crm.STATUSES,
                               q=request.args.get("q", ""), agent_filter=only, filter_agents=agents() if sc is None else [],
                               today=ctx.today(), **form_context())

    @app.post("/talepler/eslestir")
    @login_required(owner_only=True)
    def talepler_rematch():
        new = crm.refresh_and_notify(db, cfg, ctx.today())
        flash(f"Tüm talepler portföyle yeniden eşleştirildi: {len(new)} yeni eşleşme.", "ok")
        return redirect(url_for("talepler"))

    # ------------------------------------------------------------ yeni talep
    @app.route("/talepler/yeni", methods=["GET", "POST"])
    @login_required()
    def talep_new():
        f = request.form
        if request.method == "POST":
            text = f.get("request", "").strip()
            parsed = M.parse(text)
            crit, warnings = criteria_from_form(f, parsed.criteria)
            name = f.get("name", "").strip() or parsed.name
            phone = f.get("phone", "").strip() or parsed.phone
            try:
                if not name:
                    raise ValueError("Müşteri adını yazın (ör. \"Mehmet Bey 0532 …\" ya da Ad alanı).")
                if phone and len(clean_phone_for_whatsapp(phone)) < 12:
                    raise ValueError("Telefon numarası eksik görünüyor (ör. 0532 111 22 33).")
                if not text and crit.is_vague():
                    raise ValueError("Müşterinin ne aradığını yazın: oda, bütçe, bölge…")
                agent_id = g.user["id"]
                if g.user["role"] == "owner" and f.get("agent_id", "").isdigit():
                    agent_id = int(f["agent_id"])
                move = f.get("move_date", "").strip() or None
                did = crm.create_demand(db, name=name, phone=phone, criteria=crit, notes=text,
                                        source=f.get("source", ""), agent_id=agent_id, created_by=g.user["id"],
                                        move_date=move)
            except ValueError as e:
                flash(str(e), "err")
                return render_template("talep_form.html", values=form_values(f), **form_context()), 400
            new = crm.refresh(db, ctx.today(), demand_ids=[did], silent=True)
            for w in warnings:
                flash(w, "err")
            strong = sum(1 for nm in new if nm.result.strong)
            if new:
                flash(Markup(f"Talep kaydedildi. <b>{len(new)} uygun seçenek</b> bulundu"
                             + (f", {strong} tanesi tam uyumlu" if strong else "") + " — tek tıkla WhatsApp'tan gönderin."),
                      "ok")
            else:
                flash("Talep kaydedildi. Şu an uygun ilan yok; yeni ilan veya boşalan daire geldiğinde size anında "
                      "bildirilecek.", "ok")
            return redirect(url_for("talep_view", did=did))
        return render_template("talep_form.html", values={"request": request.args.get("q", "")}, **form_context())

    @app.get("/api/talepler/ayristir")
    @login_required()
    def api_talep_parse():
        p = M.parse(request.args.get("q", "")[:600])
        return jsonify({"name": p.name, "phone": p.phone, "found": p.found,
                        "chips": [{"icon": i, "text": t, "cls": c} for i, t, c in M.chips(p.criteria)],
                        "vague": p.criteria.is_vague()})

    # ------------------------------------------------------------ talep detayı
    @app.get("/talep/<int:did>")
    @login_required()
    def talep_view(did: int):
        d = get_demand(did)
        crit = M.Criteria.from_row(d)
        today = ctx.today()
        matches = crm.matches_for(db, d, today=today, brand=cfg.brand_name, include_inactive=True)
        live = [m for m in matches if m["active"] and m["status"] not in ("dismissed", "rejected")]
        closed = [m for m in matches if m not in live]
        return render_template(
            "talep.html", d=d, crit=crit, chips=M.chips(crit), live=live, closed=closed,
            notes=crm.notes_for(db, did), statuses=crm.STATUSES, match_statuses=crm.MATCH_STATUSES,
            wa_url=demand_wa(d), places=places_text(crit), budget=M.budget_label(crit), today=today,
            values={"name": d["name"], "phone": d["phone"] or "", "source": d["source"] or "",
                    "move_date": d["move_date"] or "", "agent_id": d["agent_id"], "request": d["notes"] or "",
                    "deal_type": crit.deal_type, "kinds": crit.kinds, "rooms": crit.rooms,
                    "rooms_min": crit.rooms_min or "", "budget_min": R.money(crit.budget_min).replace(" ₺", "")
                    if crit.budget_min else "", "budget_max": R.money(crit.budget_max).replace(" ₺", "")
                    if crit.budget_max else "", "places": places_text(crit), "amenities": crit.amenities,
                    "unfurnished": "esyali" in crit.avoid, "urgency": crit.urgency, "category": crit.category},
            **form_context())

    @app.post("/talep/<int:did>")
    @login_required()
    def talep_update(did: int):
        d = get_demand(did)
        f = request.form
        if f.get("reparse"):   # ölçütleri güncellenen metinden baştan çıkar
            crit, warnings = M.parse(f.get("request", "")).criteria, []
        else:
            crit, warnings = criteria_from_form(f, M.Criteria.from_row(d), structured=True)
        try:
            phone = f.get("phone", "").strip()
            if phone and len(clean_phone_for_whatsapp(phone)) < 12:
                raise ValueError("Telefon numarası eksik görünüyor (ör. 0532 111 22 33).")
            fields = {"name": f.get("name", ""), "phone": phone, "notes": f.get("request", ""),
                      "source": f.get("source", ""), "move_date": f.get("move_date", "")}
            if g.user["role"] == "owner" and f.get("agent_id", "").isdigit():
                fields["agent_id"] = int(f["agent_id"])
            crm.update_demand(db, did, g.user["id"], criteria=crit, **fields)
        except ValueError as e:
            flash(str(e), "err")
            return redirect(url_for("talep_view", did=did) + "#duzenle")
        new = crm.refresh(db, ctx.today(), demand_ids=[did], silent=True)
        for w in warnings:
            flash(w, "err")
        flash(f"Talep güncellendi ve yeniden eşleştirildi ({len(new)} yeni seçenek).", "ok")
        return redirect(url_for("talep_view", did=did))

    @app.post("/talep/<int:did>/durum")
    @login_required()
    def talep_status(did: int):
        get_demand(did)
        try:
            crm.set_status(db, did, request.form.get("status", ""), g.user["id"], request.form.get("note", ""))
            st = request.form.get("status")
            if st == "active":
                crm.refresh(db, ctx.today(), demand_ids=[did], silent=True)
            msg = {"won": "🏆 Tebrikler! Talep kazanıldı olarak kapatıldı.", "lost": "Talep kapatıldı.",
                   "paused": "Talep beklemeye alındı; bildirim gönderilmeyecek.",
                   "active": "Talep yeniden aktif; eşleştirme açıldı."}[st]
            flash(msg, "ok")
        except (ValueError, KeyError):
            flash("Geçersiz durum.", "err")
        return redirect(ctx.safe_next(url_for("talep_view", did=did)))

    @app.post("/talep/<int:did>/not")
    @login_required()
    def talep_note(did: int):
        get_demand(did)
        try:
            crm.add_note(db, did, g.user["id"], request.form.get("text", ""))
            db.log(g.user["id"], "talep_not", did, request.form.get("text", "")[:120])
            flash("Görüşme notu kaydedildi.", "ok")
        except ValueError as e:
            flash(str(e), "err")
        return redirect(ctx.safe_next(url_for("talep_view", did=did) + "#notlar"))

    @app.post("/talep/<int:did>/sil")
    @login_required()
    def talep_delete(did: int):
        d = get_demand(did)
        if g.user["role"] != "owner" and d["created_by"] != g.user["id"]:
            abort(403)
        crm.delete_demand(db, did, g.user["id"])
        flash("Talep silindi.", "ok")
        return redirect(url_for("talepler"))

    @app.post("/eslesme/<int:mid>/durum")
    @login_required()
    def match_status(mid: int):
        m = db.one("SELECT * FROM demand_matches WHERE id=?", (mid,))
        if not m:
            abort(404)
        get_demand(m["demand_id"])
        status = request.form.get("status") or request.args.get("status", "")
        viewing = request.form.get("viewing_at", "").strip() or None
        try:
            crm.set_match_status(db, mid, status, g.user["id"], request.form.get("note", ""), viewing)
        except ValueError as e:
            if request.form.get("ajax"):
                return jsonify({"ok": False, "error": str(e)}), 400
            flash(str(e), "err")
            return redirect(ctx.safe_next(url_for("talep_view", did=m["demand_id"])))
        if request.form.get("ajax"):
            return jsonify({"ok": True, "status": status, "label": crm.MATCH_STATUSES[status]})
        flash({"sent": "Gönderildi olarak işaretlendi.", "interested": "Müşteri ilgileniyor — gösterim planlayın.",
               "viewing": "Gösterim kaydedildi.", "rejected": "Not alındı: müşteri beğenmedi.",
               "dismissed": "Eşleşme gizlendi.", "new": "Eşleşme yeniden açıldı."}.get(status, "Kaydedildi."), "ok")
        return redirect(ctx.safe_next(url_for("talep_view", did=m["demand_id"])))

    # ------------------------------------------------------------ ilan / daire için uygun müşteriler
    def target_page(target_type: str, target_id: int):
        today = ctx.today()
        cand = crm.candidate_for(db, target_type, target_id, today)
        if cand is None:
            abort(404)
        if target_type == "property":
            ctx.get_prop(target_id)
        if request.args.get("eslestir"):
            crm.refresh(db, today, target=(target_type, target_id))
        rows = crm.matches_for_target(db, target_type, target_id, today=today, brand=cfg.brand_name,
                                      scope_agent=scope())
        return render_template("target_clients.html", cand=cand, rows=rows, target_type=target_type,
                               target_id=target_id, kind_labels=F.KIND_LABELS, deal_labels=M.DEAL_LABELS)

    @app.get("/ilanlar/<int:lid>/musteriler")
    @login_required()
    def listing_clients(lid: int):
        return target_page("listing", lid)

    @app.get("/mulk/<int:pid>/musteriler")
    @login_required()
    def property_clients(pid: int):
        return target_page("property", pid)

    # ------------------------------------------------------------ performans panosu
    @app.get("/performans")
    @login_required()
    def performans():
        data = performance.leaderboard(db, cfg, ctx.today(), request.args.get("donem", "ay"))
        return render_template("performans.html", periods=performance.PERIODS, owner=g.user["role"] == "owner",
                               sale_pct=cfg.commission_sale_pct, rent_months=cfg.commission_rent_months, **data)

    # ------------------------------------------------------------ kancalar
    def listing_badges(items) -> dict:
        counts = crm.listing_match_counts(db, scope())
        return {it["id"]: (f"🎯 {counts[it['id']]} müşteri", "b-gold", url_for("listing_clients", lid=it["id"]))
                for it in items if counts.get(it["id"])}

    app.extensions.setdefault("gn_listing_badges", []).append(listing_badges)

    def on_vacant(pid: int) -> None:
        crm.refresh_and_notify(db, cfg, ctx.today(), target=("property", pid), background=True)

    app.extensions.setdefault("gn_on_vacant", []).append(on_vacant)

    def crm_panel(p):
        today = ctx.today()
        soon = None
        if p["status"] == "active":
            ev = db.one("SELECT MIN(eviction_date) AS ev FROM eviction_commitments WHERE property_id=?"
                        " AND status='active' AND eviction_date>=?", (p["id"], today.isoformat()))
            soon = ev["ev"] if ev and ev["ev"] else None
            if not soon:
                return None
        rows = crm.matches_for_target(db, "property", p["id"], today=today, brand=cfg.brand_name,
                                      scope_agent=scope())
        html = Markup(render_template("_crm_panel.html", p=p, rows=rows[:5], total=len(rows), soon=soon))
        return (-10, html) if rows else html   # uygun müşteri varsa boş dairede ilk panel

    app.extensions.setdefault("gn_property_panels", []).append(crm_panel)

    # ------------------------------------------------------------ Takip Merkezi
    @agenda.provider("match")
    def match_items(ctx_, user, today: date) -> list[AgendaItem]:
        sc = ctx_.scope_agent(user)
        where, params = ("", []) if sc is None else (" AND d.agent_id=?", [sc])
        rows = ctx_.db.query(
            "SELECT m.* FROM demand_matches m JOIN client_demands d ON d.id=m.demand_id"
            f" WHERE m.active=1 AND m.status='new' AND d.status='active'{where} ORDER BY m.score DESC", params)
        by: dict[int, list] = {}
        for m in rows:
            by.setdefault(m["demand_id"], []).append(m)
        items: list[AgendaItem] = []
        for did, ms in by.items():
            d = crm.get_demand(ctx_.db, did)
            top = crm.matches_for(ctx_.db, d, today=today, brand=ctx_.cfg.brand_name)
            top = [x for x in top if x["status"] == "new"]
            if not top:
                continue
            best = top[0]
            cand = best["cand"]
            url = url_for("talep_view", did=did)
            actions = []
            if best["wa_url"]:
                actions.append({"label": "WhatsApp'tan gönder", "icon": "chat", "style": "wa", "external": True,
                                "url": best["wa_url"],
                                "data": {"wa-send": url_for("match_status", mid=best["id"]) + "?status=sent"}})
            actions.append({"label": "Eşleşmeler", "icon": "target", "url": url})
            tags = [(f"%{best['score']} uyum", "b-gold")]
            if d["urgency"] == "acil":
                tags.insert(0, ("ACİL", "b-err"))
            if not d["phone"]:
                tags.append(("Telefon yok", "b-warn"))
            n = len(top)
            items.append(AgendaItem(
                severity="match", kind="match",
                title=(f"Bu {'ilan' if cand.type == 'listing' else 'daire'} tam {crm.tr_genitive(d['name'])} "
                       f"aradığı kriterde" + (f" (+{n - 1} seçenek)" if n > 1 else "")),
                subtitle=f"{cand.title} · {M.money_short(cand.price)}" + (f" · {cand.location}" if cand.location else ""),
                url=url, icon="target", tags=tags, actions=actions, side=f"%{best['score']}"))
        for fu in crm.followups(ctx_.db, today, sc):
            d = fu["demand"]
            url = url_for("talep_view", did=d["id"])
            actions = [{"label": "Görüştüm", "icon": "phone", "method": "post", "style": "ok",
                        "url": url_for("talep_note", did=d["id"]),
                        "fields": {"text": "Müşteriyle görüşüldü; talep güncel.", "next": url_for("takip")}}]
            if d["phone"]:
                actions.append({"label": "Ara", "icon": "phone", "url": f"tel:{d['phone']}"})
            if fu["kind"] == "urgent":
                items.append(AgendaItem(
                    severity="warning", kind="match", title=f"Acil talep {fu['idle']} gündür aranmadı: {d['name']}",
                    subtitle=M.summary(M.Criteria.from_row(d)), url=url, icon="alert", actions=actions,
                    tags=[("ACİL", "b-err")], side=f"{fu['idle']} gün"))
            else:
                items.append(AgendaItem(
                    severity="info", kind="match", title=f"Talep güncel mi? {d['name']} ile {fu['idle']} gündür "
                                                         f"görüşülmedi", subtitle=M.summary(M.Criteria.from_row(d)),
                    url=url, icon="clock", actions=actions, side=f"{fu['idle']} gün"))
        return items

    # ------------------------------------------------------------ Spotlight
    def demand_source(ctx_, user, q: str, qd: str, today):
        sc = ctx_.scope_agent(user)
        hits = []
        for d in crm.list_demands(ctx_.db, scope_agent=sc, status="all"):
            score = text_score(q, d["name"], d["notes"]) if q else 0
            if qd and phone_matches(qd, d["phone"]):
                score = max(score, 90)
            if score <= 0:
                continue
            crit = M.Criteria.from_row(d)
            badge, cls = ((f"{d['new_count']} yeni", "b-gold") if d["new_count"] else
                          (crm.STATUSES[d["status"]], "") if d["status"] != "active" else ("", ""))
            hits.append(Hit(title=d["name"], subtitle=M.summary(crit) + (f" · {d['phone']}" if d["phone"] else ""),
                            url=f"/talep/{d['id']}", icon="target", score=score * 0.97, badge=badge, badge_class=cls))
        hits.sort(key=lambda h: (-h.score, h.title))
        return Group("Müşteri Talepleri", hits[:6])

    ctx.spotlight_providers.append(demand_source)
