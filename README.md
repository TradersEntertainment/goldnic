# Emlak Portföy Asistanı (Telegram botu + web paneli)

Emlakçıların portföyünü tek yerde toplar; kira yenileme zamanı geldiğinde "geçen sene % kaç arttı, bu sene % kaç artacak" bilgisini **hem emlakçıya hem ofis sahibine** otomatik gönderir. Emlakçı ayrılırsa portföy kaybolmaz, tek komutla devredilir. Aynı veriye Telegram'dan ya da **kullanıcı adı + şifre ile web panelinden** erişilir.

---
## 1) Telegram botunu oluştur
1. Telegram'da `@BotFather` → `/newbot` → isim ve kullanıcı adı ver → **token**'ı kopyala.
2. `@userinfobot`'a `/start` yaz → sana **sayısal Telegram ID**'ni verir (ofis sahibi ID'si).

## 2) Railway'e kurulum
1. [railway.com](https://railway.com) → **New Project → Deploy from GitHub repo** → bu repoyu (`TradersEntertainment/goldnic`) seç.
2. Servise tıkla → **Settings → Volumes → + New Volume**, mount path: `/data`
   (Veritabanı SQLite dosyasıdır; volume olmazsa her deploy'da **tüm veri silinir**.)
3. **Variables** sekmesine şunları ekle:

| Değişken | Değer |
|---|---|
| `TELEGRAM_BOT_TOKEN` | BotFather'dan aldığın token |
| `OWNER_TELEGRAM_IDS` | Senin Telegram ID'n (birden fazlaysa virgülle) |
| `DB_PATH` | `/data/emlak.db` |
| `SECRET_KEY` | Uzun rastgele metin (`python -c "import secrets;print(secrets.token_hex(32))"`) |
| `WEB_URL` | Adım 4'te alacağın adres, örn. `https://goldnic-production.up.railway.app` |
| `TIMEZONE` | `Europe/Istanbul` (isteğe bağlı) |

4. **Settings → Networking → Generate Domain** ile herkese açık adres al, `WEB_URL`'ye yaz.
5. Deploy bitince **Deployments → Logs**'ta "Web paneli port ... çalışıyor" görmelisin.

> Sadece **tek** replika çalıştırın (Telegram botu aynı anda iki yerde çalışamaz).

## 3) Kullanıcılar nasıl giriş yapar
1. Ofis sahibi bota `/start` yazar → otomatik sahip olarak kaydolur.
2. `/davet` → kod üretir. Emlakçı bota `/katil KOD` yazar.
3. Herkes (sahip dahil) bota `/sifre kullaniciadi` yazar → bot **kullanıcı adı + rastgele şifre** verir.
4. `WEB_URL` adresinden giriş yapılır; şifre "Hesabım" sayfasından değiştirilir.
5. Şifre unutulursa bota tekrar `/sifre` yazılır (yeni şifre üretir).

Emlakçı ayrılınca `/cikar ID` → portföyü devredilir **ve web girişi anında kapanır**.

## Web paneli
- **Emlakçı:** sadece kendi portföyünü görür/yönetir. **Ofis sahibi:** herkesi, ekip tablosunu, aktivite geçmişini görür.
- Mülk ekleme, bu seneki zam oranını girme, yenileme, arşivleme. Emlakçıların işlemleri ofis sahibine Telegram'dan bildirilir.

## Güvenlik
Şifreler hash'lenir (werkzeug), CSRF koruması, HttpOnly + Secure + SameSite çerez, 8 saatlik oturum, 10 dakikada 5 hatalı denemede kilit, güvenlik başlıkları (CSP, HSTS, X-Frame-Options). Davet kodu tek kullanımlıktır. `.env` ve `*.db` git'e girmez.

## Bot nasıl çalışır
- Her gün 09:00'da; yenilemeye **30 / 15 / 7 / 1 / 0 gün** kala (ve gecikmişse) mesaj gider: geçen yılki artış, bu yıl girilen artış (girilmemişse uyarı), TÜFE sınırı. "✅ Bildirildi" butonu ile emlakçı onaylar.
- Ofis sahibi sorun varsa her gün, her Pazartesi tam rapor alır (gecikenler, zam girilmeyenler, onaylanmayanlar, pasif emlakçılar, emlakçı bazlı özet).
- Komutlar için `/yardim`.

## Yerelde çalıştırma
```powershell
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
copy .env.example .env     # doldur
.\.venv\Scripts\python -m emlak_bot.main    # web: http://localhost:8000
.\.venv\Scripts\python -m pytest
```

## Notlar
- **TÜFE:** Konut kira artışı yasal olarak TÜFE 12 aylık ortalamasını aşamaz. Güncel oranı sahip `/tufe 33.5` ile girer; bot aşan zamları uyarır. Değeri TÜİK'ten doğrulayın.
- **WhatsApp:** `emlak_bot/messenger.py` içindeki `Messenger` arayüzünü uygulayan `WhatsAppMessenger` yazılacak; `users.channel` alanı hazır.
- **Yedek:** Railway volume'daki `emlak.db` dosyasını düzenli yedekleyin (`/csv` komutu da portföyü Excel'e verir).
