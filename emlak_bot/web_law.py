"""Hukuk Radarı web modülü: TÜFE tavanı, tahliye taahhütleri, 5. yıl tespit, 10. yıl tahliye."""
from __future__ import annotations

from datetime import date

from flask import Flask, abort, flash, g, redirect, render_template, request, url_for
from markupsafe import Markup

from . import agenda, legal, tufe
from . import law as L
from . import rent as R
from .agenda import AgendaItem

KIND_ICONS = {"tufe": "percent", "taahhut": "gavel", "tespit5": "trend", "tahliye10": "door", "veri": "file"}


def register(app: Flask, ctx) -> None:
    db, cfg = ctx.db, ctx.cfg
    login_required = ctx.login_required

    def critical_count(user) -> int:
        return sum(1 for _, a in legal.radar(db, ctx.today(), ctx.scope_agent(user)) if a.severity == "critical")

    ctx.add_nav("hukuk", "scale", "Hukuk Radarı", count=critical_count)
    ctx.takip_endpoints = ctx.takip_endpoints + ("hukuk",)
    ctx.add_action("Hukuk radarı — TBK süreleri", "hukuk", "scale",
                   "hukuk tbk tahliye taahhut taahhutname 5 yil 10 yil tespit dava ihtar")
    ctx.add_action("TÜFE kira artış hesaplayıcı", "hukuk", "percent", "tufe kira artis hesapla tavan oran zam",
                   _anchor="hesapla")

    def parse_day(raw: str | None, label: str) -> date:
        try:
            return R.parse_date(raw or "")
        except ValueError as e:
            raise ValueError(f"{label}: {e}") from e

    # ------------------------------------------------------------ Hukuk Radarı
    @app.get("/hukuk")
    @login_required()
    def hukuk():
        today = ctx.today()
        scope = ctx.scope_agent(g.user)
        rows = legal.radar(db, today, scope)
        rates = tufe.load_rates(db)
        latest = L.latest_rate(rates, today)
        history = sorted(rates.values(), key=lambda r: r.period, reverse=True)[:13]
        groups: dict[str, list] = {}
        for p, a in rows:
            groups.setdefault(a.kind, []).append((p, a))
        missing = [p for p, a in groups.pop("veri", [])]

        calc = None
        if request.args.get("kira"):
            try:
                rent_val = R.parse_amount(request.args["kira"])
                renew = parse_day(request.args.get("tarih"), "Yenileme tarihi")
                kind = request.args.get("tur", "konut")
                cap = L.rent_cap(renew, rates, kind if kind in legal.PROPERTY_KINDS else "konut")
                calc = {"rent": rent_val, "renewal": renew, "cap": cap, "max": cap.max_rent(rent_val), "kind": kind}
            except ValueError as e:
                flash(str(e), "err")
        next_period = tufe.expected_latest_period(today)
        return render_template(
            "hukuk.html", groups=groups, missing=missing, latest=latest, history=history, calc=calc,
            counts={k: len(v) for k, v in groups.items()},
            critical=sum(1 for _, a in rows if a.severity == "critical"),
            source_labels=tufe.SOURCE_LABELS, kind_titles=legal.KIND_TITLES, kind_icons=KIND_ICONS,
            period_label=L.period_label, next_label=lambda per: L.period_label(L.shift_period(per, 1)),
            today=today, next_renewal_cap=L.rent_cap(today, rates),
            last_attempt=db.get_setting("tufe_last_attempt"), last_result=db.get_setting("tufe_last_result"),
            missing_since=legal.cpi_missing_since(db, today), expected_period=next_period,
            property_kinds=legal.PROPERTY_KINDS,
        )

    @app.post("/hukuk/tufe/guncelle")
    @login_required(owner_only=True)
    def tufe_refresh():
        res = tufe.refresh(db, ctx.today(), evds_key=cfg.evds_api_key, evds_series=cfg.evds_series)
        db.log(g.user["id"], "tufe_guncelledi", None, res.message)
        flash(("TÜFE güncellendi — " if res.ok else "TÜFE otomatik çekilemedi — ") + res.message,
              "ok" if res.ok else "err")
        return redirect(url_for("hukuk"))

    @app.post("/hukuk/tufe")
    @login_required(owner_only=True)
    def tufe_manual():
        try:
            period = (request.form.get("period") or "").strip()[:7]
            rate = R.parse_pct(request.form.get("rate", ""))
            if not tufe.upsert_rate(db, period, rate, "manual"):
                raise ValueError("Bu ay için resmi kaynaktan alınmış veri var; elle giriş yapılmadı.")
            db.log(g.user["id"], "tufe_guncelledi", None, f"{period}: %{rate} (elle)")
            flash(f"{L.period_label(period)} TÜFE 12 aylık ortalaması {R.pct_str(rate)} olarak kaydedildi.", "ok")
        except ValueError as e:
            flash(str(e), "err")
        return redirect(url_for("hukuk"))

    # ------------------------------------------------------------ mülk hukuki bilgileri
    @app.post("/mulk/<int:pid>/hukuk")
    @login_required()
    def property_legal_update(pid: int):
        p = ctx.get_prop(pid)
        f = request.form
        try:
            fields = {}
            if "lease_start_date" in f:
                raw = f.get("lease_start_date", "").strip()
                fields["lease_start_date"] = parse_day(raw, "Sözleşme başlangıcı").isoformat() if raw else None
            if "delivery_date" in f:
                raw = f.get("delivery_date", "").strip()
                fields["delivery_date"] = parse_day(raw, "Teslim tarihi").isoformat() if raw else None
            if "lease_term_months" in f:
                term = int(f.get("lease_term_months") or 12)
                if not 1 <= term <= 600:
                    raise ValueError("Sözleşme süresi 1–600 ay arasında olmalı.")
                fields["lease_term_months"] = term
            if "property_kind" in f:
                kind = f.get("property_kind")
                fields["property_kind"] = kind if kind in legal.PROPERTY_KINDS else "konut"
            if fields.get("lease_start_date") and fields["lease_start_date"] > ctx.today().isoformat():
                raise ValueError("Sözleşme başlangıç tarihi gelecekte olamaz.")
            db.update_property(pid, **fields)
            db.log(g.user["id"], "duzenledi", pid, "hukuki bilgiler: " + ", ".join(sorted(fields)))
            flash("Sözleşme bilgileri kaydedildi; hukuki radar güncellendi.", "ok")
        except ValueError as e:
            flash(str(e), "err")
        return redirect(ctx.safe_next(url_for("property_view", pid=pid)) + ("" if "next" in f else "#hukuk"))

    @app.post("/mulk/<int:pid>/taahhut")
    @login_required()
    def commitment_add(pid: int):
        p = ctx.get_prop(pid)
        try:
            signed = parse_day(request.form.get("signed_date"), "İmza tarihi")
            eviction = parse_day(request.form.get("eviction_date"), "Tahliye tarihi")
            legal.add_commitment(db, g.user["id"], p, signed, eviction, request.form.get("tenant_name", "").strip(),
                                 request.form.get("notes", "").strip())
            check = L.check_commitment(signed, eviction, ctx.today(),
                                       legal._d(p["delivery_date"]) or legal._d(p["lease_start_date"]))
            if check.issues:
                flash(Markup("Taahhütname kaydedildi, ancak dikkat: ") + check.issues[0], "err")
            else:
                flash("Tahliye taahhüdü takibe alındı. İhtar ve icra süreleri otomatik izlenecek.", "ok")
        except ValueError as e:
            flash(str(e), "err")
        return redirect(url_for("property_view", pid=pid) + "#hukuk")

    @app.post("/taahhut/<int:cid>")
    @login_required()
    def commitment_update(cid: int):
        row = db.one("SELECT * FROM eviction_commitments WHERE id=?", (cid,))
        if not row:
            abort(404)
        ctx.get_prop(row["property_id"])  # yetki kontrolü
        action = request.form.get("action", "")
        try:
            when = parse_day(request.form.get("date") or ctx.today().isoformat(), "Tarih")
            if action == "notice":
                legal.update_commitment(db, g.user["id"], cid, notice_date=when)
                flash("İhtarname kaydedildi: icra/dava süresi 1 yıla uzadı (TBK 353).", "ok")
            elif action in ("executed", "evicted", "waived", "lapsed", "active"):
                legal.update_commitment(db, g.user["id"], cid, status=action,
                                        action_date=when if action != "active" else None,
                                        notes=request.form.get("notes", "").strip() or None)
                flash(f"Taahhüt durumu: {L.COMMITMENT_STATUSES[action]}.", "ok")
            else:
                raise ValueError("Geçersiz işlem.")
        except ValueError as e:
            flash(str(e), "err")
        return redirect(url_for("property_view", pid=row["property_id"]) + "#hukuk")

    @app.post("/mulk/<int:pid>/hukuk/onay")
    @login_required()
    def legal_ack(pid: int):
        ctx.get_prop(pid)
        kind, ref, action = request.form.get("kind", ""), request.form.get("ref", ""), request.form.get("action", "")
        if kind not in legal.ACK_ACTIONS or action not in legal.ACK_ACTIONS[kind] or not ref:
            abort(400, "Geçersiz onay.")
        legal.ack(db, g.user["id"], pid, kind, ref, action, request.form.get("notes", "").strip())
        db.log(g.user["id"], "hukuk_onayladi", pid, f"{legal.KIND_TITLES[kind]}: {legal.ACK_ACTIONS[kind][action]}")
        flash(f"Kaydedildi: {legal.ACK_ACTIONS[kind][action]}. Bu uyarı kapatıldı.", "ok")
        return redirect(ctx.safe_next(url_for("property_view", pid=pid) + "#hukuk"))

    # ------------------------------------------------------------ mülk sayfası paneli
    def law_panel(p):
        today = ctx.today()
        pl = legal.property_legal(db, p, today)
        return Markup(render_template(
            "_law_panel.html", p=p, pl=pl, today=today, kinds=legal.PROPERTY_KINDS, statuses=L.COMMITMENT_STATUSES,
            ack_actions=legal.ACK_ACTIONS, acks=legal.acks_for(db, p["id"]), period_label=L.period_label,
            source_labels=tufe.SOURCE_LABELS, kind_icons=KIND_ICONS))

    app.extensions.setdefault("gn_property_panels", []).append(law_panel)

    # ------------------------------------------------------------ Takip Merkezi
    @agenda.provider("legal")
    def legal_items(ctx_, user, today: date) -> list[AgendaItem]:
        items: list[AgendaItem] = []
        missing = 0
        for p, a in legal.radar(ctx_.db, today, ctx_.scope_agent(user)):
            if a.kind == "veri":
                missing += 1
                continue
            url = url_for("property_view", pid=p["id"]) + "#hukuk"
            items.append(AgendaItem(
                severity=a.severity, kind="legal", title=f"{a.title}: {p['title']}", subtitle=a.detail, url=url,
                icon=KIND_ICONS.get(a.kind, "scale"), due=a.due, tags=[(a.law_ref, "b-gold")],
                actions=[{"label": "Hukuki durum", "icon": "scale", "url": url}], property_id=p["id"],
                side=L.fmt(a.due)))
        if missing:
            items.append(AgendaItem(
                severity="info", kind="legal", title=f"{missing} mülkte sözleşme başlangıç tarihi eksik",
                subtitle="5. yıl kira tespiti ve 10. yıl tahliye hakkı bu mülkler için hesaplanamıyor.",
                url=url_for("hukuk") + "#eksik", icon="file",
                actions=[{"label": "Tamamla", "icon": "edit", "url": url_for("hukuk") + "#eksik"}]))
        since = legal.cpi_missing_since(ctx_.db, today)
        if since and user["role"] == "owner":
            period = tufe.expected_latest_period(today)
            items.append(AgendaItem(
                severity="critical", kind="system", title=f"{L.period_label(period)} TÜFE oranı sisteme girilmedi",
                subtitle=f"TÜİK {L.fmt(since)} tarihinde açıkladı; otomatik çekme başarısız. Kira tavanları eski "
                         f"orana göre hesaplanıyor — oranı elle girin veya yeniden deneyin.",
                url=url_for("hukuk") + "#tufe", icon="percent",
                actions=[{"label": "TÜFE gir", "icon": "edit", "url": url_for("hukuk") + "#tufe"}]))
        return items
