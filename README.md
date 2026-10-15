# Gold Nicomedia OS — Gayrimenkul İşletim Sistemi

Kocaeli / İzmit'te **Gold Nicomedia** (ofis sahibi: Diren Doğan) için geliştirilen, kira takibinden müşteri
eşleştirmeye kadar ofisin tüm akışını tek yerde toplayan web paneli + Telegram botu.

| Modül | Ne yapar |
|---|---|
| **PWA arayüzü** | Ana ekrana eklenebilir, tam ekran mobil uygulama; alt menü, `Ctrl/⌘ + K` Spotlight araması, Takip Merkezi |
| **Türk kira hukuku** | TÜİK/TCMB'den TÜFE 12 aylık ortalaması (yasal artış tavanı, kuruşu kuruşuna), tahliye taahhüdü radarı, 5. yıl kira tespiti, 10. yıl tahliye hakkı |
| **Resmi belge motoru** | Tek tıkla PDF Kira Sözleşmesi, Tahliye Taahhütnamesi, Teslim/Demirbaş Tutanağı; doğrulama kodu, WhatsApp/Telegram paylaşımı |
| **Müşteri Talep Havuzu** | "3+1 kiralık, 30-35 bin, Yahya Kaptan, acil" cümlesini anlar; Sahibinden ilanları ve boşalan dairelerle otomatik eşleştirir, tek tıkla WhatsApp'tan gönderir; performans panosu |
| **Şifreli gece yedeği** | Her gece AES-256 şifreli veritabanı yedeği Diren Doğan'ın kişisel Telegram'ına (+ isteğe bağlı S3/R2); denetim izi |

---

## Hızlı başlangıç (Railway)
1. [railway.com](https://railway.com) → **New Project → Deploy from GitHub repo** → bu repo.
2. **Settings → Volumes → + New Volume**, mount path: `/data`  
   (Veritabanı SQLite dosyasıdır; volume olmazsa her deploy'da **tüm veri silinir**.)
3. **Variables** — en az şunlar:

| Değişken | Değer |
|---|---|
| `SECRET_KEY` | Uzun rastgele metin (`python -c "import secrets;print(secrets.token_hex(32))"`) — sabit kalmalı |
| `DB_PATH` | `/data/emlak.db` |
| `ADMIN_USERNAME` / `ADMIN_PASSWORD` | Ofis sahibinin web girişi |
| `WEB_URL` | Railway domain'i, ör. `https://goldnic-production.up.railway.app` |
| `TELEGRAM_BOT_TOKEN` | `@BotFather`'dan |
| `OWNER_TELEGRAM_IDS` | Diren Doğan'ın sayısal Telegram ID'si (`@userinfobot`) — gece yedeği buraya gider |
| `BACKUP_PASSPHRASE` | Yedek şifresi (uzun bir cümle; **kaybetmeyin**) |

Tüm seçenekler açıklamalarıyla [`.env.example`](.env.example) dosyasında (TÜFE/EVDS, belge başlığı, komisyon
oranları, S3/R2, WhatsApp Cloud API).

4. **Settings → Networking → Generate Domain**. Deploy bitince loglarda `Web paneli port … çalışıyor` görülür.

> Sadece **tek** replika çalıştırın (Telegram botu aynı anda iki yerde çalışamaz).

## Kullanıcılar
1. Ofis sahibi `ADMIN_USERNAME` ile web'e girer; Telegram'da bota `/start` yazar (`OWNER_TELEGRAM_IDS` ayarlıysa sahip olur).
   ID bilinmiyorsa `SETUP_CODE=uzun-gizli-kod` ekleyip bota `/kurulum uzun-gizli-kod` yazılır, sonra değişken silinir.
2. Danışman eklemek: web → **Ekip** (kullanıcı adı + şifre) ya da Telegram'da `/davet` → danışman `/katil KOD`.
3. Telegram kullanıcıları `/sifre kullaniciadi` ile web şifresi alır.
4. Danışman ayrılınca portföyü tek tıkla devredilir, web girişi anında kapanır.

## Telefona uygulama olarak kurmak (PWA)
- **iPhone (Safari):** Paylaş → **Ana Ekrana Ekle**. **Android (Chrome):** menü → **Uygulamayı yükle**.
- Tam ekran açılır; alttaki menüden Panel, İlanlar, **+ Hızlı Ekle**, Takip; sağ üstteki avatardan tüm bölümler.
- Herhangi bir sayfada `Ctrl/⌘ + K` (veya `/`): kiracı, ev sahibi, telefon, daire, ilan, müşteri, belge no.

## Modüller

### Takip Merkezi — "Sıfır olay kaçırma"
Kira yenilemeleri, hukuki süreler, müşteri eşleşmeleri, boş daireler ve sistem uyarıları (TÜFE girilmedi, yedek
alınamadı) önem sırasıyla tek listede; her kalemde **Aradım / WhatsApp / Detay** gibi tek tık işlemler.

### Türk kira hukuku (`/hukuk`)
- **TÜFE tavanı (TBK 344):** yenileme ayından önceki ayın 12 aylık ortalama TÜFE değişimi; TÜİK her ayın 3'ünde
  açıklar, sistem 11:05'te çeker (EVDS → TÜİK bülteni → yerleşik tablo). Konutta 11.06.2022–01.07.2024 arası %25 sınırı.
- **Tahliye taahhüdü (TBK 352/1, 353; İİK 272):** teslimden sonra imza kuralı, tahliye tarihinden itibaren 1 aylık
  icra süresi, tarihten önce ihtar çekilirse 1 yıl — her aşama için hatırlatma.
- **5. yıl kira tespiti (TBK 344/3, 345)** ve **10. yıl gerekçesiz tahliye hakkı (TBK 347)**: 3 ay önceden kırmızı alarm.
- Emsal ilanlardan rayiç kira tahmini, kira artış hesaplayıcı.

### Resmi belgeler (Mülk → **Belge Merkezi**)
Taraf bilgileri (T.C. kimlik no ve IBAN doğrulanır) girildikten sonra tek tıkla PDF. Her belgede GN-YYYY-NNNNNN
numarası ve doğrulama kodu bulunur; `/belge/dogrula` sayfasından sahteciliğe karşı kontrol edilir. Tahliye
taahhütnamesi oluşturulurken hukuk radarına da eklenir.

### Müşteri Talep Havuzu (`/talepler`)
- Müşterinin cümlesini olduğu gibi yazın: *"Mehmet Bey 0532 111 22 33, 3+1 kiralık, bütçe 30-35 bin, Yahya Kaptan veya
  Alikahya, acelesi var"* → ad, telefon, işlem türü, oda, bütçe, bölge, olanaklar ve aciliyet otomatik anlaşılır.
- Talep kaydedildiği anda Sahibinden portföyü, boş daireler ve 90 gün içinde boşalacak daireler (tahliye taahhüdü)
  puanlanır (0–100, gerekçeleriyle). Yeni ilan senkronlandığında ya da daire boşaldığında **anında** Telegram'a
  *"🔔 Bu daire tam Mehmet Bey'in aradığı kriterde!"* mesajı ve **WhatsApp'tan gönder** butonu düşer.
- Gönderildi / ilgileniyor / gösterim / beğenmedi takibi, görüşme notları, 2 gündür aranmayan acil müşteri uyarısı.
- **Performans panosu (`/performans`):** danışman bazında kiralama/satış, tahmini ciro (kirada 1 aylık kira, satışta
  %4, KDV hariç — değiştirilebilir), ortalama kiralanma süresi, gönderilen ilan ve takip sayısı.
- Telegram: `/talep <cümle>` ve `/talepler`.

### Şifreli gece yedeği (`/yedek`, sadece ofis sahibi)
Her gece 03:00'te: tutarlı SQLite anlık görüntüsü → bütünlük kontrolü → gzip → **AES-256-GCM** (scrypt anahtar) →
sunucuda son 14 kopya + Diren Doğan'ın **kişisel** Telegram sohbeti (sessiz; danışman grubuna asla) + isteğe bağlı
S3/Cloudflare R2. Yedek alınamazsa veya 26 saattir başarılı yedek yoksa kırmızı alarm. Telegram'da `/yedek` ile
elle alınır.

**Geri yükleme:**
```bash
BACKUP_PASSPHRASE='…' python -m emlak_bot.backup verify gn-yedek-2026-10-15-0300.gnbak
BACKUP_PASSPHRASE='…' python -m emlak_bot.backup decrypt gn-yedek-2026-10-15-0300.gnbak -o emlak.db
```
Büyük yedekler `.part1of3` gibi parçalar halinde gelir; ilk parçayı vermek yeterlidir. `BACKUP_PASSPHRASE`
tanımlı değilse ve `SECRET_KEY` ortamdan geliyorsa yedek onunla şifrelenir (`--secret-key` ile çözülür); hiçbiri
yoksa yedek sunucudan **şifresiz çıkmaz**, yalnız yerel kopya tutulur.

**Denetim izi:** girişler, hatalı şifre denemeleri, telefonla arama ve WhatsApp tıklamaları (numara maskeli),
dışa aktarma ve yedek indirmeleri `/yedek` sayfasında filtrelenebilir.

## Telegram komutları
`/yardim` tam listeyi verir. Öne çıkanlar: `/liste`, `/yaklasan`, `/mulk 5`, `/zam 5 %30`, `/yenile 5`, `/tufe`,
`/hukuk`, `/talep …`, `/talepler`, `/ozet`, `/csv`, `/yedek`.

## Güvenlik
Şifreler hash'lenir (werkzeug), CSRF koruması, HttpOnly + Secure + SameSite çerez, 8 saatlik oturum, IP başına
hatalı giriş kilidi, sıkı CSP (satır içi betik yok), HSTS, X-Frame-Options. Danışman yalnız kendi portföyünü ve
müşterilerini görür. Dışa aktarılan CSV'de T.C. kimlik no ve IBAN yer almaz (KVKK). `.env`, `*.db` ve yedekler
git'e girmez.

## Geliştirme
```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env            # doldurun
python -m emlak_bot.main        # web: http://localhost:8000
pytest -o pythonpath=.          # tüm testler yeşil olmalı
```
Mimari, modül kancaları ve kurallar için [`CLAUDE.md`](CLAUDE.md).
