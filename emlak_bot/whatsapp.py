"""WhatsApp entegrasyonu ve bildirim servisi.
Hem 1-tık doğrudan WhatsApp mesajlaşma linkleri (wa.me)
hem de Meta WhatsApp Business Cloud API / Webhook ile otomatik gönderim destekler.
"""
from __future__ import annotations

import logging
import re
import urllib.parse
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from emlak_bot.config import Config

logger = logging.getLogger(__name__)


def clean_phone_for_whatsapp(phone: str | None) -> str:
    """Telefon numarasını uluslararası WhatsApp formatına (örn: 905320602802) getirir."""
    if not phone:
        return ""
    digits = re.sub(r"\D", "", phone)
    if not digits:
        return ""
    if digits.startswith("90") and len(digits) == 12:
        return digits
    if digits.startswith("0") and len(digits) == 11:
        return "9" + digits
    if len(digits) == 10:
        return "90" + digits
    return digits


def get_whatsapp_chat_url(phone: str, message: str) -> str:
    """Doğrudan WhatsApp web/uygulamasında hazır mesajla sohbet açma linki döner."""
    clean = clean_phone_for_whatsapp(phone)
    if not clean:
        return ""
    encoded = urllib.parse.quote(message)
    return f"https://wa.me/{clean}?text={encoded}"


def send_whatsapp_notification(phone: str, message: str, cfg: Any = None) -> bool:
    """
    Arka planda WhatsApp mesajı gönderir.
    Meta Cloud API (WHATSAPP_TOKEN, WHATSAPP_PHONE_NUMBER_ID)
    veya Genel Webhook / API (WHATSAPP_API_URL) üzerinden çalışır.
    """
    import httpx

    target = clean_phone_for_whatsapp(phone)
    if not target:
        logger.warning("WhatsApp bildirimi için geçersiz telefon numarası: %r", phone)
        return False

    if not cfg:
        return False

    # 1. Meta WhatsApp Business Cloud API
    if getattr(cfg, "whatsapp_token", "") and getattr(cfg, "whatsapp_phone_number_id", ""):
        url = f"https://graph.facebook.com/v20.0/{cfg.whatsapp_phone_number_id}/messages"
        headers = {
            "Authorization": f"Bearer {cfg.whatsapp_token}",
            "Content-Type": "application/json",
        }
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": target,
            "type": "text",
            "text": {"preview_url": False, "body": message},
        }
        try:
            r = httpx.post(url, json=payload, headers=headers, timeout=10)
            if r.status_code in (200, 201):
                logger.info("WhatsApp Cloud API mesajı gönderildi: %s", target)
                return True
            logger.warning("WhatsApp Cloud API hatası (%s): %s", r.status_code, r.text)
        except Exception as e:
            logger.warning("WhatsApp Cloud API istek hatası: %s", e)

    # 2. Özel WhatsApp Gateway / Webhook (GreenAPI, Twilio, UltraMsg vb.)
    if getattr(cfg, "whatsapp_api_url", ""):
        url = cfg.whatsapp_api_url
        headers = {"Content-Type": "application/json"}
        if getattr(cfg, "whatsapp_token", ""):
            headers["Authorization"] = f"Bearer {cfg.whatsapp_token}"
        payload = {"phone": target, "to": target, "message": message, "text": message}
        try:
            r = httpx.post(url, json=payload, headers=headers, timeout=10)
            if r.status_code in (200, 201, 202):
                logger.info("WhatsApp Gateway mesajı iletildi: %s", target)
                return True
            logger.warning("WhatsApp Gateway hatası (%s): %s", r.status_code, r.text)
        except Exception as e:
            logger.warning("WhatsApp Gateway istek hatası: %s", e)

    return False


def build_tenant_message(
    title: str,
    renewal_date_str: str,
    days_left: int,
    agent_name: str,
    brand: str = "Gold Nicomedia",
) -> str:
    """Kiracıya gönderilecek profesyonel hatırlatma metni."""
    if days_left > 0:
        vakit = f"{days_left} gün sonra ({renewal_date_str})"
    elif days_left == 0:
        vakit = "bugün"
    else:
        vakit = f"{abs(days_left)} gün önce dolmuştur"

    return (
        f"Sayın Kiracımız,\n\n"
        f"{brand} Gayrimenkul'den {agent_name}. "
        f"İkamet ettiğiniz '{title}' adresindeki kiralık gayrimenkulün yıllık kira sözleşme yenileme dönemi {vakit}.\n\n"
        f"Yeni dönem kira artış oranı ve sözleşme detaylarını değerlendirmek için sizinle görüşmek isteriz.\n\n"
        f"İyi günler dileriz."
    )


def build_agent_escalation_message(
    title: str,
    renewal_date_str: str,
    days_left: int,
    rent: float,
    tenant_name: str = "",
    tenant_phone: str = "",
    brand: str = "Gold Nicomedia",
) -> str:
    """Danışmana ve Diren Doğan'a giden kademeli takip mesajı."""
    urgent = "🚨 ACİL: " if days_left <= 14 else "📅 "
    return (
        f"{urgent}{brand} Kira Artış Uyarısı!\n\n"
        f"🏠 Mülk: {title}\n"
        f"⏳ Kalan Süre: {days_left} gün ({renewal_date_str})\n"
        f"💰 Mevcut Kira: {rent:,.0f} TL\n"
        f"👤 Kiracı: {tenant_name or 'Belirtilmedi'} ({tenant_phone or '-'})\n\n"
        f"⚠️ Müşteri henüz aranmadı! Müşteriyle görüştükten sonra panelden 'Müşteriyi Aradım' butonuna tıklayarak bildirimi kapatabilirsiniz."
    )
