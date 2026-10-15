"""Kanal soyutlaması. Telegram şimdi; WhatsApp için aynı arayüzü uygulayan sınıf yazılacak."""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod

log = logging.getLogger(__name__)

# Buton: [(etiket, callback_verisi), ...] satırlar listesi
Buttons = list[list[tuple[str, str]]]


def send_telegram_sync(token: str, chat_id: str, text: str) -> bool:
    """Web panelden (async olmayan bağlamdan) Telegram'a bildirim gönderir."""
    import httpx

    try:
        r = httpx.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            timeout=8,
        )
        return r.status_code == 200
    except Exception:  # noqa: BLE001
        log.exception("Telegram bildirimi gönderilemedi: %s", chat_id)
        return False


def send_telegram_document_sync(token: str, chat_id: str, filename: str, data: bytes, caption: str = "",
                                silent: bool = False) -> bool:
    """Telegram'a dosya gönderir (sözleşme PDF'i, şifreli yedek). En fazla 50 MB."""
    import httpx

    try:
        r = httpx.post(
            f"https://api.telegram.org/bot{token}/sendDocument",
            data={"chat_id": chat_id, "caption": caption[:1000], "parse_mode": "HTML",
                  "disable_notification": "true" if silent else "false"},
            files={"document": (filename, data, "application/octet-stream")},
            timeout=60,
        )
        if r.status_code != 200:
            log.warning("Telegram belge gönderimi başarısız (%s): %s", r.status_code, r.text[:200])
        return r.status_code == 200
    except Exception:  # noqa: BLE001
        log.exception("Telegram belgesi gönderilemedi: %s", chat_id)
        return False


class Messenger(ABC):
    channel: str = "base"

    @abstractmethod
    async def send(self, chat_id: str, text: str, buttons: Buttons | None = None) -> bool:
        """Mesaj gönderir. Başarılıysa True döner. HTML biçimli metin kabul eder."""


class TelegramMessenger(Messenger):
    channel = "telegram"

    def __init__(self, bot):
        self.bot = bot

    async def send(self, chat_id: str, text: str, buttons: Buttons | None = None) -> bool:
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        markup = None
        if buttons:
            markup = InlineKeyboardMarkup(
                [[InlineKeyboardButton(label, callback_data=data) for label, data in row]
                 for row in buttons]
            )
        try:
            await self.bot.send_message(
                chat_id=int(chat_id), text=text, parse_mode="HTML", reply_markup=markup
            )
            return True
        except Exception:  # noqa: BLE001 - bir kullanıcıya gidememesi diğerlerini durdurmamalı
            log.exception("Telegram mesajı gönderilemedi: %s", chat_id)
            return False


class WhatsAppMessenger(Messenger):
    """İleride: WhatsApp Business Cloud API ile gönderim. users.channel='whatsapp' kayıtları için."""

    channel = "whatsapp"

    async def send(self, chat_id: str, text: str, buttons: Buttons | None = None) -> bool:
        raise NotImplementedError("WhatsApp entegrasyonu henüz yapılmadı")
