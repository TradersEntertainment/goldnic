"""Belge Merkezi: tek tıkla Kira Sözleşmesi, Tahliye Taahhütnamesi ve Teslim Tutanağı (PDF)."""
from __future__ import annotations

from datetime import date

from flask import Flask, Response, abort, flash, g, redirect, render_template, request, url_for
from markupsafe import Markup

from . import docstore, legal
from . import documents as D
from . import rent as R
from .messenger import send_telegram_document_sync
from .validators import clean_tc, iban_clean, iban_format, iban_valid, tc_kimlik_valid

TEXT_FIELDS = ("address", "unit_desc", "landlord_name", "landlord_phone", "landlord_address", "tenant_name",
               "tenant_phone", "tenant_address", "guarantor_name", "guarantor_phone", "guarantor_address",
               "usage_purpose", "property_condition", "fixtures", "special_terms")


def parse_details(form, p) -> dict:
    """Belge bilgileri formunu doğrular (T.C. Kimlik No, IBAN, depozito sınırı)."""
    out: dict = {k: (form.get(k) or "").strip() or None for k in TEXT_FIELDS if k in form}
    for col, label in (("landlord_tc", "Ev sahibi"), ("tenant_tc", "Kiracı"), ("guarantor_tc", "Kefil")):
        if col in form:
            raw = clean_tc(form.get(col))
            if raw and not tc_kimlik_valid(raw):
                raise ValueError(f"{label} T.C. Kimlik No geçersiz (kontrol hanesi tutmuyor): {raw}")
            out[col] = raw or None
    if "landlord_iban" in form:
        raw = iban_clean(form.get("landlord_iban"))
        if raw and not iban_valid(raw):
            raise ValueError("IBAN geçersiz (TR ile başlayan 26 karakter ve doğru kontrol basamakları olmalı).")
        out["landlord_iban"] = raw or None
    for col in ("deposit_amount", "guarantor_limit"):
        if col in form:
            raw = (form.get(col) or "").strip()
            out[col] = R.parse_amount(raw) if raw else None
    if "payment_day" in form:
        raw = (form.get("payment_day") or "").strip()
        day = int(raw) if raw else None
        if day is not None and not 1 <= day <= 31:
            raise ValueError("Ödeme günü 1–31 arasında olmalı.")
        out["payment_day"] = day
    if out.get("deposit_amount") and out["deposit_amount"] > 3 * float(p["rent"]) + 0.005:
        raise ValueError(f"Güvence bedeli üç aylık kirayı ({R.money(3 * p['rent'])}) aşamaz (TBK m. 342).")
    return out


def register(app: Flask, ctx) -> None:
    db, cfg = ctx.db, ctx.cfg
    login_required = ctx.login_required
    ctx.add_action("Belge doğrulama (belge no / kod)", "doc_verify", "shield", "belge dogrula kod sozlesme pdf")

    def meta_kwargs(p) -> dict:
        agent = db.get_user(p["agent_id"])
        office = " · ".join(x for x in (cfg.brand_address, cfg.brand_phone) if x)
        return {"brand": cfg.brand_name, "office_line": office, "license_no": cfg.brand_license_no,
                "agent_name": agent["name"] if agent else ""}

    def pdf_response(row, inline: bool = True) -> Response:
        disp = "inline" if inline else "attachment"
        return Response(row["pdf"], mimetype="application/pdf",
                        headers={"Content-Disposition": f'{disp}; filename="{docstore.filename(row)}"',
                                 "Cache-Control": "private, no-store"})

    def parse_day(raw: str | None, default: date) -> date:
        raw = (raw or "").strip()
        return R.parse_date(raw) if raw else default

    # ------------------------------------------------------------ Belge Merkezi
    @app.get("/mulk/<int:pid>/belgeler")
    @login_required()
    def docs_center(pid: int):
        p = ctx.get_prop(pid)
        today = ctx.today()
        return render_template(
            "docs.html", p=p, ready=docstore.readiness(p), docs=docstore.list_for(db, pid), kinds=D.DOC_KINDS,
            today=today, default_eviction=docstore.default_eviction_date(p, today),
            iban_fmt=iban_format(p["landlord_iban"]) if p["landlord_iban"] else "",
            fixtures_default="\n".join(f"{x} | 1 | Sağlam" for x in docstore.lines(p["fixtures"])),
            new_id=request.args.get("yeni", type=int), property_kinds=legal.PROPERTY_KINDS,
        )

    @app.post("/mulk/<int:pid>/belgeler/bilgi")
    @login_required()
    def docs_details(pid: int):
        p = ctx.get_prop(pid)
        try:
            fields = parse_details(request.form, p)
            if fields:
                db.update_property(pid, **fields)
                db.log(g.user["id"], "duzenledi", pid, "belge bilgileri: " + ", ".join(sorted(fields)))
            flash("Belge bilgileri kaydedildi.", "ok")
        except ValueError as e:
            flash(str(e), "err")
        return redirect(url_for("docs_center", pid=pid) + "#bilgiler")

    @app.post("/mulk/<int:pid>/belge/<kind>")
    @login_required()
    def doc_generate(pid: int, kind: str):
        if kind not in D.DOC_KINDS:
            abort(404)
        p = ctx.get_prop(pid)
        today = ctx.today()
        miss = docstore.missing_fields(p, kind)
        if miss:
            flash(f"{D.DOC_KINDS[kind]} için eksik bilgi: {', '.join(miss)}.", "err")
            return redirect(url_for("docs_center", pid=pid) + "#bilgiler")
        f = request.form
        try:
            if kind == "kira_sozlesmesi":
                data = docstore.lease_from_property(
                    p, contract_date=parse_day(f.get("contract_date"), today),
                    start=parse_day(f.get("start_date"), docstore._d(p["lease_start_date"]) or today),
                    term_months=int(f.get("term_months") or p["lease_term_months"] or 12))
                render = lambda meta: D.build_lease_pdf(data, meta)  # noqa: E731
                payload = {"kiraya_veren": data.landlord.name, "kiraci": data.tenant.name, "adres": data.address,
                           "kira": data.rent, "baslangic": data.start, "sure_ay": data.term_months}
            elif kind == "tahliye_taahhutnamesi":
                signed = parse_day(f.get("signed_date"), today)
                eviction = parse_day(f.get("eviction_date"), docstore.default_eviction_date(p, today))
                data = docstore.eviction_from_property(p, signed=signed, eviction=eviction)
                if signed <= data.delivery_date:
                    raise ValueError("Taahhütname kiralananın tesliminden SONRA düzenlenmelidir (TBK m. 352/1). "
                                     f"Teslim tarihi {R.fmt_date(data.delivery_date)}; imza tarihini sonraki bir gün seçin.")
                render = lambda meta: D.build_eviction_pdf(data, meta)  # noqa: E731
                payload = {"kiraci": data.tenant.name, "imza": signed, "tahliye": eviction, "teslim": data.delivery_date}
            else:
                data = D.HandoverData(
                    landlord=docstore.landlord_of(p), tenant=docstore.tenant_of(p), address=p["address"] or "",
                    handover_date=parse_day(f.get("handover_date"), today),
                    direction="iade" if f.get("direction") == "iade" else "teslim", unit_desc=p["unit_desc"] or "",
                    keys=f.get("keys", "").strip(), meter_electric=f.get("meter_electric", "").strip(),
                    meter_water=f.get("meter_water", "").strip(), meter_gas=f.get("meter_gas", "").strip(),
                    condition=f.get("condition", "").strip() or (p["property_condition"] or ""),
                    items=docstore.handover_items(f.get("items") if "items" in f else
                                                  "\n".join(docstore.lines(p["fixtures"]))),
                    notes=f.get("notes", "").strip())
                render = lambda meta: D.build_handover_pdf(data, meta)  # noqa: E731
                payload = {"yon": data.direction, "tarih": data.handover_date, "kalem": len(data.items),
                           "anahtar": data.keys}
            doc = docstore.create(db, kind=kind, property_id=pid, payload=payload, render=render,
                                  user_id=g.user["id"], secret=cfg.secret_key, meta_kwargs=meta_kwargs(p))
        except ValueError as e:
            flash(str(e), "err")
            return redirect(url_for("docs_center", pid=pid))
        if kind == "tahliye_taahhutnamesi" and f.get("add_to_radar", "1") == "1":
            legal.add_commitment(db, g.user["id"], p, data.signed_date, data.eviction_date, data.tenant.name,
                                 f"{doc['doc_no']} ile oluşturuldu")
        if f.get("respond") == "redirect":
            flash(Markup(f"{D.DOC_KINDS[kind]} oluşturuldu: <b>{doc['doc_no']}</b>"), "ok")
            return redirect(url_for("docs_center", pid=pid, yeni=doc["id"]) + "#belgeler")
        return pdf_response(docstore.get(db, doc["id"]))

    # ------------------------------------------------------------ indirme / paylaşım
    def doc_for_user(doc_id: int):
        row = docstore.get(db, doc_id)
        if not row:
            abort(404)
        if row["property_id"]:
            p = db.get_property(row["property_id"])
            if p is None:
                abort(404)
            if not ctx.can_view_all(g.user) and p["agent_id"] != g.user["id"] and row["created_by"] != g.user["id"]:
                abort(403)
        elif g.user["role"] != "owner" and row["created_by"] != g.user["id"]:
            abort(403)
        return row

    @app.get("/belge/<int:doc_id>.pdf")
    @login_required()
    def doc_download(doc_id: int):
        row = doc_for_user(doc_id)
        return pdf_response(row, inline=request.args.get("indir") != "1")

    @app.post("/belge/<int:doc_id>/telegram")
    @login_required()
    def doc_telegram(doc_id: int):
        row = doc_for_user(doc_id)
        if not cfg.bot_token:
            flash("Telegram botu yapılandırılmamış (TELEGRAM_BOT_TOKEN).", "err")
        else:
            target = g.user["chat_id"] if g.user["channel"] == "telegram" else (cfg.chat_id or cfg.group_chat_id)
            if not target:
                flash("Telegram hedefi bulunamadı: hesabınızı bota bağlayın veya CHAT_ID tanımlayın.", "err")
            else:
                ok = send_telegram_document_sync(cfg.bot_token, target, docstore.filename(row), row["pdf"],
                                                 f"📄 {D.DOC_KINDS[row['kind']]} — {row['doc_no']}")
                flash("Belge Telegram'a gönderildi; oradan WhatsApp'a iletebilirsiniz." if ok
                      else "Belge Telegram'a gönderilemedi.", "ok" if ok else "err")
        return redirect(ctx.safe_next(url_for("docs_center", pid=row["property_id"]) if row["property_id"]
                                      else url_for("dashboard")))

    @app.get("/belge/dogrula")
    @login_required()
    def doc_verify():
        q = request.args.get("kod", "").strip()
        row = docstore.find(db, q) if q else None
        prop = db.get_property(row["property_id"]) if row and row["property_id"] else None
        return render_template("doc_verify.html", q=q, row=row, prop=prop, kinds=D.DOC_KINDS)

    # ------------------------------------------------------------ mülk sayfası paneli
    def docs_panel(p):
        return Markup(render_template("_docs_panel.html", p=p, ready=docstore.readiness(p), kinds=D.DOC_KINDS,
                                      docs=docstore.list_for(db, p["id"])[:3]))

    app.extensions.setdefault("gn_property_panels", []).append(docs_panel)

    # ------------------------------------------------------------ Spotlight: belge no / doğrulama kodu
    from .search import Group, Hit

    def doc_source(ctx_, user, q: str, qd: str, today):
        raw = request.args.get("q", "").strip().upper()
        if len(raw) < 4 or not any(ch.isdigit() for ch in raw):
            return Group("Belgeler")
        rows = db.query("SELECT id, doc_no, kind, verify_code, property_id FROM documents"
                        " WHERE doc_no LIKE ? OR verify_code LIKE ? ORDER BY id DESC LIMIT 6",
                        (f"%{raw}%", f"%{raw}%"))
        hits = []
        for r in rows:
            p = db.get_property(r["property_id"]) if r["property_id"] else None
            if p and not ctx_.can_view_all(user) and p["agent_id"] != user["id"]:
                continue
            hits.append(Hit(title=f"{D.DOC_KINDS[r['kind']]} · {r['doc_no']}", subtitle=p["title"] if p else "",
                            url=f"/belge/{r['id']}.pdf", icon="file", score=88, badge=r["verify_code"],
                            badge_class="b-gold"))
        return Group("Belgeler", hits)

    ctx.spotlight_providers.append(doc_source)
