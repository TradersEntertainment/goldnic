# GOLD NICOMEDIA REAL ESTATE OS — MASTER ARCHITECTURE & DEVELOPMENT DIRECTIVE
> **MİSYON:** Bu proje, Kocaeli / İzmit bölgesinin lider emlak ofisi **Gold Nicomedia** (Ofis Sahibi: **Diren Doğan**) için geliştirilen, sıradan bir emlak takip uygulamasının çok ötesinde, sektördeki hiçbir olayı kaçırmayan, yapay zeka destekli, ultra lüks tasarımlı ve kurumsal düzeyde çalışan **hepsi-bir-arada Emlak İşletim Sistemi'dir (Real Estate OS)**.

🔗 **GitHub Repository:** [https://github.com/TradersEntertainment/goldnic](https://github.com/TradersEntertainment/goldnic)  
🚀 **Git Clone:** `git clone https://github.com/TradersEntertainment/goldnic.git`  
🌿 **Canlı Dağıtım Dalı:** `main` (Railway otomatik CI/CD entegreli)

---

## 🏛️ SİSTEM ÖZETİ VE MEVCUT MİMARİ
- **Backend:** Python 3.12+ (Flask, Waitress WSGI, Asyncio, Httpx, Pydantic/Dataclasses)
- **Veritabanı:** SQLite with WAL mode (`/data/emlak.db` persistent Railway volume)
- **Güvenlik:** HTTPOnly Secure Sessions, CSRF koruması, PBKDF2/Argon2 şifreleme, Rol Bazlı Yetkilendirme (Owner: Diren Doğan / Agent: Danışmanlar)
- **Entegrasyonlar:**
  - WhatsApp Business Cloud API & `wa.me` hızlı iletişim motoru
  - Sahibinden.com otomatik portföy botu (64+ aktif ilan, otomatik senkronizasyon, yinelenen ilan tespiti)
  - Telegram bildirim ve alarm altyapısı
  - Kademeli kira artış takibi (30 gün kala haftalık, son 2 hafta her 3 günde bir)
  - Toplu portföy içeri aktarma (Excel/Sheets tab-separated yapıştırma, CSV, hızlı seri form)

---

## 🎯 TEMEL FELSEFE & GELİŞTİRME KURALLARI
Claude Code olarak bu projeyi geliştirirken aşağıdaki prensiplerden **asla taviz verme**:
1. **Sıfır Olay Kaçırma (Zero-Loss Reliability):** Hiçbir kira yenilemesi, hiçbir tahliye taahhütnamesi süresi, hiçbir portföy fırsatı veya müşteri araması gözden kaçamaz. Sistem unutsa bile kullanıcıya hatırlatmalıdır.
2. **Kullanım Aşırı Kolay Olacak (Zero-Friction UX):** Diren Doğan ve danışmanları karmaşık menülerde kaybolmamalıdır. Tek tıkla arama yapabilmeli, tek tıkla WhatsApp mesajı atabilmeli, klavyeden `Tab` ve `Enter` ile saniyeler içinde iş bitirebilmelidir.
3. **Ultra Şık, Tam Ekran & Mobil Uyumlu (Executive Luxury UI):**
   - Tasarım sıradan bir bootstrap paneli gibi değil; Linear, Apple, Vercel standardında lacivert/antrasit/altın (`Gold`) renk paletiyle parlamalıdır.
   - PWA (Progressive Web App) desteği olacak: iPhone ve Android'de Safari/Chrome üzerinden "Ana Ekrana Ekle" dendiğinde tam ekran, adres çubuğu olmayan native bir mobil uygulama gibi çalışmalıdır.
4. **Masraftan ve API'den Kaçınma:** En iyi yapay zeka API'leri (OpenAI / Claude / Whisper), harita API'leri, resmi TÜİK/TCMB veri kaynakları sisteme çekirdekten entegre edilmelidir.
5. **Veri Bütünlüğü & Test Disiplini:** Yapılan her geliştirmede `pytest -o pythonpath=.` çalıştırılmalı, mevcut 28+ test daima yeşil kalmalı ve yeni özellikler için testler eklenmelidir.

---

## 🚀 GELİŞTİRİLECEK BÜYÜK MODÜLLER & YOL HARİTASI

### 1. TÜRK KİRA HUKUKU & AKILLI YASAL TAKİP MOTORU (EMLAKÇININ GİZLİ SİLAHI)
*Sıradan uygulamalar sadece tarihi sayar; Gold Nicomedia OS ise Türk Borçlar Kanunu'nu (TBK) bilir:*
- **Otomatik TÜFE Çekici (TÜİK & TCMB Entegrasyonu):** Her ayın 3'ünde TÜİK tarafından açıklanan *12 Aylık Ortalamalara Göre TÜFE Değişim Oranı*'nı otomatik çek. Yenilemesi gelen daireye yasal tavan kira artışını kuruşu kuruşuna hesapla.
- **Tahliye Taahhütnamesi Takip Modülü:** Kiracının imzaladığı tahliye taahhütnamesinin geçerlilik süresini ve yasal bildirim tarihini takip et (sözleşme tarihinden sonra imzalanma kuralı, 1 ay önceden ihtar çekme uyarısı).
- **5. Yıl Kira Tespit Davası Radarı:** TBK madde 344 uyarınca 5 yılı dolduran kiralarda piyasa rayicine göre kira tespit davası açma hakkı doğar. 5. yılını dolduracak mülkler için Diren Doğan'a özel "Rayiç Yenileme & Tespit Fırsatı" uyarısı üret.
- **10 Yıllık Uzama Süresi Sonu (TBK 347):** 10 yılı dolduran kiracılarda gerekçesiz tahliye hakkı doğar; sistem bunu 3 ay öncesinden kırmızı alarm olarak haber verir.
- **Depozito Değer Kaybı Hesaplayıcı:** Kiracının ilk girdiği tarihteki depozitonun altın/dolar/enflasyon karşısındaki reel değerini göster (tahliyede anlaşmazlıkları önler).

### 2. SESLİ NOT & YAPAY ZEKA ASİSTANI (WHISPER + LLM ENTEGRASYONU)
- **WhatsApp Sesli Mesajdan Portföy Güncelleme:** Danışman yolda veya arabadayken ses kaydı atar: *"Diren abi, Yahya Kaptan'daki Ahmet Bey'le görüştüm, kirayı 32 binden 45 bine çıkardık, sözleşmeyi 1 Aralık'ta yeniliyoruz."*
- Whisper API sesi metne döker, Claude/OpenAI mesajı ayrıştırır (`daire: Yahya Kaptan`, `yeni_kira: 45000`, `tarih: 2026-12-01`), mülkü otomatik günceller ve notlara ekler.
- **Tek Tıkla Viral İlan Metni Üretici:** Mülkün oda sayısı, katı, konumu girildiğinde Sahibinden, Hepsiemlak, Instagram ve Twitter için profesyonel, ilgi çekici emlak ilan açıklamaları üreten AI sihirbazı.
- **Yapay Zeka Destekli Rayiç Fiyatlama (AI Valuation):** Sahibinden'den çekilen 64+ aktif ilan ve piyasa verisini analiz ederek yeni girilen bir daire için ideal kira ve satış fiyatı önerisi sunar.

### 3. TEK TIKLA RESMİ KİRA SÖZLEŞMESİ & PDF MOTORU
- Mülk yenilendiğinde veya yeni kiracı girdiğinde;
  - T.C. İçişleri & Maliye Bakanlığı standartlarına tam uyumlu **Kira Sözleşmesi**,
  - **Tahliye Taahhütnamesi**,
  - **Kira Teslim / Demirbaş Tutanağı**
- Tüm tarafların (Ev Sahibi, Kiracı, Kefil, Emlak Ofisi) T.C. Kimlik, IBAN, adres ve özel şartlarıyla eksiksiz doldurulmuş şekilde, kurumsal Gold Nicomedia filigranı ve logosuyla tek tıkla **PDF olarak çıktısını alma ve WhatsApp'tan taraflara gönderme**.

### 4. AKILLI ALICI / KİRACI TALEP HAVUZU (MATCHING CRM)
- **Müşteri Talep Havuzu:** *"3+1 kiralık arıyor, bütçe 30-35 bin TL, Yahya Kaptan veya Alikahya istiyor, acelesi var."* şeklinde müşteri talepleri toplanır.
- **Otomatik Eşleşme:** Sahibinden botu yeni bir ilan çektiğinde ya da bir daire boşaldığında sistem derhal eşleştirme yapar:
  *"🔔 Bu daire tam Mehmet Bey'in aradığı kriterde! Tek tıkla WhatsApp'tan ilanı gönder."*
- Portföydeki dairelerin kaç günde kiralandığını, hangi danışmanın ne kadar ciro ürettiğini gösteren **Ofis Performans & Liderlik Panosu (Leaderboard)**.

### 5. PWA (PROGRESSIVE WEB APP) & TAM EKRAN MOBİL DENEYİM
- `manifest.json`, modern SVG app iconları ve Service Worker kurulumu.
- Mobil cihazlarda masaüstü butonları yerine alt navigasyon barı (Bottom Navigation: Dashboard, İlanlar, +Hızlı Ekle, Takip, Ekip).
- Hızlı Arama (Spotlight / `Cmd + K` veya `Ctrl + K`): Klavyeden iki tuşla herhangi bir kiracı, ev sahibi, telefon no veya daire adını anında bulup ekrana getirme.

### 6. 7/24 BULUT YEDEKLEME & GÜVENLİK KALKANI
- `/data/emlak.db` veritabanının her gece otomatik olarak şifreli `.dump` yedeğinin alınması.
- Alınan yedeğin Diren Doğan'ın şahsi Telegram hesabına veya güvenli bir S3/Drive deposuna sessizce iletilmesi (sıfır veri kaybı garantisi).
- Tüm oturumlarda Rate-Limiting, Brute-Force koruması ve detaylı denetim izi (Audit Trail: kim hangi müşterinin telefonunu görüntüledi, kim ne zaman aradı).

---

## 🛠️ GELİŞTİRME & KOMUT YÖNERGESİ

### Geliştirme Ortamı Komutları:
```bash
# Bağımlılıkları kontrol et ve çalıştır
pip install -r requirements.txt

# Tüm testleri çalıştır (Tüm testler istisnasız geçmelidir)
pytest -o pythonpath=.

# Spesifik test dosyaları
pytest -o pythonpath=. tests/test_core.py
pytest -o pythonpath=. tests/test_web.py
pytest -o pythonpath=. tests/test_listings.py
pytest -o pythonpath=. tests/test_whatsapp_and_bulk.py

# Uygulamayı yerelde başlat
python -m emlak_bot.main
```

### Git & Dağıtım Standartları:
- Kod değişiklikleri temiz, modüler ve Python tip ipuçlarıyla (`typing`) yazılmalıdır.
- Kod tamamlandığında `pytest` koşturulmalı, ardından `git commit` ve `git push origin main` yapılarak Railway otomatik dağıtımı tetiklenmelidir.
- Veritabanı şeması değişikliklerinde geriye dönük uyumluluk (`PRAGMA table_info` kontrolleriyle dinamik ALTER TABLE) korunmalıdır.

---

## 👑 DİREN DOĞAN & GOLD NİCOMEDİA İÇİN VİZYON
Bu sistem tamamlandığında, Gold Nicomedia sadece İzmit'in değil Türkiye'nin en teknolojik, en kurumsal ve portföyüne en hakim gayrimenkul ofisi haline gelecektir. Hiçbir kira günü şaşmayacak, hiçbir sözleşme gecikmeyecek ve ofis sahibi Diren Doğan cep telefonundan tek bir dokunuşla tüm ofis operasyonunu kusursuzca yönetecektir.
