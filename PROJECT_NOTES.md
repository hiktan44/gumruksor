# Proje Devir Notu — Ticaret Bilgi Masası / mevzuat-mcp

Son güncelleme: 1 Ekim 2026 (Europe/Istanbul)

## 1 Ekim 2026 — açık admin listesi için ayrı PR hazırlığı

- Kullanıcının öneriyi uygulama talimatıyla PR #97, doğrulanan `1d3723a`
  üzerinden birleştirildi. Main commit'i `2fac4561b00f0be5ab585e84948d6fca71fe0327`.
  Main Security firewall ve otomatik Coolify deploy workflow'u başarılı;
  canlı `/health` aynı commit'i ve `healthy` durumunu bildirdi. Ana sayfa 200;
  Google giriş başlangıcı doğru callback ile accounts.google.com'a yönlendirir.
  Kullanıcının gerçek Google girişiyle dönüşü ayrıca doğrulanmalıdır.
- Bu ayrı dal `fix/admin-explicit-allowlist`, `2fac456` üzerinden hazırlandı.
  `DEFAULT_ADMIN_EMAILS` ve yapılandırmaya otomatik ekleme kaldırıldı.
  Yalnız açık `ADMIN_EMAILS` veya constructor listesi yetki verir; boş/eksik
  liste hiçbir e-postaya yönetici yetkisi vermez. Tam adres eşleşmesi korunur.
- Kullanıcı iki tam adresli yönetici listesini doğruladı. Canlıda bu listenin
  çalışma zamanı `ADMIN_EMAILS` değerine yazıldığı doğrulanmadan bu PR
  birleştirilmemeli/yayınlanmamalı; önceki varsayılanla erişen yöneticiler
  de açıkça yapılandırılmalıdır. Canlı yapılandırma bu PR'da değiştirilmez.
  Kullanıcıya ait adresler kaynak koda yeni bir varsayılan olarak gömülmez.
- Beş yeni test ve güncellenen regresyonlar: eksik/boş liste, ortamdan liste,
  constructor'ın ortamı geçersiz kılması, eski varsayılanların yalnız listede
  olduğunda yetkili olması, üç yedekleme ucunda boş listenin erişimi reddetmesi.
  Önceki davranışta 5 regresyon testi 15 başarısız assertion üretti; hata yok.
- Düzeltmeyle, UTC ve dış ağ/DNS kapalı geçici verilerde **85/85 odaklı test**
  ve **1542 toplu test: 1532 başarılı, 10 atlandı, hata yok**.
  compileall, JavaScript sözdizimi ve diff kontrolleri başarılı.
- README ve .env.example, geçişten önce açık admin listesinin tanımlanmasını
  belgeliyor. Atanmış kullanıcı rolleri, genel OAuth domain politikası ve
  bağımlılıklar değişmedi. Bu ayrı PR canlıya alınmadı.

## 1 Ekim 2026 — PR #97 bağımlılık taraması ve imza testi güncellemesi

- Kullanıcı PR güncellemesini onayladı. Dal: `fix/admin-exact-email`;
  PR: https://github.com/hiktan44/gumruksor/pull/97 (açık, taslak değil).
- Önceki HEAD `be4354b` üzerinde iki GitHub kontrolü tamamlandı:
  Agentic Security Guard CI başarılı; Security firewall yalnız Trivy adımında
  başarısız. Testler, sözdizimi, gitleaks, container build ve appuser kontrolü geçti.
  Trivy, PyJWT 2.13.0 için altı ve urllib3 2.7.0 için iki bulgu raporladı
  (toplam bir kritik, yedi yüksek).
- `uv.lock` yalnız PyJWT **2.14.0** ve urllib3 **2.8.0** kayıtlarını yükseltir.
  Diğer paket kayıtları ve proje bağımlılık tanımları korunur. Kilit çözümü
  CI'da kullanılan `uv 0.11.6` ile doğrulandı; `uv sync --frozen` başarılı.
- İnceleme yorumundaki son karakter `A→B` için 1/16 kararsızlık mevcut kodda
  doğrulanmadı: `auth_service._b64decode` kanonik biçimi ayrıca kontrol eder.
  Tüm 16 HMAC son karakteri ve 192 HTTP ret kontrolü geçti. Yine de test artık
  imzanın **ilk karakterini** değiştirir; bu doğrudan farklı imza baytları ve
  HMAC doğrulama başarısızlığı üretir, padding denetimine dayanmaz.
- Yeni sürümlerle, dış ağ/DNS kapalı ve geçici veri dizinleriyle: **80/80 odaklı
  test başarılı**; **1537 toplu test: 1527 başarılı, 10 atlandı, hata yok**.
  Toplu paket GitHub runner'ı ile aynı **UTC** saat diliminde çalıştırıldı;
  önceki yerel iki tarih sınırı başarısızlığı bu koşulda görülmedi.
- Python compileall, `node --check web/app.js` ve `git diff --check` başarılı.
  Yerel loglar proje yanındaki `verification/dependency-*.log` dosyalarında.
  Yeni commit'in container/Trivy sonucu GitHub CI üzerinden doğrulanmalı.
- Bu çalışma PR dalı kapsamındadır; merge ve canlıya alma yapılmaz. Admin
  varsayılanları ve OAuth sağlayıcısı politikası aşağıdaki kapsamda korunur.

## 1 Ekim 2026 — kod kopyası ikinci bilgisayara eşleştirildi

- `192.168.1.18` Mac mini'ye erişim, açık SSH anahtarının mevcut güvenilen kayıtla
  birebir eşleşmesi üzerinden doğrulandı; güven kaydı ve hesaplar değiştirilmedi.
- Düzeltme commit'i `5caf5625a6091508aae6dba5ae768a6c9e36ec04`, iki bilgisayarda
  `fix/admin-exact-email` dalında bulunuyor. Bu devir notu da iki kopyaya aktarılır.
- Kaynak repo: `/Users/ht44/Documents/Codex/2026-10-01/task/gumruksor` (`192.168.1.15`).
- Hedef repo: `/Users/hikmettanriverdi/Documents/Codex/2026-10-01/gumruksor-admin-fix/gumruksor`
  (`192.168.1.18`). Takip edilen 222 kaynak dosyası SHA-256 ile birebir doğrulandı.
  Yedekler/veritabanları hariç tutuldu; eski mevzuat-mcp kopyası ve yerel dosyası korundu.
- Hedefte `uv sync --frozen`, Python 3.12.9: 80 odaklı test başarılı. Test sürecinde
  dış ağ/DNS erişimi kapalı, veri dizinleri geçiciydi. Kaynakta da aynı 80 test
  dış ağ kapalı olarak geçti. Kaynaktaki önceki toplu paket sonuçları aşağıdadır;
  hedefte toplu paket yeniden çalıştırılmadı.
- Hedefte `compileall`, `git diff --check` ve temiz çalışma kopyası kontrolleri geçti.
  Her iki proje klasörünün yanındaki `verification/` altında eşleştirme kanıtı,
  test logları, patch ve Git bundle bulunur. Push/merge/deploy yapılmadı.
- Bu, talep edilen mevcut kodun tek seferlik eşleştirmesidir; sürekli bir
  dosya senkronizasyonu görevi veya canlı veri aktarımı oluşturulmadı.

## 30 Eylül 2026 — yönetici kimliği için tam e-posta eşleşmesi (yerel hazırlık)

- Taban: `hiktan44/gumruksor` main, `f71528579f9cb3162967ecd6b74044116ec4c6ff`.
  Yerel dal: `fix/admin-exact-email`.
- `AccountService.is_admin` yalnız boşlukları ayıklanıp büyük/küçük harfi
  normalleştirilmiş **tam e-posta adresi** allow-list içinde olduğunda yetki verir.
  Yerel ad üzerinden eşleşme ve Gmail domaini ekleme kaldırıldı; boş, metin olmayan
  ve eksik/bozuk adresler reddedilir. Eksik `@gmail` varsayılanları kaldırıldı.
- Mevcut geçerli yapılandırılmış tam adresler ve iki yerleşik `@gmail.com` adresi
  korunur. **Kalan politika riski:** bu iki yerleşik adres `ADMIN_EMAILS` boş olsa
  veya başka adreslerle yapılandırılsa bile her zaman eklenir. Bu davranışı
  kaldırmak ayrı bir karar gerektirir; bu değişiklikte kapsam genişletilmedi.
- Sekiz yeni regresyon testi: tam/yapılandırılmış kimlikler, başka domainlerde
  aynı yerel ad, bozuk/boş kimlikler, normal kullanıcı rolü/kotası ve yedek raporu,
  oluşturma, indirme yetki sınırları. HTTP testleri gerçek oturum ayrıştırma ve
  admin kontrolünü kullanır; yetkisiz çağrıda rapor/yedek/dosya çözümleme çalışmaz.
  Anonim, bozuk, değiştirilmiş ve süresi dolmuş oturumlar da reddedilir.
- Python 3.12.14, `uv sync --frozen`: 80 odaklı test geçti. Toplu
  `python -m unittest discover -s tests -v`: 1537 test, 1525 başarılı,
  2 başarısız, 10 atlandı. Değiştirilmemiş taban kopyasında 1529 test çalıştı;
  aynı iki başarısızlık ve aynı 10 atlama görüldü:
  `test_eu_vat.SeedLookupTests.test_a_stale_confirmation_brings_the_warning_back`
  ve `test_temporal_validity.TariffTemporalTests.test_same_start_date_yields_observed_boundary`.
  Atlamalar: 9 canlı tarife/kontrol önbelleği testi, 1 Chromium PDF testi.
- `compileall`, `node --check web/app.js`, `git diff --check` geçti. Test süreçleri
  yeni geçici veri dizinleriyle ve uygulama ortamını miras almadan çalıştırıldı.
  Gerçek kullanıcı veritabanı/yedeği ve üretim üzerinde test yapılmadı;
  OAuth sağlayıcısı ve canlı yapılandırma değiştirilmedi. Push/merge/deploy yok.
- Kullanıcı kod/repo dosyalarının `192.168.1.18` ile eşleşmesini istedi. Bu hedef
  oturuma bağlı değil; SSH güven doğrulaması tamamlanmadı. Aktarım henüz yapılmadı;
  yerel commit ve taşınabilir patch hazırlandıktan sonra hedef erişimi ve mevcut
  repo değişiklikleri doğrulanarak uygulanmalı.
- Container build/Trivy, gerçek Google OAuth ve canlı/veri bağımlı E2E kontroller
  bu yerel hazırlıkta çalıştırılmadı.

## 8 Eylül 2026 oturumu — 3., 4. ve 5. sıra aşamalarının tamamlanması

- Raporun 3. sırası kapatıldı:
  - 4 haneli HS4 karar ağacı ve arama desteği (`tariff_engine.py`, `mevzuat_mcp_server.py`). HS4 kodları doğrudan HS6 seviyesine dallandırılıyor.
  - "İGV listesinde yoksa %0" mantığı: İGV ekli listelerinde yer almayan doğrulanmış GTİP'lerde ek gümrük vergisi otomatik olarak %0 kabul ediliyor ve hesap `complete` statüsüne geçiyor; `lookup` ve arayüzde bilgilendirme uyarısı veriliyor.
- Raporun 4. sırası kapatıldı:
  - Sunucu tarafı kapı bayrakları doğrulaması (`customs_advisor.py:evidence_pack`): `exact_gtip_confirmed`, `tariff_selection_confirmed` ve `classification_confidence_score` bayrakları resmî tarife veritabanı eşleşmesine göre denetleniyor; eşleşmeyenler sunucuda düşürülüyor.
  - Görsel base64 redaksiyon atlaması (`security_firewall.py`): `data:image/` ve `data:application/` URL'leri TCKN/telefon maskelemesinden muaf tutularak piksellerin bozulması önlendi.
  - Erken görsel boyut denetimi ve dekompresyon bombası koruması (`customs_advisor.py:validate_image`, `app.py`): Piksel açılmadan önce `image.size` başlığı denetlenip 25 MP üzeri ve geçersiz boyutlar derhal engelleniyor; gövde akışı 12 MB ile sınırlandırıldı.
- Raporun 5. sırası (Arayüz & Kullanılabilirlik) kapatıldı:
  - Danışman sekmesi görünürlüğü (`web/app.js`): Açılışta `initMarketplaceStatus()` ile `/api/consultants` denetlenerek `marketplace-enabled` sınıfı atanıyor; sekme kendiliğinden görünür kılınıyor.
  - PDF çok sayfalı yazdırma ve detay açılımı (`web/app.css`, `web/app.js`): `inset: 0` ve mutlak konumlandırma yerine akışkan yazdırma düzeni sağlandı, yazdırma esnasında kapalı `<details>` blokları açılarak çok sayfalı dosyaların ilk sayfada kırpılması engellendi.
  - `safeStorage` try/catch sarmalayıcısı (`web/app.js`): Bütün depolama işlemleri sarmalanarak kısıtlı gizli sekme modlarında uygulamanın çökmesi önlendi.
  - Tarife doğrulama / analiz gönderim yarışı (`web/app.js`): `#customsForm` submit anında doğrudan tarife ağacı beklenerek `pendingSubmit` kilitlenmesi giderildi; 4 haneli kodlar arayüz doğrulamasına dahil edildi.
  - Cihazda kayıtlı ön değerlendirmeler (`web/app.js`): `saveLocalScenario` ile her ön değerlendirme `gumrukce-scenarios` anahtarına yerel cihaz kaydı olarak yazılıyor; giriş yapmamış kullanıcılar için `#scenarioList` paneli aktifleştirildi.
- 225 birim ve entegrasyon testi 0 hata ile geçiyor (`pytest`).
- Cloudflare "Workers Builds: mevzuat-mcp" kontrolü depoya dışarıdan bağlı ve her commit'te kırmızı;
  depoda Workers yapılandırması yok. Cloudflare panosundan Workers & Pages → mevzuat-mcp → Settings →
  Builds → Disconnect ile kaldırılmalı (MCP'de bu işlem için araç yok).

## 4 Eylül 2026 oturumu — rekabet analizi karşılıkları

Atez TARIFF rekabet analizindeki Faz 1-2 maddeleri uygulandı (109 test OK):

- **`describe_product_image` MCP aracı**: fotoğraftan görünür evsaf çıkarımı artık MCP istemcilerinde de
  (web'deki akışın aynısı). Ortak `decode_image_data_url` customs_advisor.py'de; MCP çıktısı kırmızılaştırılıyor.
- **Hızlı düzeltmeler**: /api/search gelecek tarihli RG kayıtlarını sayfa sonuna atıp `date_warning` veriyor
  (kaynak hatası korundu, sıralama bozulmuyor); Değişiklikler sekmesi ham anahtarları Türkçeleştirdi;
  danışman pazaryeri `CONSULTANTS_MARKETPLACE_ENABLED=1` ile açılır (varsayılan kapalı — demo profiller gizli);
  GTİP12 onayında resmî oran alanlara öneri olarak dolduruluyor (`.rate-suggested`).
- **Menşe belge kural tablosu** (`origin_documents.py`): A.TR / EUR.1 / menşe şahadetnamesi kuralları,
  resmî yürürlükteki STA listesinden (04.09.2026; Katar, BAE, Morityus, BK dahil; Tunus listede YOK).
  Ön değerlendirme çıktısına `origin_documents` alanı olarak eklendi (MCP dahil).
- **Senaryo karşılaştırma** `/api/tariff/scenarios`: aynı GTİP için menşe başına resmî sütun + oran + belge;
  Tarife & Maliyet aracında "Menşe senaryoları" paneli. (Örn. 691110000011: Çin %12+%19, Almanya/G.Kore %0.)
- **Belge girdisi** `/api/customs/ingest-source`: kullanıcı PDF'i veya HTTPS ürün sayfasından metin çıkarımı;
  SSRF koruması (yalnız https, özel/meta IP reddi, DNS çözümleme denetimi, atlama başına yeniden doğrulama),
  10 MB / 6 bin karakter sınırı; metin kullanıcı onayına sunulup ürün tanımına eklenir.
- **PDF çıktısı**: ön değerlendirme dosyasında "PDF olarak kaydet" (tarayıcı yazdırma görünümü, print CSS).

E-posta gönderimi: `/api/email/precheck` transactional API ile (env: `RESEND_API_KEY` + `MAIL_FROM`,
Coolify'da girilince açılır; yalnız giriş yapan kullanıcının kendi adresine, 5/saat limiti, HTML sunucuda
şablonlanır). Maliyet motoruna ödeme şekli bağlandı: peşin → KKDF %0, kredili/vadeli → %6 önerisi + uyarı;
`total_taxes` ara toplamı ve sabit beyanname harcı uyarısı eklendi (formula_version v3).
EN/DE arayüz ve AB TARIC varış tarifesi ayrı oturum konusu.

## Proje nedir
FastMCP tabanlı Türkiye mevzuat + Ticaret Bakanlığı bilgi sunucusu ve üstündeki web uygulaması
("Ticaret Bilgi Masası", "Gümrükçe'ye Sor"). ASGI giriş noktası `app.py`, arayüz `web/`.
Yerelde çalıştırma: `scripts/dev.sh` (anahtar gerekmez; veri `.dev-data/` altına yazılır).

## 2 Eylül 2026 oturumu — ne yapıldı

### Doğrulananlar (yerel sunucu, Playwright ile bir kullanıcı gibi gezildi)
- 103 birim testi geçiyor (`pytest`).
- Açılış sayfası, /app (Kaynak araştır, Genel mevzuat, Gümrükçe'ye Sor ve 6 alt sekmesi),
  Hesabım diyaloğu (Özet, Kanıt dosyaları, Paketler), /admin, /gizlilik, /kullanim-kosullari
  masaüstü (1440) ve mobil (390) genişlikte konsol hatasız açılıyor.
- Uçtan uca akışlar çalışıyor: katalog araması + belge okuyucu, genel mevzuat araması,
  ön değerlendirme (anahtarsız "yalnız kanıt paketi" modunda), Tarife & Maliyet sorgusu
  (610342000000 · Çin → %12 GV, %39 İGV kaynak satırlarıyla), Kontroller sorgusu
  (2026/18 tebliğ eşleşmesi), kanıt dosyası kaydı ve Hesabım'da listelenmesi, admin özeti.
- Google girişi yapılandırılmadığında "/auth/google" düzgün biçimde açılış sayfasına
  bilgilendirme ile döndürüyor.

### Düzeltilen hatalar
- Mobilde (≤840px) üst çubuk 4px taşıyor, sayfa yatay kaydırılabiliyordu → `web/app.css`
  840px media bloğu (`.topbar`, `.topbar-tools`, `.app-login/.app-account`).
- Kenar çubuğu "Canlı kapsam" kartında değerler etiketle bitişik ve hizasız
  ("Yenileme1 sa. / tam 6 sa.") → `.coverage dl div`, `.coverage dd`.

### Görsel yumuşatma (istek: "daha yumuşak olsun")
- `web/app.css` sonuna "Yumuşak yüzey katmanı" eklendi: `--radius-*` ve `--shadow-card`
  belirteçleri, kartlar/alanlar/düğmeler/sekmeler/diyaloglar için köşe yuvarlatma, daha açık
  çizgi rengi, odakta yumuşak halka. Izgara, tipografi ve metinler değişmedi; koyu tema
  doğrulandı.
- `web/landing.css` sonuna aynı katman: sert ofset gölgeler yerine yumuşak gölge, kartlar
  ve düğmelerde köşe yuvarlatma.
- Geri almak için iki dosyanın sonundaki katman blokları silinir.
- Dikkat: köşe yuvarlatma için `overflow: hidden` yalnızca kaydırmayan kutulara verildi.
  `.customs-view-tabs` mobilde yatay kaydırır (`overflow-x: auto` korunmalı), `.account-dialog`
  ve `.consultation-dialog` dikey kaydırır (overflow verilmemeli). Bu iki regresyon aynı
  oturumda yakalanıp geri alındı; yeni kutu yuvarlatırken aynı kontrolü yapın.

### Yerelde doğrulanamayanlar (anahtar/dış servis gerekir)
- Gerçek Google OAuth ile hesap açma (oturum, `AUTH_SESSION_SECRET` ile imzalanmış test
  çerezi üretilerek taklit edildi; gerçek kayıt akışı denenmedi).
- Stripe checkout / portal / webhook (`billing_mode: disabled`).
- OpenRouter görsel analiz ve GTİP aday üretimi (anahtarsız akış "evidence_only" döndü).
- Canlı Bedesten/ticaret.gov.tr eşitlemesinde 40 sayfa güvenlik sınırı hataları
  (`gumruk`, `destekler` kaynakları) — tasarım gereği; katalog yine 6 binin üstünde kayıtla dolu.

## Bekleyen işler / plan (öncelik sırasıyla)

### P1 — Kullanıcının hemen fark edeceği
1. **Katalog eşitleme sınırı**: `gumruk` ve `destekler` kaynaklarında 40 sayfa sınırı
   dolduğu için 36 + 79 sayfa taranmıyor. Sınırı kaynak bazında artırmak veya kalan
   sayfaları bir sonraki saatlik turda devam ettirmek (kaldığı yerden) gerekir.
2. **Ön değerlendirme sonuç ekranı çok uzun**: kanıt defteri 30+ kaynak kartını art arda
   listeliyor. Kaynak defteri katlanabilir (`details`) olmalı, önce özet/riskler görünmeli.
3. **Mobil menü**: 390px'te "Gümrükçe'ye Sor" sekmesi iki satıra bölünüyor ve alt
   sekmeler (6 adet) yatay kaydırma gerektiriyor; mobilde ikon+kısa etiket veya açılır
   menü düşünülmeli.
4. **Boş durumlar**: "Yayındaki danışmanlar" ve "Değişiklikler" panelleri ilk kullanımda
   yalnızca "yok" diyor; ne yapılacağını gösteren kısa bir yönlendirme kartı eklenmeli.

### P2 — Eksik/yeni bölümler
5. **Danışman e-posta bildirimi**: uygulama içi rozet eklendi; e-posta için SMTP/sağlayıcı
   (ör. Resend) yapılandırması ve şablon gerekli.
6. **Kanıt dosyası paylaşımı**: Ekip paketi "paylaşılan ürün dosyaları" vadediyor ama
   `/api/dossiers` yalnızca kullanıcıya özel. Ekip/kuruluş kavramı gerekiyor.
7. **Değişiklik takibi bildirimi**: izleme listesi cihazda (localStorage) tutuluyor;
   sunucu tarafına taşınıp değişiklikte e-posta gönderilmeli.
8. **Genel mevzuat aramada boş tarih**: `gazette_date` çoğu kayıtta null; Bedesten'den
   RG tarihi çekilip listede gösterilmeli.
9. **Erişilebilirlik turu**: düğme kontrastları (turuncu üstü beyaz metin), odak sırası,
   `aria-live` bölgelerinin gerçekten duyurulup duyurulmadığı.

### P3 — Altyapı
10. Yerel geliştirme için `.env.example` içine `PUBLIC_BASE_URL=http://localhost:8000`
    notu ve `scripts/dev.sh` açıklaması README'ye eklenmeli.
11. `.claude/launch.json` sürüm kontrolünde değil (yalnız bu makinede); gerekirse
    `.gitignore`'a `.claude/` eklenmeli.
12. Playwright tabanlı görsel duman testi (`scratchpad/shoot.py` benzeri) `tests/`
    altına alınabilir; şu an yalnızca oturum içi kullanıldı.

## 2 Eylül 2026 (akşam) — kullanıcı gibi tam özellik turu
İki hesapla (müşteri + yönetici/danışman) her kontrol tıklandı. Çalıştığı doğrulananlar:
açılış sayfası etkileşimleri (ürün sekmeleri, aylık/yıllık, SSS), hızlı rotalar, kaynak /
belge türü / yıl / mülga filtreleri, sayfalama (26 sayfa ileri-geri), belge okuyucu (metin
içi arama ve vurgulama, atıf kopyalama, resmî bağlantılar, 300 bin karakterlik Gümrük
Kanunu'nda 112 bölümlük içindekiler), genel mevzuat başlık/içerik/numara modları ve tür +
tarih filtreleri, MCP adresi kopyalama, tema kalıcılığı, görsel yükleme (80×80 altı görsel
için açık hata), aday kod → tarife ağacı → dal seçimi, ön değerlendirme + takip sorusu +
uzman paketi indirme + kanıt dosyası kaydı, izleme listesine ekleme/kaldırma, değişiklik
defteri, işlem rehberi, danışman başvurusu → yönetici onayı → talep gönderme → kabul →
karşılıklı mesaj → kapatma, yönetici panelinde paket değişikliği, Hesabım'da kota, JSON
indirme (110 KB), dosya silme, güvenli çıkış, hesap silme, admin erişim kontrolü (303/403).

### Bu turda bulunan eksikler — 2 Eylül akşamı hepsi uygulandı
Aşağıdaki dokuz madde aynı gün kapatıldı (Playwright ile doğrulandı, 104 test geçiyor):
1. Tarife doğrulama yarışı → `state.customsPendingSubmit`: doğrulama bitince analiz kendiliğinden başlıyor.
2. "Bu oranları kullan" düğmesi → hem Tarife & Maliyet aracında hem ön değerlendirme sonucunda
   (`applyRatesButton`); belirsiz (alt GTİP'e göre değişen) oranlar aktarılmaz.
3. Danışmanlık formunda karakter sayacı + en az uzunluk ipucu; geçersiz gönderimde toast.
4. Danışmanlar sekmesinde bekleyen talep rozeti (`refreshConsultationBadge`, girişte ve her
   yüklemede). E-posta bildirimi hâlâ yok: SMTP/sağlayıcı yapılandırması gerekir (bekleyen iş).
5. Paketler: Stripe kapalıyken "Çevrim içi ödeme yakında" metni + `SALES_CONTACT_EMAIL`
   (varsayılan hiktan44@gmail.com) ile "Satış ekibiyle görüşün" mailto bağlantısı; `/api/plans`
   `sales_email` döndürür.
6. Markalı 404: `web/404.html`; `/api/*`, `/mcp` ve HTML istemeyen istekler JSON 404 alır.
   `/app?scope=customs` ve `#customs` doğrudan Gümrükçe'yi açar.
7. Görsel yükleme ipucunda 80×80 alt sınırı.
8. Kanıt dosyası silinince kota iadesi (`AccountService.delete_dossier` usage_ledger satırını
   siler; test eklendi) ve Hesabım'da kota açıklaması.
9. Sıfır sonuçta aktif filtre çipleri ("Tümünü kaldır" dahil), tıklayınca arama yenilenir.

### Önceki liste (arşiv)
- **Tarife doğrulama yarışı**: GTİP yazıp hemen "GTİP bul" düğmesine basınca "doğrulanmasını
  bekleyin" uyarısı çıkıyor; doğrulama bitince analiz kendiliğinden başlamalı (P1).
- **Vergi oranları maliyete akmıyor**: Tarife aracı %12 GV ve %39 İGV'yi buluyor ama ön
  değerlendirmede maliyet için kullanıcıdan aynı oranların elle "doğrulanmış" girilmesi
  isteniyor; "bulunan oranı kullan" düğmesi gerekli (P1).
- **Danışmanlık formu min. uzunluk**: konu 5, mesaj 10 karakter altında yalnızca tarayıcı
  balonu çıkıyor; alan altına görünür ipucu ve sayaç eklenmeli (P2).
- **Danışman bildirimi yok**: yeni talep yalnızca Danışmanlar sekmesi açılınca görünüyor;
  sekmede okunmamış rozeti ve e-posta gerekli (P2, mevcut madde 5 ile birleşti).
- **Kurumsal paket düğmesi pasif**: Hesabım › Paketler'de "Satış ekibiyle görüşün" tıklanamıyor;
  açılış sayfasındaki gibi mailto bağlantısı olmalı. Stripe kapalıyken "Stripe ayarı
  bekleniyor" yerine kullanıcıya anlamlı bir metin ("Yakında") gösterilmeli (P2).
- **404 sayfası**: bilinmeyen adresler düz metin "Not Found" döndürüyor; markalı 404 (P3).
- **Görsel boyut ipucu**: yükleme kutusunda 80×80 alt sınırı yazmıyor (P3).
- **Silinen kanıt dosyası kotayı iade etmiyor** (4/10 kalıyor); bilinçli tasarımsa Hesabım'da
  açıklanmalı (P3).
- **Kaynak filtre + belge türü + yıl** birlikte çoğu zaman 0 sonuç veriyor; boş sonuçta
  hangi filtrenin daralttığını gösteren "filtreyi kaldır" çipleri yararlı olur (P3).

## Önemli kimlikler / seçiciler
- Oturum çerezi: `tbm_session` (HMAC imzalı, `auth_service.GoogleAuthService`).
- Kapsam sekmeleri: `[data-scope=customs|ticaret|general]`; Gümrükçe alt sekmeleri
  `[data-customs-view=assistant|tariff|controls|changes|consultants|guide]`.
- Ön değerlendirme formu: `#customsQuestion #productDescription #originCountry
  #candidateGtip #composition #analyseButton`; sonuç `#customsOutput`, kaydet `#saveScenario`.
- Tarife: `#tariffForm #tariffGtip #tariffOrigin`; kontrol: `#controlsForm #controlsGtip`.
- Hesap diyaloğu: `#appAccountButton`, sekmeler `[data-account-tab=summary|dossiers|plans]`.

## Kararlar
- Tasarım yumuşatması "ek katman" olarak dosya sonuna yazıldı; mevcut kuralların üstüne
  yazılmadı ki fark tek blokta görülsün ve gerekirse tek hamlede geri alınsın.
- Yerel test için gerçek Google hesabı açılmadı; imzalı test çerezi kullanıldı.

## 6 Eylül 2026 – resmî önlem listeleri, günlük eşitleme, TCMB kuru, Eylemio

- `exchange_rates.py`: TCMB bülteni (today.xml / arşiv), tescil tarihinden önceki son iş günü kuralı, döviz satış kuru; `/api/tariff/exchange-rate`, MCP `get_customs_exchange_rate`, formlarda "TCMB kurunu getir".
- `trade_measures.py`: damping/sübvansiyon (Bakanlık xlsx), korunma (Bakanlık xlsx), gözetim (mevzuat.gov.tr fihrist metinleri) ve İthalat Tebliğleri dizini; `data/official/*.json` tohumları, `trade_measures.sqlite3` anlık görüntü + değişiklik defteri; `TradeMeasureEngine.periodic_sync_loop` günde bir çalışır (`TRADE_MEASURES_SYNC_INTERVAL_SECONDS`). Tarife lookup sonucuna `trade_measures`, maliyet hesabına uyarılar ve kapsam durumu eklendi.
- Resmî veri sandbox üzerinden çekildi: mevzuat.gov.tr ara sertifika (GeoTrust) göndermediği için `trusted_certificates` ile doğrulama; gözetim metinlerinin 193/194'ü tohumda (856 satır / 851 GTİP; 10 metinde tablo yok); tarım tarife kontenjanları 13 karar / 428 satır (.docx), .doc ekleri sunucuda antiword ile günlük eşitlemede (site 150 istek sonrası yavaşlıyor; eşitleme istekler arasında bekler).
- `eylemio_client.py`: Eylemio `ticaret-beyanname` konektörü ile beyanname durumu (login → hesap listesi → read). Depo bulunamadığı için API şekli canlı siteden çıkarıldı; yanıt alanları genel olarak gösterilir.
- Testler: 183 (exchange_rates 6, trade_measures 11, eylemio 4).
- Tarife kontenjanı ekleri: 21 ülke / 863 satır tohumda; `.doc` dosyaları `olefile` ile saf Python'da okunuyor (antiword yalnız son çare), başlık eşleştirmesi caption sonuna bakıyor ve kısa/kaymış satırları hizalıyor.
