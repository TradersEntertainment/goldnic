"""Sahibinden ilan çekme, ayrıştırma ve ilk yükleme (seeding) modülü.
Anti-bot koruması nedeniyle hem arka plan tohumlama (initial_listings.json)
hem de tarayıcı üzerinden 1-tık senkronizasyon (Bookmarklet / HTML yapıştırma) destekler.
"""
from __future__ import annotations

import html
import json
import logging
import re
from pathlib import Path
from typing import Any

from emlak_bot.db import Database

logger = logging.getLogger(__name__)

INITIAL_FILE = Path(__file__).parent / "initial_listings.json"


def clean_price(price_raw: str) -> float:
    """Metin fiyatı ('26.000 TL', '3.350.000 TL') float değere dönüştürür."""
    if not price_raw:
        return 0.0
    clean = (
        price_raw.replace("TL", "")
        .replace("tl", "")
        .replace(".", "")
        .replace(",", ".")
        .replace(" ", "")
        .strip()
    )
    try:
        return float(clean)
    except ValueError:
        return 0.0


def parse_listings_from_html(
    html_text: str,
    default_consultant: str = "Gold Nicomedia",
    default_phone: str = "0 (532) 060 28 02",
) -> list[dict[str, Any]]:
    """Sahibinden mağaza sayfası HTML içeriğinden ilanları çıkartır."""
    if not html_text:
        return []

    # Eğer sayfada danışman adı veya başlığı varsa tespit et
    detected_consultant = default_consultant
    detected_phone = default_phone

    c_match = re.search(r'class="store-user-name"[^>]*>\s*([^<]+)', html_text)
    if c_match:
        detected_consultant = c_match.group(1).strip()

    phone_match = re.search(r'href="tel:([^"]+)"', html_text)
    if phone_match:
        detected_phone = phone_match.group(1).strip()

    # <div class="classified " bloklarını ayır
    parts = html_text.split('<div class="classified "')
    if len(parts) <= 1:
        # Alternatif: tablo görünümü veya tek parça
        parts = re.split(r'<tr[^>]*data-id="(\d+)"', html_text)
        if len(parts) > 1:
            # Table-based fallback
            return _parse_table_view(html_text, detected_consultant, detected_phone)

    items: list[dict[str, Any]] = []
    for p in parts[1:]:
        url_m = re.search(r'data-box-url="([^"]+)"', p)
        if not url_m:
            url_m = re.search(r'href="(https?://[^"]*sahibinden\.com/ilan/[^"]+)"', p)
        if not url_m:
            continue
        url = url_m.group(1)
        if not url.startswith("http"):
            url = f"https://sahibinden.com{url}"

        id_m = re.search(r"-(\d+)/detay", url)
        cid = id_m.group(1) if id_m else ""
        if not cid:
            continue

        img_m = re.search(r'<img[^>]+src="([^"]+)"[^>]*alt="([^"]+)"', p)
        img = img_m.group(1) if img_m else ""
        title_raw = img_m.group(2) if img_m else ""
        title = re.sub(r"\s*#\d+\s*$", "", html.unescape(title_raw)).strip()

        if not title:
            t_m = re.search(r'<p class="title">\s*<a[^>]*>(.*?)</a>', p, re.DOTALL)
            if t_m:
                title = html.unescape(t_m.group(1).strip())

        loc_m = re.search(r'<p class="location">\s*(.*?)\s*</p>', p, re.DOTALL)
        loc = loc_m.group(1).strip() if loc_m else ""

        date_m = re.search(r'<p class="date">\s*(.*?)\s*</p>', p, re.DOTALL)
        dt = date_m.group(1).strip() if date_m else ""

        price_m = re.search(r'<p class="price">\s*(.*?)\s*</p>', p, re.DOTALL)
        price_raw = price_m.group(1).strip() if price_m else ""

        ltype = "kiralik" if "kiralik" in url else ("satilik" if "satilik" in url else "diger")

        items.append(
            {
                "cid": cid,
                "url": url,
                "title": title or f"İlan #{cid}",
                "img": img,
                "location": loc,
                "date": dt,
                "price_raw": price_raw,
                "price": clean_price(price_raw),
                "listing_type": ltype,
                "consultant_name": detected_consultant,
                "consultant_phone": detected_phone,
            }
        )

    return items


def _parse_table_view(html_text: str, consultant: str, phone: str) -> list[dict[str, Any]]:
    """Sahibinden liste/tablo görünümü fallback."""
    items = []
    rows = re.findall(r'<tr[^>]*data-id="(\d+)"[^>]*>(.*?)</tr>', html_text, re.DOTALL)
    for cid, row in rows:
        url_m = re.search(r'href="([^"]+)"', row)
        url = url_m.group(1) if url_m else f"https://sahibinden.com/ilan/{cid}/detay"
        if not url.startswith("http"):
            url = f"https://sahibinden.com{url}"

        title_m = re.search(r'class="classifiedTitle"[^>]*>([^<]+)', row)
        title = title_m.group(1).strip() if title_m else f"İlan #{cid}"

        price_m = re.search(r'class="classified-price"[^>]*>([^<]+)', row)
        price_raw = price_m.group(1).strip() if price_m else ""

        ltype = "kiralik" if "kiralik" in url else ("satilik" if "satilik" in url else "diger")

        items.append(
            {
                "cid": cid,
                "url": url,
                "title": title,
                "img": "",
                "location": "",
                "date": "",
                "price_raw": price_raw,
                "price": clean_price(price_raw),
                "listing_type": ltype,
                "consultant_name": consultant,
                "consultant_phone": phone,
            }
        )
    return items


def seed_initial_listings(db: Database, force: bool = False) -> dict[str, int]:
    """Uygulama açılışında kayıtlı ilan yoksa initial_listings.json dosyasını veritabanına yükler."""
    if not INITIAL_FILE.exists():
        logger.warning(f"Initial listings file not found: {INITIAL_FILE}")
        return {"total": 0, "created": 0, "updated": 0, "republished": 0}

    stats = db.get_sahibinden_stats()
    if stats["total"] > 0 and not force:
        logger.info(f"Sahibinden listings already seeded ({stats['total']} listings exist).")
        return {"total": stats["total"], "created": 0, "updated": 0, "republished": 0}

    try:
        with open(INITIAL_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        logger.error(f"Failed to read initial listings: {e}")
        return {"total": 0, "created": 0, "updated": 0, "republished": 0}

    created = 0
    updated = 0
    republished = 0

    for item in data:
        _, status = db.upsert_sahibinden_listing(item)
        if status == "created":
            created += 1
        elif status == "updated":
            updated += 1
        elif status == "republished":
            republished += 1

    logger.info(
        f"Seeded Sahibinden listings: {len(data)} total ({created} new, {updated} updated, {republished} republished)"
    )
    return {
        "total": len(data),
        "created": created,
        "updated": updated,
        "republished": republished,
    }


def sync_listings_data(raw_payload: Any, db: Database) -> dict[str, int]:
    """
    Kullanıcı arayüzünden veya bookmarklet'ten gelen veriyi (JSON listesi veya HTML metni)
    ayrıştırıp veritabanına aktarır.
    """
    items: list[dict[str, Any]] = []

    if isinstance(raw_payload, list):
        items = raw_payload
    elif isinstance(raw_payload, dict):
        if "items" in raw_payload and isinstance(raw_payload["items"], list):
            items = raw_payload["items"]
        elif "html" in raw_payload:
            items = parse_listings_from_html(
                str(raw_payload["html"]),
                default_consultant=raw_payload.get("consultant", "Gold Nicomedia"),
                default_phone=raw_payload.get("phone", "0 (532) 060 28 02"),
            )
        else:
            items = [raw_payload]
    elif isinstance(raw_payload, str):
        text = raw_payload.strip()
        if text.startswith("[") or text.startswith("{"):
            try:
                parsed = json.loads(text)
                return sync_listings_data(parsed, db)
            except Exception:
                pass
        items = parse_listings_from_html(text)

    created = 0
    updated = 0
    republished = 0

    for it in items:
        if not it.get("cid") and not it.get("classified_id"):
            continue
        _, st = db.upsert_sahibinden_listing(it)
        if st == "created":
            created += 1
        elif st == "updated":
            updated += 1
        elif st == "republished":
            republished += 1

    return {
        "processed": len(items),
        "created": created,
        "updated": updated,
        "republished": republished,
    }


def fetch_live_store_listings(max_pages: int = 4) -> list[dict[str, Any]]:
    """
    Sahibinden mağaza sayfalarından canlı veri çekmeyi dener.
    DataDome bot engeli gelirse sessizce boş döner ve mevcut veriyi korur.
    """
    import urllib.request

    base_url = "https://goldnicomedia.sahibinden.com/"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7",
    }
    all_items: list[dict[str, Any]] = []
    for page in range(1, max_pages + 1):
        url = base_url if page == 1 else f"{base_url}?pagingOffset={(page - 1) * 20}"
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=10) as resp:
                if resp.status == 200:
                    html_content = resp.read().decode("utf-8", errors="replace")
                    items = parse_listings_from_html(html_content)
                    if not items:
                        break
                    all_items.extend(items)
        except Exception as e:
            logger.info(f"Canlı çekme kontrolü (sayfa {page}): {e}")
            break
    return all_items


def run_auto_sync(db: Database) -> dict[str, Any]:
    """Arka planda periyodik olarak çalışan otomatik senkronizasyon."""
    from emlak_bot.db import now_iso

    # 1. Kayıt yoksa ilk seed'i yap
    seed_initial_listings(db)

    # 2. Canlı siteden yeni ilan çekmeyi dene
    live_items = fetch_live_store_listings(max_pages=4)
    res: dict[str, Any] = {"live_fetched": len(live_items), "created": 0, "updated": 0, "republished": 0}
    if live_items:
        res = sync_listings_data(live_items, db)

    db.set_setting("last_auto_sync", now_iso())
    return res


def start_auto_sync_thread(db: Database, interval_seconds: int = 21600) -> None:
    """
    Arka planda ek server maliyeti yaratmayan (0 CPU/RAM masrafı) daemon thread başlatır.
    Varsayılan olarak 6 saatte bir (günde 4 kez) otomatik kontrol eder.
    """
    import threading
    import time

    def _loop():
        # Sunucu açıldıktan 30 saniye sonra ilk kontrol
        time.sleep(30)
        while True:
            try:
                res = run_auto_sync(db)
                logger.info("Otomatik senkronizasyon tamamlandı: %s", res)
            except Exception as e:
                logger.warning("Otomatik senkronizasyon hatası: %s", e)
            time.sleep(interval_seconds)

    t = threading.Thread(target=_loop, name="auto_sync", daemon=True)
    t.start()
    logger.info("Otomatik senkronizasyon thread'i başlatıldı (Aralık: %s saniye)", interval_seconds)
