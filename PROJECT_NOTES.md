# Proje Devir Notu — Ticaret Bilgi Masası / mevzuat-mcp

Son güncelleme: 15 Ekim 2026 (Europe/Istanbul)

## 15 Ekim 2026 — Külliyat PR5: GTİP örnek görselleri (dal `feat/kulliyat-gtip-gorseller`, PR4 üstünde)

- Vergi/tarife sonucunun altında "Örnek görseller" kutusu (GTİP en az 6 hane). `GET /api/gtip/images`
  Türkiye BTB kayıtlarını (aynı GTİP önce, sonra 10/8/6 hane öneki; en çok 6) ve AB EBTI kararlarını döndürür.
- **TR BTB fotoğrafı doğrulandı:** `uygulama.gtb.gov.tr/BTBBasvuru/grdpht.ashx?ImID=<BTB no>` image/jpeg
  (3024x4032, ~2,3 MB); fotoğrafı olmayan kayıtta 302 döner, bu yüzden varlık önceden bilinemez.
  Görsel **saklanmaz, proxylenmez**: kullanıcı "Fotoğrafı göster" deyince tarayıcı doğrudan Bakanlıktan
  yükler (`referrerpolicy=no-referrer`, eager; lazy ekran dışında hiç başlamıyordu), hata olursa "fotoğraf
  bulunamadı" der. Sitede kullanım koşulu/telif metni bulunamadı, robots.txt yok (404); yine de kopyalanmıyor.
- **AB EBTI görseli doğrulanamadı:** `ebti_details.jsp` ayrıntı sayfası 15 Ekim'de HTTP 500 (gerçek bir
  DEBTI referansıyla denendi) ve CSV'de görsel alanı yok; bu yüzden görsel adresi **uydurulmadı**
  (`image_url: null`), yalnız resmî karar bağlantısı + "görsel adresi doğrulanamadı" notu verilir. EBTI
  servisi düzelince adres kalıbı ölçülüp eklenmeli. Not: `parse_decisions` CSV başlıkları görsel içermiyor.
- Gizlilik/uyarı: TR için "BTB yalnız başvuru sahibini bağlar, görseller örnek niteliğinde"; AB için
  "Türkiye'de bağlayıcı değildir". AB sonuçları `foreign_tariff` kilidine tabidir (giriş yoksa "locked").
- Dağıtım notu: uygulamada CSP başlığı yok; ters proxy/Coolify tarafında `img-src` kısıtı varsa
  `uygulama.gtb.gov.tr` izinli olmalı.
- Testler: `tests/test_gtip_images.py` (7). Tam paket TZ=UTC: 1672 test, hata yok. Gerçek veriyle elle doğrulandı.
- Külliyat planı (PR1-PR5) tamam. Açık iş: resmî risk kriterleri ve 2002+ arşivi, soru bankası genişletme,
  EBTI görsel adresi, `data/official/question_bank.json` ve `risk_criteria.json` editoryal doldurma.

## 15 Ekim 2026 — Külliyat PR4: Eğitim soru bankası + mütalaa talebi (dal `feat/kulliyat-egitim-mutalaa`, PR3 üstünde)

- `education.py` + `data/official/question_bank.json`: her soru mevzuat dayanağı ve https kaynakla
  yüklenir (yoksa reddedilir). Cevap anahtarı yalnız cevaptan/teslimden sonra döner. Süreli sınav
  süresi sunucuda tutulur (20 sn tolerans, aşım "süre aşıldı" etiketlenir). "Yanlışlarım" = sorudaki
  son deneme yanlışsa. API: `/api/education/{status,practice,answer,exams,exams/{id}/submit,wrong}`,
  yönetici için `POST /api/education/questions`.
- **Veri boşluğu:** tohum yalnız **9 tarife/BTB sorusu**; hepsi Bakanlığın BTB SSS sayfasından
  doğrulandı. Kıymet, menşe, rejimler ve mevzuat branşları boş (uydurma soru eklenmedi). Editoryal
  olarak kaynaklı soru eklenmeli (yönetici POST veya tohum dosyası).
- Mütalaa: yeni uç yok; mevcut `/api/consultation-requests` akışı kullanılır. Konu `[Mütalaa] [ACİL]`
  önekiyle gider, `_consultation_packet` yalnız izinli `mutalaa` alanlarını taşır (aciliyet, GTİP,
  belge notu). Dosya eki bilinçli olarak yok (paket gizlilik kuralı). Durum takibi: Mütalaa sekmesi
  gönderilen taleplerin durumunu listeler; mesajlaşma Gümrük Danışmanları sekmesinde.
- Hata yakalandı: `"ACİL".lower() != "acil"` (noktalı İ); aciliyet artık `turkish_text.fold` ile okunur.
- Testler: `tests/test_education.py` (14). Tam paket TZ=UTC: 1660 test, hata yok. Yerelde alıştırma
  akışı elle doğrulandı; sınav ve talep gönderimi Google girişi gerektirdiği için API testleriyle kapsandı.
- Sıradaki: PR5 GTİP örnek görselleri (TR BTB fotoğrafı `grdpht.ashx` + AB EBTI).

## 15 Ekim 2026 — Külliyat PR3: BTB (TR) arama + eşya fihristi (dal `feat/kulliyat-btb-fihrist`, PR2 üstünde)

- Kaynak: Ticaret Bakanlığı BTB Başvuru Sistemi, halka açık ekranlar (`uygulama.gtb.gov.tr/BTBBasvuru`:
  `Btbler` geçerli kayıt listesi, `BtbWebArama` ayrıntı). robots.txt yok (404). ASP.NET WebForms:
  sayfalama `__doPostBack` ile, olay doğrulaması yalnız o anki sayfanın pager bağlantılarını kabul eder;
  bu yüzden sayfalar zincirleme gezilir, bitiş = pager'da sonraki sayfa bağlantısı yok.
  Canlı tam eşitleme: **2.445 geçerli BTB**, ~2 dk, 1 istek/sn.
- **Düzeltme (KULLIYAT_GOREV.md PR5 notu yanlış):** TR BTB'de görsel VAR. "Fotoğraf Görünümü" ve
  `grdpht.ashx?ImID=<BTB no>` JPEG döndürür (ölçüldü: image/jpeg, ~2,3 MB). Bu PR yalnız adresi üretir
  ve "Resmî fotoğraf" bağlantısı verir; dosya indirilmez/saklanmaz. PR5 bunu kullanmalı (lisans/kullanım
  koşulu kontrolü hâlâ gerekli).
- `btb_client.py`: `BtbStore` (SQLite `btb_rulings`), `BtbEngine.sync` (haftalık döngü `btb-sync`,
  `BTB_SYNC_ENABLED=0` ile kapanır), `fetch_detail` (tam tanım + sınıflandırma gerekçesi, talep üzerine
  bir kez alınıp saklanır). Liste tanımı ~25 karakterde kesik gelir ("kısaltılmış" etiketi). Liste %80'in
  altına düşerse eşitleme uygulanmaz; iki eşitlemede görünmeyen kayıt pasif olur, silinmez.
- API: `GET /api/btb/search` (TR BTB + AB EBTI + resmî eşya tanımı yan yana; EBTI `foreign_tariff` kilidine
  tabi), `GET /api/btb/{no}`, `GET /api/btb/status`, `GET /api/fihrist` (6 haneli alt pozisyonlar, Türkçe
  alfabe sırası, oran vermez).
- Ekran: "BTB / Fihrist" sekmesi (üç sütun, "Gerekçeyi göster", "Resmî fotoğraf", A-Z fihrist, "Vergi
  hesabında kullan").
- Testler: `tests/test_btb_client.py` (15, ağsız). Tam paket TZ=UTC hatasız.
- Sıradaki: PR4 Eğitim + Mütalaa, PR5 görseller (TR BTB fotoğrafı dahil).

## 15 Ekim 2026 — Külliyat PR2: Risk ön kontrolü (dal `feat/kulliyat-risk-kontrol`, PR1 dalının üstünde)

- Rapor: "Gümrük Külliyatı REA İncelemesi" (14 Ekim); eksik özellik listesi `KULLIYAT_GOREV.md` ile aynı.
  Rapor ilkeleri uygulandı: açıklanabilir sonuç, varsayım yok, yalnız birincil kaynak.
- `risk_engine.py`: `RiskStore` (SQLite `risk_criteria`) + `automatic_signals` + sabit `assess_level`
  (yüksek sinyal → Yüksek; orta sinyal veya resmî kriter → Orta; yalnız not → Bilgi).
  Resmî kriter yalnız yönetici ve https kaynak bağlantısıyla eklenir; kullanıcı kayıtları
  "kullanıcı notu (resmî değildir)" etiketlidir ve yalnız sahibine görünür.
- API: `POST /api/risk/check`, `POST /api/risk/criteria`, `DELETE /api/risk/criteria/{id}`.
  Otomatik sinyaller mevcut `trade_measure_engine` (damping, korunma, gözetim, kontenjan) ve
  `control_engine` (TAREKS/TSE) çıktısından türetilir; yeni oran uydurulmaz. Menşe bilinmiyorsa
  damping "menşe doğrulanmalı" olarak orta düzeyde kalır.
- **Veri boşluğu:** `data/official/risk_criteria.json` bilinçli olarak boş; resmî risk kriterleri (2002+)
  editoryal olarak kaynaklı eklenmeli (yönetici POST ile veya tohum dosyası). Kaynaksız kayıt reddedilir.
- Ekran: "Risk Kontrol" sekmesi (GTİP, menşe, yön, yıl; seviye rozeti; sinyaller; kriterler; kendi notum).
- Testler: `tests/test_risk_engine.py` (14). Tam paket TZ=UTC: 1625 test, hata yok.
- Sıradaki: PR3 BTB / eşya fihristi (`KULLIYAT_GOREV.md`).

## 15 Ekim 2026 — Külliyat PR1: düzen ve vergi hesap ekranı (dal `feat/kulliyat-duzeni-vergi-ekrani`)

- Kaynak görev: `KULLIYAT_GOREV.md` (5 PR'lık plan; bu PR1). Taban `a3958c9`. Merge/deploy yok.
- Motor: `calculate_landed_cost` her satıra `explanation` ekler (`source`, `source_detail`,
  `legal_basis`, `base_components`); aritmetik değişmedi. `TariffEngine.calculate` resmî
  tarifeden gelen GV/İGV/EMY satırlarını `official_tariff` (veya doğrulanmış uygulanmazlık
  için `official_not_applicable`) diye işaretler ve snapshot başlık/sürüm/SHA bilgisini ekler.
- Arayüz (vanilla SPA korundu): Vergi formu 4 gruba ayrıldı (Eşya, Kıymet, Belge soruları,
  Ek yükler); zorunlu alanlar yeşil, isteğe bağlılar mavi sol çerçeve. A.TR ve gözetim belgesi
  Evet/Hayır/Bilmiyorum. Her maliyet satırında "Neden?" paneli. Üstte bilgi şeridi
  (TCMB USD kuru, tarife sürümü, değişiklik kaydı; okunamazsa "—"). Solda mevzuat ağacı
  (yalnız mevcut hızlı arama sorguları). Menüde Risk Kontrol, BTB / Fihrist, Eğitim, Mütalaa
  "yakında" olarak devre dışı; PR2-PR4'te açılacak.
- Testler: `tests/test_cost_explanations.py` (4), `tests/test_kulliyat_layout.py` (4).
  Tam paket TZ=UTC: 1606 test, 10 atlama, hata yok. Yerelde 8765 portunda elle kontrol
  edildi (masaüstü ve 360 px, yatay taşma yok).
- Sıradaki: PR2 Risk ön kontrolü (`KULLIYAT_GOREV.md`).

## 15 Ekim 2026 — PR #104 gözetim giriş yolları entegrasyonu

- İnceleme P1/P2 bulguları `d38108c` üzerinde doğrulandı. Tarife ve danışman web
  formlarına iki birim, gözetim dövizi ve çapraz kur alanları eklendi; bilinmeyen
  birim varsayılmıyor. `CustomsInquiry`, deterministik maliyet ve MCP ön kontrol
  aracı aynı alanları taşır. Maliyet alanları ve karar soruları birim/kur cevaplarını
  kullanıcıdan alır; geçersiz/silinen çapraz kur cevabı eski değeri kullanmaz.
- Açıklamalı CSV/XLSX başlıkları en uzun özel alias ile eşleşir; genel “Gözetim”
  başlığı artık döviz veya birim alanını tüketmez. Noktalı çapraz kur ondalık kalır.
- Yeni `tests/test_surveillance_product_paths.py`: gerçek HTML kontrolleri ve
  üretim JavaScript gönderim/karar/odak olayları Node VM'de yürütülür; çıkan
  payload çevrimdışı ASGI API'ye ve gerçek aritmetiğe verilir. Danışman HTTP/MCP
  yollarında LLM ve dış kaynak toplayıcılar taklit edilir; hesap ve modeller gerçektir.
- 12 yeni test başarılı. Tam paket: **1.602 test; 1.592 başarılı, 10 atlama,
  hata yok**. Node sözdizimi, Python derleme ve diff kontrolleri başarılı.
- Aynı PR: https://github.com/hiktan44/gumruksor/pull/104 . Merge/deploy yok;
  mevcut kullanıcı değişiklikleri ve ilk güvenlik kontrolleri korundu.

## 15 Ekim 2026 — EMY, toplu gözetim ve yürürlük kapsamı yeniden incelemesi

- Taban `main`: `19b1a0b24a49fdce1c6261bc4e23c6437217d4cf`; doğru remote
  `hiktan44/gumruksor`. Yeni temiz klonda `fix/tariff-data-completeness` dalı.
  PR #101 birleşmiş; açık PR #103 yalnız test/rapor ekliyor. Üç risk hâlâ mevcuttu.
- Kısmi/eksik EMY kapsamındaki satır yokluğu veya kaynak sorgu hatası artık
  uygulanmazlık/%0 sayılmaz. Açık resmî sıfır ve kullanıcıca doğrulanan oran korunur.
- Toplu JSON/CSV/XLSX gözetim dövizi, çapraz kur ve iki birimi taşır. Eksik birim,
  kur veya belge durumunda nihai toplam verilmez. Sentetik 100 kg × 20.000 USD/ton
  örneği 2.000 USD taban verir; bu bir hukuki oran önerisi değildir.
- Etkin snapshot sorgu gününü kapsamıyorsa yalnız günü kapsayan onaylı eski sürüm
  seçilir. Böyle bir sürüm yoksa tarih/kaynak eksikliği açıkça bildirilir.
- 19 yeni regresyon başlangıç kodunda 35 başarısız assertion/alt senaryo ve
  1 metadata sorgu hatası üretti; düzeltmelerle 19/19 geçiyor. Tam paket:
  **1.590 test, 1.580 başarılı, 10 atlama, hata yok**. Atlamalar: 9 canlı önbellek,
  1 Chromium PDF testi. Geçici veri, temiz ortam ve dış ağ/DNS engeli kullanıldı.
- Yeniden yürütme: `.venv/bin/python scripts/run_offline_tests.py discover -s tests -v`.
  Ayrıntı: `docs/tariff-data-safety-verification.md`. Gerçek müşteri/ERP/LLM veya
  üretim kaydı kullanılmadı; merge ve deploy yapılmadı. Yerel container/Trivy yok.

## 15 Ekim 2026 — Faz 0: vergi hesabı düzeltmeleri (dal `fix/faz0-vergi-motoru`)

- Kaynak: "GümrükSor Üzerine Külliyat + Mevzuat.Net Yapısı ve Sağlam Vergi Motoru"
  çalışmasındaki Faz 0 listesi. Taban `aed1b07`. Merge/deploy yapılmadı.
- `calculate_import_landed_cost` MCP aracı tanımsız `as_of` ile her çağrıda NameError
  veriyordu; `as_of` parametresi eklendi ve motora iletiliyor.
- KKDF: yeni `payment_terms.classify_payment_method` kelime sınırı ve beyanname ödeme
  kodlarıyla çalışır. "akreditif" artık %6 almaz (vadeli akreditif alır); belirsiz
  metinde (akreditif, vesaik mukabili) oran kullanıcıya sorulur.
- İleri tarihli tarife snapshot'ı `valid_from` gelmeden bugünkü hesaba girmez; o güne
  kadar önceki onaylı sürüm kullanılır. "Bugün" artık Türkiye saatiyle
  (`temporal.today_local`, sabit UTC+3); TCMB kur varsayılan tarihi de buna bağlandı.
- EMY: motor resmî listede EMY bulamayıp "uygulanmaz (%0)" dediğinde hesap %0 alır
  (İGV ile aynı kural). `TariffLookupResult.not_applicable_measures` yapısal alanı
  eklendi; tasarruf modülü uyarı metni yerine bu alanı okur.
- Kullanıcı/resmî oran çakışması tek yardımcıda (`rate_override_conflicts`); ön kontrol
  yolu artık sessiz değil. Tasarruf karşılaştırması bilinçli olarak resmî oranı kullanır.
- Gözetim: eşik dövizi (`surveillance_currency`, varsayılan USD) fatura dövizinden
  farklıysa çapraz kur ister; `surveillance_unit` / `quantity_unit` ile kg↔ton çevrilir,
  uyumsuz birimde kıymet sessizce yükseltilmez.
- Toplu hesap ve tasarruf rotaları `as_of` iletir; toplu hesapta istek ve satır düzeyinde
  A.TR beyanı desteklenir. Oran/kur alanlarında "41.250" ondalık okunur; tutar
  alanlarında Türkçe binlik kuralı korunur ve satıra açıklama notu düşer. FOB sütununda
  navlun/sigorta boşsa uyarı verilir.
- Testler: `tests/test_faz0_tax_fixes.py` (18 yeni test). İki mevcut test beklenen davranış
  değişikliğine göre güncellendi (toplu hesapta EMY %0, senaryo satır alanları).
  Toplu paket TZ=UTC: **1571 test, 10 atlandı, hata yok**. Not: TZ=Europe/Istanbul ile
  gece yarısı civarında `test_eu_vat.test_a_stale_confirmation_brings_the_warning_back`
  main dalında da başarısız; bu değişiklikle ilgisiz, önceden var olan tarih sınırı sorunu.

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

## 15 Ekim 2026 – Külliyat veri dolum araçları (dal feat/kulliyat-veri-dolum, PR5 üstüne)

Veri uzman kaynaklı girilir; bu dal yalnız dolum ve onay araçlarını getirir. Risk kriteri ve soru uydurulmaz.
- **Risk kriterleri** (`risk_engine.py`, `risk_import.py`): `status` (draft/approved/rejected), `kind` (criterion/precedent), `verified_at`. Yalnız onaylı resmî kayıtlar (ve sahibinin notları) `check`e girer. CSV: `GET /api/risk/review/template` (yalnız yönetici), `POST /api/risk/review/import` (`{"csv","dry_run"}`; tek hata varsa hiçbir satır yazılmaz, satır satır rapor, hatasız dosya taslak yazılır). Onay: `GET /api/risk/review`, `POST /api/risk/review/{id}/approve|reject` (yönetici veya aktif danışman; onayda doğrulama tarihi zorunlu, onaylı kayıtta tarih yenilenir). 12 aydan eski ya da tarihsiz kayıt `stale` uyarısı taşır. `POST /api/risk/criteria` ile resmî ekleme artık taslak açar.
- **Soru bankası** (`education.py`): `questions.status` (eski veritabanları migrasyonla `approved` kalır), taslak/reddedilen soru öğrenciye hiçbir uçta görünmez ve kimlikle cevaplanamaz. Uçlar: `GET /api/education/review`, `POST /api/education/review/{id}/approve|reject`, `POST /api/education/drafts/import` (yönetici; hep draft). Eğitim sekmesinde "Soru onayı (uzman)" modu `auth/me.user.can_review` ile yetkililere görünür; Risk sekmesinde "Kriter onayı ve toplu yükleme" kutusu.
- **Soru taslağı betiği** `scripts/soru_taslagi_uret.py`: mevzuat.gov.tr madde metni (`data/official/question_sources.json`, madde no metinden ayrıştırılır) + Bedesten karar metni (`ictihat.IctihatArchive._post`: seri, 429'da bekler). Soruyu yazan model `QUESTION_LLM_BASE_URL/API_KEY/MODEL` (OpenAI uyumlu) ile verilir; model yoksa soru üretilmez. Kaynak metinde birebir geçmeyen alıntı ve kaynakta olmayan sayı otomatik reddedilir (`--reddedilen`). Karar kaynaklı sorular `source_kind='karar'` ve "Emsal karar; bağlayıcı olmayabilir." uyarısı taşır. Canlıda henüz çalıştırılmadı; ilk dalga branş başına 10-15 taslak `--hedef 12` ile üretilip uzman onayına sunulacak.
- **Emsal notu** `scripts/emsal_notu_aktar.py`: ictihat arşivinden kıymet/tarife kararlarını `emsal` kategorisiyle taslak aktarır; resmî kriter değildir (`official=0`), seviyeyi yükseltmez, etiket "Yargı emsali (resmî kriter değildir; bağlayıcı olmayabilir)". Kararın sonucu yorumlanmaz.
- Bekleyen: gerçek uzman onayıyla ilk resmî kriter CSV'si, canlı üretim koşusu için model anahtarı, `pyproject` data-files'a eklenen `question_sources.json` Docker imajında doğrulanmalı.

## 15 Ekim 2026 — Abone raporu sayaç ucu (yerel geliştirme)
GET /api/admin/subscriber-summary?start=&end= mevcut _require_admin korumasıyla eklendi. Yalnız toplam/yeni users sayısını okur; müşteri satırı yok, ücretli abone/oturum/süre alanları bilinmiyor/null. Saat dilimli ISO, en fazla31gün, [start,end) uygulanır. Yeni token/rol/erişim veya üretim yayını yok. 3 SQLite hesaplama +3 çevrimdışı ASGI HTTP testinde dönem sınırları, 403 yönetici koruması, 400 veri doğrulama, no-store ve kişisel veri taşımama geçti. ASGI TestClient çıktılarındaki gumruksor.com adresi yerel test origin'idir; gerçek canlı istek değildir. Kullanıcının asıl repo/çalışma dalı değiştirilmedi.
