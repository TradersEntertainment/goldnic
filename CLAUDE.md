# CLAUDE.md — Proje rehberi (Gold Nicomedia Emlak Portföy Asistanı)

Bu dosya projeye yeni giren geliştirici/AI için: **neden var, nasıl çalışır, nerede ne var, nelere dikkat edilir.** Kurulum/deploy için `README.md`'ye bak.

## 1) Amaç ve temel mantık
Emlak ofisinde çalışan emlakçı sorumsuzca ayrılınca ofis sahibi portföyden habersiz kalıyordu. Çözüm:
- Her emlakçı portföyünü (mülk, ev sahibi, kiracı, kira, yenileme tarihi, geçen yıl artış %) sisteme girer.
- Kira yenileme dönemi yaklaşınca sistem **otomatik** mesaj atar: *geçen sene kaç arttı, bu sene kaç artacak.* Mesaj hem sorumlu emlakçıya hem ofis sahibine (ve varsa Telegram grubuna) gider.
- Ofis sahibi tüm emlakçıların ne yaptığını tek yerden görür (Telegram + web panel). Emlakçı ayrılırsa portföyü `/cikar` ile başkasına devredilir, web girişi kapanır.
- İki arayüz var: **Telegram botu** (asıl kanal) ve **web paneli** (kullanıcı adı + şifre). İleride **WhatsApp** eklenecek.

Roller: `owner` (ofis sahibi, her şeyi görür/yönetir) ve `agent` (emlakçı, yalnızca kendi mülkleri).

## 2) Mimari (tek süreç)
`python -m emlak_bot.main` tek süreçte şunları çalıştırır:
- **Telegram botu** (python-telegram-bot, long polling) — **ana thread**'de (`app.run_polling()`).
- **Web paneli** (Flask + waitress) — **arka plan thread**'inde (`main._start_web`), `PORT` env'inde dinler.
- **Zamanlayıcı** (PTB JobQueue): her gün `REMINDER_HOUR` (09:00 Europe/Istanbul) + açılıştan 15 sn sonra bir kez (kaçırılan günler için; mükerrer göndermez).
- **Veritabanı:** SQLite, Railway Volume'da `/data/emlak.db` (`/data` varsa otomatik, yoksa `emlak.db`). Her çağrıda yeni bağlantı açılır (`db.Database._conn`), bot ve web thread'i aynı dosyayı güvenle kullanır.

Tek replika çalışmalı (aynı token ile iki bot çalışamaz, SQLite de tek yazıcıyı sever).

## 3) Dosya haritası (`emlak_bot/`)
| Dosya | Sorumluluk |
|---|---|
| `main.py` | Giriş noktası. Config, DB, bot, handler kaydı, job kaydı, web thread. Logda token sızmasın diye `httpx` loglarını susturur. |
| `config.py` | Env değişkenlerini `Config` dataclass'ına okur ve doğrular. |
| `db.py` | SQLite şeması + tüm sorgular (`Database` sınıfı). Eski DB'ler için `__init__`'te basit migration var (`username`, `password_hash`). |
| `rent.py` | **Saf fonksiyonlar:** tarih/tutar/yüzde parse (TR formatları: `45.000`, `%33,5`), `apply_pct`, `pct_change`, `money`, `pick_stage`. Test edilmesi en kolay yer. |
| `texts.py` | Telegram mesaj metinleri (HTML). `property_card`, `reminder_message`, `short_line`. |
| `reminders.py` | Hatırlatma motoru: `run_reminders`, `collect_issues`, `build_owner_report`, `run_owner_digest`, `daily_job`. |
| `messenger.py` | **Kanal soyutlaması** (`Messenger` ABC). `TelegramMessenger` var, `WhatsAppMessenger` iskelet. `send_telegram_sync` web thread'inden bildirim için. |
| `handlers.py` | Tüm Telegram komutları. `@registered(owner_only=...)` dekoratörü yetki kontrolü yapar. `/ekle` ConversationHandler. |
| `services.py` | Bot ve web'in ortak iş kuralı: `set_planned_increase`, `renew`, `create_property`. Hata = `ValueError` (Türkçe mesaj). |
| `web.py` | Flask uygulaması (`create_app`): login, panel, mülk detay/zam/yenile/sil, ekle, aktivite, hesap. Güvenlik katmanı burada. |
| `templates/` | Jinja şablonları (`login.html` bağımsız, diğerleri `base.html`'i genişletir). Harici CSS/JS yok (CSP sıkı). |

`tests/test_core.py` (hesap, hatırlatma, devir, grup, kurulum kodu), `tests/test_web.py` (login, CSRF, kilit, yetki, ekleme). Çalıştır: `python -m pytest -q`.

## 4) Veri modeli (SQLite)
- `users` (id, channel, chat_id, name, role, active, last_active, username, password_hash) — `channel` WhatsApp için hazır (`telegram` | `whatsapp`).
- `properties` (agent_id, title, landlord_*, tenant_*, `rent`, `renewal_date` ISO, `last_increase_pct/amount`, `planned_pct/rent`, notes, `status` active|archived).
- `rent_history` (her yenilemede eski→yeni kira, %).
- `reminder_log` PK (property_id, due_date, stage) → **aynı aşama bir daha gönderilmez**; `acked` onay butonu için.
- `activity` (kim ne yaptı — ofis sahibinin görünürlüğü buradan).
- `invites` (tek kullanımlık davet kodları), `settings` (şu an `tufe`).

## 5) Kritik iş kuralları
- **Hatırlatma aşamaları:** `STAGES = (30,15,7,1,0)`; `pick_stage(days_left)` o güne uygun **en yakın** aşamayı seçer (30 gün sonra → 30, 10 gün sonra → 15). Gecikmişse `-1` (60 güne kadar). `reminder_log` ile tekrar önlenir. Hiçbir alıcıya ulaşmadıysa kayıt atılmaz, ertesi gün tekrar denenir.
- **Alıcılar:** sorumlu emlakçı (aktifse) + tüm owner'lar + `GROUP_CHAT_ID` (varsa).
- **Yenileme** (`services.renew`): `add_history`, kira güncellenir, `renewal_date` +1 yıl, `last_increase_*` doldurulur, `planned_*` sıfırlanır.
- **TÜFE:** Konut kirası artışı yasal olarak TÜFE 12 aylık ortalamasını aşamaz. Oran **kodda sabit değil**; owner `/tufe 33.5` ile girer, `settings.tufe`'de durur. Aşan zamlar uyarılır.
- **Sorun tespiti** (`collect_issues`): geciken yenileme, ≤15 gün kala zam girilmemiş, 2+ gündür onaylanmamış hatırlatma, `INACTIVITY_DAYS` gündür aktif olmayan emlakçı. Owner'a her gün (sorun varsa), Pazartesi tam rapor gider.
- **Ayrılan emlakçı:** `/cikar ID [YENI_ID]` → `transfer_properties` + `deactivate_user`. Web'de her istekte `active` kontrol edildiği için oturumu anında düşer.

## 6) Kimlik ve yetki
- **Telegram:** `OWNER_TELEGRAM_IDS` (sayısal ID, kullanıcı adı DEĞİL) veya `SETUP_CODE` + `/kurulum KOD` ile owner olunur. Emlakçı `/davet` kodu ile `/katil KOD` yapar. Bot yalnızca özel sohbette tam çalışır.
- **Web girişi:** Bota `/sifre kullaniciadi` → kullanıcı adı + rastgele şifre. Şifre hash'lenir (werkzeug). `/sifre` ve `/davet` gruplarda **bilerek** çalışmaz (sızmasın).
- **Web güvenliği:** CSRF belirteci (boş token reddedilir — geçmişte bu bir bug'dı, testi var), HttpOnly+Secure+SameSite çerez, 8 saat oturum, IP başına 10 dk'da 5 hatalı denemede kilit (bellekte tutulur), CSP/HSTS/X-Frame-Options, ProxyFix (Railway arkasında doğru IP). Kullanıcı bulunamasa bile sahte hash ile karşılaştırma yapılır (zamanlama).

## 7) Ortam değişkenleri
`TELEGRAM_BOT_TOKEN` (zorunlu) · `OWNER_TELEGRAM_IDS` **veya** `SETUP_CODE` (en az biri) · `SECRET_KEY` (web oturumu; yoksa her başlatmada rastgele) · `DB_PATH` (varsayılan `/data/emlak.db` varsa) · `WEB_URL` · `GROUP_CHAT_ID` (`-100...`) · `BRAND_NAME` (varsayılan "Gold Nicomedia") · `TIMEZONE` · `REMINDER_HOUR` · `INACTIVITY_DAYS` · `WEB_ENABLED` · `PORT` (Railway verir) · `COOKIE_SECURE`.
`.env.example`'a bak. **Token/şifreleri asla loga veya repoya yazma.**

## 8) Deploy
Railway, GitHub `main` dalından otomatik deploy eder. `railway.json` start komutunu ve `/health` kontrolünü tanımlar; `.python-version` = 3.12. **Volume `/data`'ya bağlı olmalı**, yoksa her deploy'da veri silinir.

## 9) Bilinen eksikler / yapılacaklar
- **WhatsApp:** `WhatsAppMessenger` yalnızca iskelet. Yapılacak: WhatsApp Business Cloud API ile gönderim, `users.channel='whatsapp'` kullanıcıları, şablon mesaj onayı (24 saat pencere kuralı).
- **Mantık tekrarı:** `handlers.py` içindeki `cmd_zam`/`cmd_renew`/`cmd_add` hâlâ kendi mantığını içeriyor; `services.py` şu an yalnızca web tarafından kullanılıyor. Bota da `services`'i kullandırmak iyi bir refactor.
- **Owner çıkarma:** `/cikar` owner'ı çıkarmaz; owner rolünü düşürme/ekleme komutu yok (`SETUP_CODE` ile eklenebilir).
- **Tek ofis:** Çok kiracılı (multi-tenant) değil. Her müşteri için ayrı Railway servisi/volume/bot token gerekir. Çok ofis istenirse `office_id` alanı ve veri ayrımı gerekir.
- **Giriş limiti** bellekte; çok replikada paylaşılmaz (zaten tek replika).
- **Web'de eksik:** mülk düzenleme formu (bot `/duzenle` var), ekip yönetimi (davet/çıkarma yalnızca botta), CSV indirme (yalnızca botta).
- **Postgres:** SQLite yeterli ama ölçek/yedek için Postgres'e geçiş düşünülebilir (`db.py` tek yerde toplandı).
- Demo/tanıtım için örnek veriyle dolu, girişsiz demo panel yok.

## 10) Çalışma kuralları (bu repo için)
- Kullanıcıya dönük metinler **Türkçe**; kod/yorum Türkçe-İngilizce karışık, mevcut stile uy.
- Para/yüzde biçimi `rent.money` / `rent.pct_str` ile (`30.000 ₺`, `%33,5`); kendi formatını yazma.
- Yeni iş kuralı → önce `rent.py`/`services.py`'a, test ekle (`tests/`), sonra bot/web'e bağla.
- Telegram mesajları HTML parse mode; kullanıcı girdisini `texts.esc` ile kaçır.
- Windows'ta geliştirildi (PowerShell); git satır sonu uyarıları (LF→CRLF) zararsız.
- Değişiklikten sonra `python -m pytest -q`, sonra `git push` (Railway otomatik deploy eder).
