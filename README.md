# Türkiye Mevzuat ve Ticaret Bakanlığı Bilgi MCP Sunucusu

Bu proje; Adalet Bakanlığı Mevzuat Bilgi Sistemi, Bedesten Mevzuat servisi ve Ticaret Bakanlığının resmî bilgi kaynaklarını tek bir [FastMCP](https://gofastmcp.com/) sunucusunda birleştirir. Kanun, karar, yönetmelik ve tebliğlerin yanında gümrük, ithalat-ihracat, devlet destekleri, istatistikler, yayınlar, ülke-pazar raporları ve ticaret müşavirliği/ataşeliği bilgileri ChatGPT, Codex ve diğer MCP istemcilerince aranabilir ve analiz edilebilir.

<a href="https://glama.ai/mcp/servers/@saidsurucu/mevzuat-mcp">
  <img width="380" height="200" src="https://glama.ai/mcp/servers/@saidsurucu/mevzuat-mcp/badge" alt="Mevzuat MCP server" />
</a>

![örnek](./ornek.png)

🎯 **Temel Özellikler**

* Adalet Bakanlığı Mevzuat Bilgi Sistemi'ne programatik erişim için standart bir MCP arayüzü.
* **44 farklı tool** ile kapsamlı mevzuat, tarife ve Ticaret Bakanlığı bilgi erişimi (üç resmî kaynak ailesi):
    * **mevzuat.gov.tr** üzerinden 21 araç (türe özel arama ve içerik)
    * **bedesten.adalet.gov.tr** üzerinden 5 araç (birleşik arama, gerekçe, içindekiler)
    * **ticaret.gov.tr** ve bağlı resmî alt alanlardan 18 araç (canlı katalog, belge okuma, resmî tarife/İGV, maliyet, ürün kontrol tebliğleri, ürün fotoğrafı evsaf çıkarımı ve gümrük ön değerlendirme kanıtı)
* Desteklenen 12 mevzuat türü:
    * **Kanun** - Türkiye Cumhuriyeti kanunları
    * **KHK** - Kanun Hükmünde Kararnameler
    * **Tüzük** - Tüzükler
    * **Kurum Yönetmeliği** - Kurum ve kuruluş yönetmelikleri
    * **Üniversite Yönetmeliği** - Üniversite yönetmelikleri
    * **Cumhurbaşkanlığı Kararnamesi** - Cumhurbaşkanlığı kararnameleri
    * **Cumhurbaşkanı Kararı** - Cumhurbaşkanı kararları
    * **CB Yönetmeliği** - Cumhurbaşkanlığı ve Bakanlar Kurulu yönetmelikleri
    * **CB Genelgesi** - Cumhurbaşkanlığı genelgeleri
    * **Bakanlar Kurulu Yönetmeliği** - Bakanlar Kurulu yönetmelikleri
    * **Tebliğ** - Tebliğler
    * **Mülga Mevzuat** - Yürürlükten kaldırılmış mevzuat
* **mevzuat.gov.tr araçları (21 tool)**: Her mevzuat türü için çift tool yapısı:
    * **Arama tool'u**: Başlık ve içerikte arama, Boolean operatörler (AND, OR, NOT), tarih filtreleme
    * **İçinde arama tool'u**: Madde bazında arama (keyword + semantik), alakalılık skoru ile sıralama
* **bedesten.adalet.gov.tr araçları (5 tool)**: Tüm mevzuat türlerini tek araçla kapsar:
    * **`search_mevzuat`**: 12 türde birleşik arama (başlık, içerik, numara, RG tarihi/sayısı filtreleme)
    * **`get_mevzuat_content`**: Tam metin getirme
    * **`search_within_mevzuat`**: Madde bazında anahtar kelime araması
    * **`get_mevzuat_gerekce`**: Kanun gerekçesi (amaç, komisyon raporları, madde gerekçeleri)
    * **`get_mevzuat_madde_tree`**: İçindekiler / madde ağacı (bölüm-madde hiyerarşisi)
* **Semantik Arama**: Tüm 9 `search_within_*` aracında `semantic=True` parametresi ile doğal dilde anlam tabanlı arama. OpenRouter API üzerinden embedding modelleri kullanır.
* Gelişmiş özellikler:
    * PDF'leri Mistral OCR ile metin çıkarma (CB Kararı ve CB Genelgesi için)
    * HTML'den Markdown'a otomatik dönüştürme
    * In-memory caching (1 saat TTL) ile hızlı erişim
    * Boolean arama operatörleri (AND, OR, NOT)
    * Tam cümle araması (exact phrase)
    * Tarih aralığı filtreleme
* Claude Desktop ve 5ire gibi MCP istemcileri ile kolay entegrasyon

## Ticaret Bakanlığı bilgi katmanları

Kaynak listesi `ticaret_sources.json` dosyasında ayrı ve elle düzenlenebilir tutulur; başlıklar, sektörler, ülke adları ve belge bilgileri canlı sayfalardan dinamik olarak çıkarılır. Gümrük, ithalat, ihracat, destekler ve İthalat Genel Müdürlüğü duyuruları saatlik; bütün katalog altı saatte bir yenilenir. Ayrıca Adalet Bakanlığı Bedesten resmî mevzuat servisindeki son 45 günlük Resmî Gazete kayıtları her saat taranarak Bakanlık sayfalarındaki yayımlama gecikmesi kapatılır. `TICARET_PRIORITY_SYNC_INTERVAL_SECONDS`, `TICARET_SYNC_INTERVAL_SECONDS`, `TICARET_SOURCES_FILE` ve `TICARET_EXTRA_SOURCES_JSON` değişkenleriyle dağıtım yeniden kod yazmadan özelleştirilebilir.

| Katman | `content_kind` | Kapsam |
|---|---|---|
| Mevzuat | `mevzuat` | Gümrük, ithalat, ihracat, iç ticaret, tüketici, ürün güvenliği, serbest bölgeler, hizmet ticareti, esnaf/kooperatif ve ürün kuralları |
| Devlet destekleri | `destek` | Destek kararları, uygulama esasları, genelgeler, program ve başvuru sayfaları |
| İstatistik ve veri | `veri` | Bakanlık istatistikleri ve resmî veri kaynaklarına açılan bağlantılar |
| Müşavirlik/pazar raporları | `rapor` | Ticaret müşavirleri ve ataşelerden gelen ülke, sektör, pazar, ihale ve ticari bilgi içerikleri |
| Ülke/pazar bilgileri | `ulke_bilgisi` | Yurt dışı teşkilatı ülke sayfaları ve doğrudan yayımlanan ülke belgeleri |
| İletişim | `iletisim` | Müşavirlik/ataşelik listeleri, adres ve iletişim servisleri |
| Bakanlık yayınları | `yayin` | Faaliyet, strateji, performans ve diğer Bakanlık rapor/yayınları |

Yeni araçlar:

* `list_ticaret_sources`: katmanları, resmî başlangıç URL'lerini ve canlı kayıt sayılarını gösterir.
* `search_ticaret_catalog`: katman, kaynak, belge türü, yıl ve mülga durumu filtreleriyle arar.
* `get_ticaret_document`: resmî HTML/PDF/DOCX/XLSX/CSV/ZIP içeriklerini güvenli sınırlar içinde getirir.
* `search_ticaret_content`: seçilen en fazla 25 belgenin tam metninde bağlamlı arama yapar.
* `get_ticaret_catalog_status`: son yenileme, sonraki tarama, kapsam parmak izi ve kaynak hatalarını verir.
* `describe_product_image`: ürün fotoğrafından yalnızca görünür evsafları (malzeme ipuçları, renkler, bileşenler, etiket metni, ambalaj) ve sınıflandırmayı Engelleyen eksik soruları çıkarır. Sonuç GTİP değildir; kullanıcı evsafı onayladıktan sonra `suggest_candidate_tariff_codes` çağrılır.
* `prepare_customs_precheck`: ürün, aday GTİP, menşe ve maliyet girdileri için TAREKS/TSE, ürün güvenliği, kimyasallar, gümrük kıymeti, vergi ve ticaret politikası önlemlerine ilişkin tarihli resmî kanıt paketi hazırlar.
* `sync_official_tariff_data`, `lookup_tariff_measures`, `resolve_turkish_tariff_tree`, `calculate_import_landed_cost`, `compare_tariff_snapshots`: 2026 İthalat Rejimi ve İGV arşivlerini SHA-256 ile sürümler; GTİP/menşe sütununu kaynak dosya, sayfa ve satır düzeyinde gösterir. Karar ağacı HS6 → CN8 → Türkiye 10 → GTİP12 dallarını otomatik seçim yapmadan açar; kısa kodda oran yalnız bütün alt GTİP12 satırlarında ortaksa güvenli oran olarak döner.
* `sync_classification_evidence`, `search_classification_evidence`: DG TAXUD'un geçerli AB sınıflandırma tüzükleri konsolide listesini metin olarak SHA-256 ile sürümler; CN kodu, tüzük referansı, sayfa ve gerekçe parçalarını aranabilir yapar. Bu veri yalnız karşılaştırmalı sınıflandırma kanıtıdır; Türkiye GTİP12 veya Türkiye vergi oranı değildir. EBTI sonuç sayfaları ve açık kullanım hakkı doğrulanmamış başvuru fotoğrafları otomatik taranmaz.
* `sync_import_control_rules`, `lookup_import_controls`, `compare_import_control_snapshots`: güncel Ürün Güvenliği ve Denetimi tebliğlerinin kaynakta belirtilen kapsam eklerini resmî konsolide metin veya resmî ek arşivinden indeksler; liste kapsamını TAREKS risk sonucu ve fiilî denetimden ayırır. 2026/21 için yalnız kapsam oluşturan Ek-1/A–D ile Ek-2 alınır; form ekleri dışarıda bırakılır ve 168 GTİP satırı indekslenir.

Bir tebliğin GTİP listesi birden çok ekte ise (`Ek-1/A`, `Ek-1/B` parçaları otomatik birleştirilir) `control_sources.json` içinde `"scope_annexes": [1, {"annex": 2, "kind": "prohibited"}]` ile ek listesi ve **ithali yasak** listesi ayrı ayrı tanımlanır; sorgu sonucunda yasak liste eşleşmesi `list_kind: prohibited` ile ayrı döner.

Kontrol tebliğlerinin ilk soğuk eşitlemesi, resmî Bedesten hız sınırına saygı göstermek için tek tek ve aralıklı yapılır; birkaç dakika sürebilir. Sonraki sorgular `/data` içindeki snapshot'tan yanıtlanır ve altı saatte bir güncellenir. `CONTROL_REQUEST_INTERVAL_SECONDS` varsayılanı `6.5` saniyedir; resmî servisin sınırını aşacak şekilde düşürülmemelidir.

Mevzuat/Resmî Gazete sunucusu geçerli TLS uç sertifikasıyla birlikte zaman zaman ara sertifikayı göndermediği için uygulama yalnızca DigiCert tarafından yayımlanan **GeoTrust TLS RSA CA G1** ara sertifikasını varsayılan güven zincirine ekler. Kök güveni, alan adı kontrolü, imza ve geçerlilik tarihleri kapatılmaz; `verify=False` kullanılmaz.

## Gümrükçe’ye Sor

Web arayüzündeki **Gümrükçe’ye Sor** sekmesi üç güvenlik aşaması kullanır. Fotoğraf cihazda önizlendikten sonra görünen **Ürünü Analiz Et** düğmesiyle kullanıcı analizi açıkça başlatır; görsel bu eylemden önce sunucuya gönderilmez. Fotoğraf yalnızca ürün evsafına çevrilir; tanım, görünen marka/model, ölçü, etiket metni, görünen ve belirsiz özellikler düzenlenebilir satırlara gelir. Kullanıcı bu alanları açıkça onaylamadan sınıflandırma başlamaz. En fazla beş **aday** kod gösterilir fakat ilk aday otomatik seçilmez. Her aday aktif Türk tarife satırında doğrulanır ve varsa resmî AB sınıflandırma tüzüğü sayfalarıyla desteklenir. Kullanıcı bir aday seçince resmî HS6 → CN8 → Türkiye 10 → GTİP12 ağacı adım adım açılır; TAREKS kapsam sorgusu yalnız doğrulanmış 12 haneli satırda çalışır. Vergi oranları, kısa kod altında bütün GTİP12 satırlarında ortaksa gösterilir; menşe/dipnot bakımından ayrışan oranlar kesin sonuç gibi sunulmaz.

Çalışma masasında ayrıca **Tarife & Maliyet**, **Kontroller & Belgeler**, **Değişiklikler**, **Gümrük Danışmanları** ve **İşlem Rehberi** sekmeleri bulunur. Tarife ekranı resmî workbook/sheet/row ve arşiv checksum'unu; kontrol ekranı tebliğ Ek-1 satırını, yetkili sistemi ve risk uyarısını; değişiklik ekranı snapshot farklarını, cihazdaki GTİP izleme listesini ve kaydedilmiş ön değerlendirmeleri gösterir. Menşe ülke ile sevk ülkesi ayrı girilir: AB'den A.TR ile gelen üçüncü ülke menşeli eşyada gümrük vergisi AB sütunundan (A.TR ibrazına bağlı), İGV ve ek mali yükümlülük menşe sütunundan değerlendirilir; AB/STA menşeli eşyada İGV/EMY tercihi tedarikçi beyanı veya menşe belgesi tevsikine bağlı olarak işaretlenir ve tevsik yoksa uygulanacak "Diğer Ülkeler" oranı gösterilir. Menşe belgesi kuralı fasıla bakar: 1-24. fasıl temel tarım ürünü ve AKÇT kömür-çelik ürünleri için A.TR yerine EUR.1; Birleşik Krallık, Güney Kore ve Singapur için menşe beyanı. Ülke adları `countries.py` kayıt defterinden (Türkçe/İngilizce) çözümlenir; tanınmayan ad uyarı üretir. Tarife ekranındaki **Menşe senaryoları** paneli aynı GTİP için menşe ülkesi başına resmî vergi sütununu, güvenli oranı ve A.TR/EUR.1/menşe şahadetnamesi belge kuralını yan yana karşılaştırır (`/api/tariff/scenarios`; kural tablosu `origin_documents.py`, resmî yürürlükteki STA listesine dayanır). Aynı paneldeki **Tasarruf önerisi** düğmesi (`/api/tariff/savings`; satır üretimi `scenarios.py`, saf sıralama mantığı `savings.py`) Tarife & Maliyet formundaki maliyet kalemlerini (fatura, navlun, sigorta, KDV, KKDF, ÖTV, damping, gözetim, EMY, TL kalemleri) her menşe için aynı maliyet motorundan geçirir, A.TR'ye uygun rotalarda (AB'den sevk edilen üçüncü ülke menşeli sanayi ürünü) ayrıca "A.TR ile" varyantını hesaplar ve senaryoları toplam ithalat maliyetine göre sıralayıp temel senaryoya (varsayılan: formdaki menşe) göre tasarrufu gösterir. Karşılaştırılabilirlik kuralları bilinçli olarak dardır: yalnız kesin eşleşen tarife satırı (`matched`) ve tanınan menşe sıralamaya girer; alt GTİP satırlarında değişen (belirsiz) oran, tanınmayan menşe veya doğrulanmamış maliyet kalemi olan senaryo "karşılaştırılamayan" listesine sebebiyle düşer; resmî listede bulunmayan kalem sessizce 0 sayılmaz (motor listede olmadığını açıkça söylüyorsa 0, aksi hâlde kullanıcının girdiği oran kullanılır ve not düşülür); resmî sütun oranı kullanıcı oranından farklıysa senaryoda resmî oran esas alınır ve fark belirtilir. Her satır koşullarıyla gelir: A.TR dolaşım belgesi ibrazı, EUR.1/menşe beyanı, İGV/EMY tercihi için tedarikçi beyanı veya menşe belgesi tevsiki; tevsik gerektiren satırlarda tevsik yoksa uygulanacak "Diğer Ülkeler" oranıyla kötümser toplam da gösterilir. Sıralama karar desteğidir, tavsiye veya bağlayıcı tarife/menşe bilgisi değildir; yasal not yanıt ve tablo altında yer alır. Özellik Uzman paketi kilidine (`scenario_compare`) bağlıdır; tablo CSV olarak indirilebilir. Ön değerlendirme dosyası menşe belgelerini de içerir ve "PDF olarak kaydet" ile yazdırılabilir. Ürün evsafı kullanıcı PDF'i, Word (.docx) belgesi veya HTTPS ürün sayfasından `/api/customs/ingest-source` ile sınırlı metin çıkarımıyla desteklenebilir; çıkarılan metin kullanıcı onayına sunulur. E-ticaret sayfalarında (Trendyol, Hepsiburada, marka siteleri) ürün verisi önce yapılandırılmış kaynaklardan okunur (`product_page.py`: JSON-LD `Product`, Trendyol `__PRODUCT_DETAIL_APP_INITIAL_STATE__`, `og:`/meta etiketleri), menü ve alt bilgi metni atılır; site otomatik okumayı engellerse açık bir hata mesajı döner ve `PRODUCT_PAGE_BROWSER_FALLBACK` (varsayılan açık) ile sayfa yalnızca kendi alan adına izin verilen başsız Chromium'da bir kez daha denenir. **Sevkiyat belgesi** bölümü konşimento (B/L), AWB, CMR, ticari/proforma fatura, çeki listesi veya menşe şahadetnamesini PDF, Word (.docx) ya da fotoğraf olarak alır (`/api/customs/ingest-shipping-document`, `shipping_documents.py`): metinli PDF ve .docx doğrudan, taranmış PDF ilk sayfası görüntüye çevrilerek (pymupdf), fotoğraf görsel modelle okunur; gönderici/alıcı, eşya tanımı, kap/ağırlık, liman, Incoterm, fatura tutarı ve konteyner alanları düzenlenebilir biçimde gösterilir ve yalnızca boş form alanlarına aktarılır. Belge metni modele gitmeden `sanitize_untrusted_context` ve `redact_text` süzgeçlerinden geçer; belgedeki HS kodu forma yazılmaz, yalnızca öneri olarak gösterilir.

**Çok motorlu girdi genişletmesi (PRD Faz 3.4).** Ürün belgesi yüklemesinde metin katmanı olmayan ya da çok az metin taşıyan PDF'ler (taranmış katalog, teknik föy taraması, teknik çizim) artık sessizce reddedilmez: sayfa başına 200 karakterden az metin varsa ilk **en fazla 3 sayfa** `shipping_documents.rasterize_pdf_pages` ile görüntüye çevrilir ve ürün fotoğrafıyla **aynı** görsel evsaf yoluna (`customs_advisor.describe_images` → mevcut çift model + hakem zinciri, aynı istem ve şema) tek istekte birden çok görsel olarak gönderilir. Yanıt `source_kind: "pdf_pages"`, `pages_used`, `page_count` ve düzenlenebilir evsaf listesiyle döner; belge 10 MB sınırına tabidir, bu yol `vision` kotasını tüketir (OAuth açıkken giriş ister) ve metinli PDF'ler eskisi gibi kotasız metin yolundan geçer. Arayüzde sonuç "taranmış/çizim PDF'i sayfa görseli olarak analiz edildi" rozetiyle gösterilir ve "Evsafları forma aktar" düğmesi alanları fotoğraf analiziyle aynı inceleme satırlarına yazar. **GTİP hiçbir koşulda forma otomatik yazılmaz**; modelin ürettiği aday kod alanları sunucuda düşürülür.

**Araç çağıran asistan (`assistant.py`, PRD Faz 3.3, `POST /api/customs/assistant`).** Gümrükçe'ye Sor alanındaki **Sohbetle sor** kutusu, serbest soruyu bir LLM orkestratörüne verir; model yanıtı ezberden yazamaz, yalnızca deterministik araçları çağırabilir: `lookup_tariff_measures`, `resolve_turkish_tariff_tree`, `calculate_import_landed_cost`, `lookup_import_controls`, `lookup_trade_measures`, `lookup_excise_tax`, `lookup_vat_rate`, `get_customs_exchange_rate`, `search_classification_evidence`, `origin_scenarios`, `savings` (hepsi mevcut motorların aynısı; motorlar `build_default_tools(...)` ile enjekte edilir, `assistant.py` sunucu modülünü import etmez). Her araç çıktısı JSON'a çevrilip `[tool_N]` kimlikli bir kaynak olur. Döngü Gemini'de yerel `tools: [{functionDeclarations}]` / `functionCall` / `functionResponse`, Z.ai ve OpenRouter yedeğinde OpenAI uyumlu `tools` / `tool_calls` / `role:"tool"` biçimiyle çalışır; en fazla `ASSISTANT_MAX_TOOL_CALLS` (varsayılan 6) araç çağrısı ve `ASSISTANT_DEADLINE_SECONDS` (varsayılan 170 sn, `asyncio.timeout`) toplam süre vardır — 7. çağrı reddedilir, bilinmeyen araç adı çalıştırılmaz, argümanlar pydantic şemasıyla doğrulanır ve hata modele veri olarak döner. **Sunucu doğrulaması**: nihai JSON yanıttaki (`answer`, `claims[] {text, source_ids}`, `gtip_candidates`, `rates`, `next_steps`) her GTİP ve oran araç çıktılarındaki kanıt kümesinde aranır; geçmeyen ya da atıfsız kalemler yanıttan düşülür, `answer` metninde `[doğrulanmadı]` ile maskelenir ve `unverified[]` listesinde gerekçesiyle gösterilir (atıfsız iddialar `_sanitize_model_result` deseniyle silinir). Kullanıcı metni ve geçmiş (en fazla 6 tur, mesaj başına 2.000 karakter) `sanitize_untrusted_context` + `redact_text` süzgecinden geçer. Rota giriş ister, `classification` kotasını tüketir ve dakikada 10 istekle sınırlıdır; yanıt `answer`, `claims`, `tool_calls[] {id, name, args, summary}`, `sources`, `unverified`, `warnings` ve `legal_notice` alanlarını döner. Arayüzde araç çağrıları katlanabilir bir bölümde, kaynak kimlikleri rozet olarak görünür; **bulunan GTİP hiçbir koşulda forma otomatik yazılmaz**. `benchmarks/assistant_benchmark.py` sahte araç çıktılarına karşı halüsinasyon sayacını ağsız çalıştırır.

**Marka/model doğrulama (PRD Faz 3.4).** `POST /api/customs/brand-model` (giriş gerekli, dakikada 20 istek) gövdesi `{brand, model, url?}` alır. **Otomatik web araması yapılmaz**: `url` verilmezse yanıt yalnızca "kaynak URL verin" yönlendirmesi ve öneri listesidir (önce üreticinin resmî ürün sayfası). URL verildiğinde sayfa `ingest-source` ile aynı SSRF korumalarından (HTTPS zorunlu, kimlik bilgisi/localhost/özel ağ/metadata IP reddi, her yönlendirme adımında yeniden doğrulama) ve `product_page.py` anti-bot/yapılandırılmış çıkarım yolundan geçirilir; `product_page.brand_model_match` marka ve modelin sayfada geçip geçmediğine göre 0-100 arası puan, `match`/`partial`/`no_match` kararı ve Türkçe gerekçe listesi üretir (büyük-küçük harf duyarsız, Türkçe `I/İ` katlaması, `K-9000 XL` ≡ `K9000XL` sadeleştirmesi). Çıkarılan sayfa alanları kullanıcıya dönmeden `sanitize_untrusted_context` ve `redact_text` süzgeçlerinden geçer; puan yalnızca metin eşleşmesidir, ürünün doğruluğunu, menşeini veya GTİP'ini teyit etmez.

**Belgeden maliyet alanı çıkarımı (PRD Faz 2.5).** Aynı okuma navlun ve sigorta tutarını (varsa belgedeki ayrı para birimiyle: `freight_currency`, `insurance_currency`), fatura tutarı/para birimini, Incoterm'i ve ödeme şeklini de çıkarır. Ödeme şekli belgedeki ham ifade (`payment_terms`) olarak okunur ve saf `payment_terms_to_method` yardımcısıyla KKDF değerlendirmesinde kullanılan normalize anahtara çevrilir (`cash_in_advance` peşin/advance/T/T in advance, `cash_against_goods` mal mukabili/open account, `cash_against_documents` vesaik mukabili/CAD/D/P, `letter_of_credit` akreditif/L/C, `acceptance_credit` kabul kredili/D/A; tanınmazsa `null`). Model yalnızca belgede açıkça yazan değeri döndürür; para birimi ISO-4217 üç harf, tutar sıfırdan küçük olamaz, aksi hâlde alan `null` kalır. Arayüzde "Ürün dosyasına aktar" yalnızca **boş** maliyet alanlarını (navlun, sigorta, ödeme şekli, Incoterm, fatura tutarı/para birimi) doldurur, dolu alanlara dokunmaz ve doldurduğu alanın yanına "belgeden alındı, doğrulayın" rozeti (`data-from-document`) koyar; kullanıcı alanı düzenleyince rozet kalkar. Navlun/sigorta belgede fatura para biriminden farklı bir para birimiyle yazılmışsa alan boş bırakılır ve bildirilir; KKDF oranı forma yazılmaz (maliyet motoru ödeme şeklinden öneri üretir, kullanıcı doğrular); HS/GTİP kodu hiçbir zaman forma yazılmaz.

**İnteraktif karar soruları** (`decision_questions.py`, PRD Faz 2.3): tarife sonucu, KDV listesi önerisi ve resmî önlem raporu, cevabı yalnız kullanıcının verebileceği kısa bir soru listesine dönüştürülür — KDV oranı belirsizse liste şartını doğrulayan seçenekli soru, tek öneri varsa onay sorusu, gözetim kapsamında "gözetim belgeniz var mı?", ödeme şekli bilinmiyorsa KKDF sorusu (peşin %0 / vadeli %6), A.TR uygunsa "A.TR ibraz edilecek mi?", tercihli oran için menşe tevsiki gerekiyorsa "tedarikçi beyanı / EUR.1 var mı?", tarife kontenjanı eşleşmesinde "kontenjan belgesi var mı?" ve kullanılmış eşya sorusu. Sorular kural tabanlıdır (yapay zekâ yok); kullanıcının zaten doldurduğu alan için soru üretilmez. Cevaplar `decision_answers` olarak gönderilir; `apply_decision_answers` yalnız `vat_rate`, `kkdf_rate`, `payment_method`, `has_surveillance_certificate` ve `atr_certificate` alanlarını, yalnız boşsa ve yalnız kullanıcı cevabıyla doldurur — bilinmeyen soru kimliği veya seçenek değeri yok sayılır, hiçbir oran otomatik girilmez ve kullanıcının forma yazdığı değer her zaman kazanır. `/api/tariff/cost` yanıtı `decision_questions` alanını taşır, `/api/customs/precheck` soruları ön değerlendirme sonucunda döndürür; arayüzde sınıflandırma soru panelinin yanındaki **Karar soruları** bloğunda seçim yapıldığında ilgili maliyet alanı doldurulur ve "cevabınızla dolduruldu" rozeti gösterilir.

**Hesaplanan işlem akışı** (`customs_workflow.py`, PRD Faz 2.4): her ön değerlendirme sonucu, sonucun kendi alanlarından kural tabanlı olarak türetilen 24 adımlık bir işlem akışı (`workflow`) taşır: eşya tanımı, evsaf onayı, aday GTİP, ağaçta 12 hane, menşe/sevk ülkesi, A.TR, EUR.1/menşe beyanı, kıymet ve Incoterm, kur/tescil tarihi, GV, İGV, EMY, damping/sübvansiyon, korunma/kota, gözetim, ÖTV, KDV, KKDF, TAREKS/ÜGD kapsamı, yasak/izin listeleri, diğer kurum izinleri, belge kontrol listesi, beyanname öncesi ödemeler (damga, ardiye, GEKAP, TRT) ve uzman devri/BTB kararı. Her adım `done | pending | blocked | not_applicable` durumu, kısa özet, kaynak alan adları (`evidence_refs`), gerekiyorsa `next_action` ve yasal dayanak taşır; durumlar yalnızca tarife/kontrol/önlem sonuçları, menşe belgesi kuralı, eksik bilgi listesi ve uzman paketinden türetilir (yapay zekâ yorumu yok). Dallanma örnekleri: AB'den A.TR ile gelen sanayi ürününde EUR.1 adımı `not_applicable`; resmî damping/gözetim/korunma listesinde eşleşme yoksa ilgili adım `not_applicable`; fatura bedeli yoksa kıymet, kur ve TL kalemleri `blocked`; 12 hane onayı yoksa kontrol adımları `blocked`. `workflow_summary` adım sayılarını ve uygulanmayan adımlar hariç tamamlanma oranını verir. Arayüzde **İşlem Rehberi** sekmesi sonuç yokken statik rehberi, sonuç geldiğinde hesaplanan akışı durum rozetleriyle gösterir; eski kayıtlarda alan boş olabilir ve statik rehber kalır.

**Sunucu tarafı PDF raporu** (`POST /api/customs/report.pdf`, `report_pdf.py`): Uzman ve üstü paketlerde (`pdf_report` özellik kilidi) "PDF olarak kaydet" düğmesi tarayıcı yazdırması yerine sunucuda üretilen A4 raporu indirir; kilidi olmayan paketlerde düğme tarayıcı yazdırmasına düşer ve bir kez paket yükseltme önerisi gösterir. Rapor yalnız doğrulanmış ön değerlendirme sonucundan (`{"result": …}` ya da kayıtlı kanıt dosyası için `{"dossier_id": …}`) üretilir; ürün görseli ve iletişim verisi içermez, kota tüketmez. İçerikte başlık bilgileri (ürün, GTİP, menşe, kaynak tarihi, hazırlanan/oluşturma), e-posta ile paylaşılan bölümler, aday kodlar, kontrol/belge/vergi bulguları, maliyet taslağı, uzman inceleme paketi ve tam **kaynak defteri** (kaynak kimliği, kurum, başlık, URL, alınma zamanı, SHA-256) yer alır. Her sayfanın altında zorunlu alt bilgi basılır: `customs_advisor._legal_notice` metni, "Nihai tarife tespiti bağlayıcı karar yerine geçmez; sonuçlar karar destek niteliğindedir." cümlesi ve `gumruksor.com · Sayfa X / Y`. `PDF_RENDERER=playwright` (varsayılan; başsız Chromium, tüm ağ istekleri engellenir, tek eş zamanlı işlem ve 20 sn sınırı) veya `pymupdf` (saf Python yedek) seçilebilir; `auto` önce Chromium'u dener. Üretim başarısız olursa uç 503 döner ve arayüz tarayıcı yazdırmasını önerir.

**Gümrük Danışmanları** alanında kullanıcı, oluşturduğu analiz paketini yönetici onaylı bağımsız bir mevzuat danışmanına kontrollü biçimde gönderebilir ve uygulama içinde mesajlaşabilir. Danışman kaydı ücretsizdir; profil değişiklikleri yeniden incelemeye alınır. Ürün görseli, Google e-postası ve mali tutarlar otomatik paylaşılmaz. Bu profiller gümrük müşavirliği yetkisi, gümrükte temsil veya fiilî gümrük işlemi hizmeti anlamına gelmez; bağlayıcı karar gereken durumda BTB ve mevzuatın gerektirdiği yetkili kanallar ayrıca kullanılmalıdır.

Kur ve kur tarihi girildiğinde maliyet defteri TL beyanname özetine çevrilir: beyanname damga vergisi ve tescil öncesi liman/ardiye giderleri TL olarak KDV matrahına eklenir, GEKAP toplama eklenir fakat matraha girmez, TRT bandrol ücreti oranla hesaplanıp matraha dahil edilir. Tarife & Maliyet sekmesindeki **Toplu hesap** bölümü CSV/XLSX ile en fazla 200 beyanname satırını aynı motorla hesaplar (şablon: `/api/tariff/bulk/template`); tarife satırları, maliyet defteri ve menşe senaryoları CSV olarak indirilebilir veya Excel'e yapıştırılmak üzere kopyalanabilir. Maliyet motoru gümrük vergisi ve İGV dışında ek mali yükümlülük, damping/sübvansiyon, KKDF, KDV, ÖTV ve gözetim kalemlerini de ayrı ayrı ister. Yapılandırılmış canlı kaynağa henüz bağlanmamış bir kalem otomatik olarak `0` kabul edilmez: kullanıcı resmî kaynaktan uygulanmadığını doğruladıysa `0` girmeli, aksi halde toplam maliyet bilinçli olarak eksik bırakılır. KKDF ödeme şekline göre önerilir: peşin ödemede %0, kredili/vadeli (mal mukabili, vadeli akreditif, kredili) ödemede %6 — öneri her zaman uyarıyla gösterilir ve kullanıcı onaylar. Sonuçta "Toplam vergi" ara toplamı ayrı gösterilir; beyanname başına sabit işlem harcı tutar olarak içerilmez, uyarıyla hatırlatılır. Sonuçtaki kapsam matrisi her kalemi `verified_snapshot`, `partial_snapshot`, `not_integrated` veya `user_confirmation_required` olarak açıkça gösterir. Giriş yaptıysanız ön değerlendirme dosyasını **E-posta ile gönder** düğmesiyle kendi adresinize iletebilirsiniz (dosya sunucuda yapılandırılmış işlem e-postası kanalıyla gider; anahtar yapılandırılmamışsa düğme açıkça "yapılandırılmadı" yanıtı verir).

Sınıflandırma kalitesi [customs_classification_v1.jsonl](benchmarks/customs_classification_v1.jsonl) içindeki kaynak URL'si, sayfa, tüzük numarası ve arşiv SHA-256 değeri sabitlenmiş resmî AB karar örnekleriyle ölçülür. Ayrı [tarihsel Türk BTB takımı](benchmarks/turkish_btb_gtip12_historical_v1.jsonl), İstanbul Gümrük ve Ticaret Bölge Müdürlüğünün resmî bülteninde yayımlanan dört gerekçeli 2016 örneğini GTİP12 Top-1/Top-3 ölçümü için kullanır. `customs_benchmark.py` hedef derinliğine göre HS6, CN8 ve GTİP12 metriklerini ayrı hesaplar. Tarihsel takım güncel tarife geçerliliği veya BTB'nin hak sahibi dışındaki kişiler bakımından hukuki bağlayıcılığı iddiasında bulunmaz.

```bash
python customs_benchmark.py predictions.json --cases benchmarks/turkish_btb_gtip12_historical_v1.jsonl
```

* Fotoğraf tek başına kesin veya bağlayıcı GTİP üretmez. Kesin sınıflandırma için teknik belge ve gerektiğinde Bağlayıcı Tarife Bilgisi gerekir.
* Güvenlik sorusu/CAPTCHA kullanan Bakanlık Tarife Arama Motoru otomatik aşılmaz; sonuçlarda yalnızca manuel doğrulama bağlantısı olarak yer alır.
* EBTI, CLASS, CN 2026 ve TARIC karşılaştırmalı sınıflandırma kanıtıdır; bunların kodları Türkiye GTİP12 veya Türkiye vergi oranı olarak doğrudan kullanılmaz.
* EBTI metinleri ve resmî indirme verileri kaynak olarak kullanılabilir. Açık kullanım hakkı teyit edilmemiş EBTI ürün görselleri topluca kopyalanmaz ve model eğitimine alınmaz.
* Her sonuç tarihli resmî kaynak zinciri, belirsizlikler ve zorunlu hukuki uyarıyla birlikte döner. Sistem yüzde yüz doğruluk veya bağlayıcı idari karar iddiasında bulunmaz.

Görsel evsaf çıkarımı ve resmî kanıt paketinin yorumlanması tek bir OpenRouter anahtarıyla çalışır. Görsel analizde modeller sırayla yedeklenir. Tarife sınıflandırmasında ise Gemini ve GLM 5.3 Flash aynı onaylı evsafı birbirinden bağımsız değerlendirir; ilk kodları ayrışırsa Grok/GPT/Claude zincirindeki ilk kullanılabilir farklı model hakem olur. Güven puanı modelin kendi iddiasından değil, bağımsız model uzlaşması, aktif Türk tarife satırı, resmî sınıflandırma gerekçesi ve eksik ayırt edici evsaftan hesaplanır. Varsayılan zincir **Gemini → GLM 5.3 Flash → Grok → GPT → Claude** şeklindedir:

```text
OPENROUTER_API_KEY=<sunucuda gizli değer>
OPENROUTER_VISION_MODELS=~google/gemini-flash-latest,z-ai/glm-5.3-flash,~x-ai/grok-latest,openai/gpt-chat-latest,~anthropic/claude-opus-latest
OPENROUTER_CUSTOMS_MODELS=~google/gemini-flash-latest,z-ai/glm-5.3-flash,~x-ai/grok-latest,openai/gpt-chat-latest,~anthropic/claude-opus-latest
CLASSIFICATION_SYNC_INTERVAL_SECONDS=86400
```

`GEMINI_API_KEY` tanımlıysa birincil sağlayıcı doğrudan Google Gemini'dir (`gemini-3.8-flash` → `gemini-flash-latest`); yoksa `ZAI_API_KEY` ile Z.ai (görsel: `glm-5v-turbo`, `glm-4.6v`; metin: `glm-5.3`, `glm-5.3-flash`), o da yoksa OpenRouter kullanılır. Görsel modellerde "düşünme" adımı varsayılan olarak kapalıdır (`ZAI_VISION_THINKING=disabled`); bu, dakikalarca süren yanıtları önler. Her istek `LLM_REQUEST_TIMEOUT_SECONDS` (75 sn), birincil zincir `LLM_PRIMARY_BUDGET_SECONDS` (95 sn) ve tüm çağrı `LLM_TOTAL_DEADLINE_SECONDS` (150 sn) ile sınırlıdır; süre dolunca kullanıcı sağlayıcı adı içermeyen net bir hata görür. Birincil sağlayıcı yanıt vermezse kalan süre içinde sırasıyla Gemini (`GEMINI_MODELS`), Z.ai ve OpenRouter (`OPENROUTER_FALLBACK_MODELS`) yedek olarak denenir; birincil olan atlanır. OpenRouter yedeği varsayılan olarak kapalıdır (`LLM_FALLBACK_TO_OPENROUTER=1` ile açılır); `LLM_FALLBACK_TO_GEMINI=0` / `LLM_FALLBACK_TO_ZAI=0` diğerlerini kapatır. `LLM_PRIMARY_PROVIDER=zai|gemini|openrouter` ile birincil sağlayıcı zorlanabilir. Gemini çağrıları, productanaliz projesinde canlıda doğrulanan yöntemle Google'ın yerel `generateContent` API'sine gider (sistem talimatı + `inlineData` görsel); 429/5xx yanıtları kısa aralıklarla yeniden denenir, 404 veya tekrarlayan 503'te sıradaki modele geçilir ve yanıt metnindeki JSON sunucu tarafında doğrulanır. Arayüzde sağlayıcı veya model adı gösterilmez; yalnızca "yapay zekâ analizi" ve güven düzeyi görünür.

Her çağrı katı JSON şeması ve `require_parameters=true` kullanır; bu nedenle görsel giriş veya yapılandırılmış çıktı desteği olmayan uçlar seçilmez. `data_collection=deny`, istemleri veri saklayabilen sağlayıcı uçlarına göndermemek için zorunludur. Nano Banana bir görsel üretim/düzenleme modelidir ve bu metin çıkarım akışında kullanılmaz. Model ürün adı, kategori, kapsamlı tanım, bileşim, kullanım, görünür menşe ibaresi, marka/model, ölçü, etiket, renk, fiziksel yapı, parçalar, çalışma mekanizması, ambalaj ve sınıflandırma sorularını ayrı alanlara çıkarır. Görselden belirlenemeyen menşe, teknik değer ve maliyet girdilerini uydurmak yerine kullanıcıya tamamlanacak bilgi olarak gösterir.

`OPENROUTER_API_KEY` yoksa arayüz uydurma yanıt üretmez; yalnızca güncel resmî kanıt paketini ve eksik bilgi listesini gösteren `evidence_only` modunda çalışır. Yüklenen JPEG/PNG/WebP görseli yeniden kodlanarak metaverisi temizlenir, kalıcı olarak saklanmaz ve istek başına 8 MB / 25 megapiksel sınırı uygulanır.

> Hukuki yorumlar bilgilendirme amaçlıdır. Sonuçlarda verilen resmî URL, tarih, sayı, mülga/yürürlük durumu ve varsa sonraki değişiklikler karar öncesinde doğrulanmalıdır.

### Resmî önlem listeleri, TCMB kuru ve günlük eşitleme

Tarife sorgusu ve maliyet hesabı, resmî listelerden okunan **ticaret politikası önlemlerini** GTİP ve menşe ile eşler ve tabloda gösterir:

* **Damping / sübvansiyon**: Ticaret Bakanlığı İthalat Genel Müdürlüğü'nün "Yürürlükteki Önlemler" çalışma kitabı (kesin ve geçici önlemler, ülke, oran/tutar, tebliğ ve bitiş tarihi).
* **Korunma önlemleri**: Korunma Önlemleri Dairesi'nin "Yürürlükte Bulunan Korunma Önlemleri" listesi (dönemsel tutarlar, kontenjan tahsisi olan önlemler işaretlenir).
* **Gözetim**: mevzuat.gov.tr'deki yürürlükteki "İthalatta Gözetim Uygulanmasına İlişkin Tebliğ" metinlerinden çıkarılan GTİP / birim gümrük kıymeti tabloları. Kapsamdaki kodlarda maliyet hesabı birim kıymeti önerir ve gözetim belgesi yoksa kıymetin yükseltileceğini uyarır.
* **Tarım ürünleri tarife kontenjanları**: Bakanlığın ülke bazlı "... Menşeli Bazı Tarım Ürünleri İthalatında Tarife Kontenjanı Uygulanması Hakkında Karar" ve tebliğ eklerinden (.docx/.doc) GTİP, kontenjan kodu, miktar, dönem ve kontenjan dâhili vergi oranı; menşe ülke (AB, EFTA ve Birleşik Krallık grup adları dâhil) ile eşlenir. Eski `.doc` ekleri ek program gerekmeden `olefile` ile (Word parça tablosu) okunur.
* **İthalat Tebliğleri dizini**: Bakanlığın yıllık "İthalat Tebliğleri" sayfasındaki tebliğ adı, numarası ve Resmî Gazete bağlantıları (`/api/tariff/communiques`).

Bu listeler `data/official/` altındaki tohum dosyalarıyla açılır; sunucu **her gün** (`TRADE_MEASURES_SYNC_INTERVAL_SECONDS`, varsayılan 86400) Bakanlık sayfalarındaki güncel çalışma kitaplarını ve mevzuat.gov.tr aramasını yeniden okur, yalnız yeni veya değişen tebliğ metinlerini indirir ve her farkı değişiklik defterine yazar. **Birleşik değişiklik defteri** (`change_ledger.py`, `changes.sqlite3`): tarife cetveli, kontrol tebliğleri, AB sınıflandırma tüzükleri ve önlem listeleri için her eşitleme bir *kayıt* (hangi snapshot hangisini değiştirdi, kaynak URL, SHA-256, RG tarihi, satır sayıları, ayrıştırma uyarıları) ve satır düzeyinde önce/sonra farkı yazar; geçmiş üçüncü snapshot gelince kaybolmaz. `GET /api/changes?kind=&gtip=&since=&limit=` defteri süzer, `GET /api/admin/changes` (yönetici/editör) kayıt listesini ve `?batch=` ile satır farkını verir; yönetim panelindeki **Veri Değişiklikleri** sekmesi aynı veriyi gösterir. Sunucu açılışında mevcut snapshot geçmişi bir kez geriye dönük (`backfilled`) işlenir. Önlem listelerinde satır düzeyi silsile `measure_rows` tablosunda tutulur (kaynak URL, dosya SHA-256, ilk/son görülme, kaldırılma tarihi, RG'den türetilen `valid_from`/`valid_to`); her önlem eşleşmesi `provenance` alanıyla döner. Farklar **Değişiklikler** sekmesinde görünür, izleme listesindeki GTİP'ler için e-posta bildirimine dâhil edilir. Oran ve tutarlar resmî tabloda yazıldığı gibi metin olarak gösterilir; firma bazlı oranlar hesaba otomatik girilmez, kullanıcı doğrulaması istenir (`/api/tariff/measures`, MCP aracı `lookup_trade_measures`).

**Editoryal inceleme kapısı** (`review_policy.py`, PRD Faz 1.3, insan onaylı veri): tarife cetveli, kontrol tebliğleri ve AB sınıflandırma tüzükleri için indirilen her yeni sürüm önce satır düzeyinde önceki onaylı sürümle karşılaştırılır, sonra `DATA_REVIEW_MODE` politikasına göre ya doğrudan yayına alınır ya da yönetim panelindeki **İnceleme Kuyruğu** sekmesinde editör/yönetici kararı bekler (`off` varsayılan: bugünkü davranış; `auto`: `DATA_REVIEW_MAX_AUTO_ROWS`, `DATA_REVIEW_MAX_AUTO_RATIO` eşiklerini aşan veya ayrıştırma uyarısı taşıyan sürümler bekler; `strict`: hepsi bekler). Bekleyen sürüm aktif değildir; sorgular önceki onaylı sürümle yanıtlanır ve uygulamadaki Değişiklikler sekmesinde "yeni sürüm incelemede" şeridi görünür. Onay yeni sürümü aktifleştirip kardeşlerini pasifleştirir; ret edilen sürüm aynı içerikle bir daha aktifleşmez. Her karar `audit_log`'a (`data_review`) ve değişiklik defterindeki kaydın `review_status` alanına yazılır; `ADMIN_EMAILS` adreslerine e-posta gönderilir (Resend yapılandırılmışsa). Rotalar: `GET /api/admin/reviews`, `POST /api/admin/reviews/{kind}/{snapshot_id}` (`{"action":"approve"|"reject","note":""}`); `/health` `pending_reviews` ve `review_mode` alanlarını döner. Önlem listeleri (damping/korunma/gözetim) için kapı sonraki adımda eklenecektir.

**Tarih bazlı geçerlilik — GTİP × ülke × tarih** (`temporal.py`, PRD Faz 1.4): her tarife ve kontrol tebliği sürümü `valid_from` (yasal başlangıç; resmî sayfa/çalışma kitabı metnindeki "… tarihinden itibaren" veya Resmî Gazete tarihi, tebliğlerde "… tarihinde yürürlüğe girer" hükmü; okunamazsa yapılandırılmış tarih, `valid_from_basis` alanı kaynağı söyler) ve yeni sürüm yayına alınınca `valid_to` (yeni sürüm daha sonra başlıyorsa yasal sınır, aksi hâlde yeni sürümün indirildiği gün = gözlemlenen sınır) taşır. `as_of` (YYYY-AA-GG) parametresi `/api/tariff/lookup|tree|cost|measures|scenarios` ve `/api/controls/lookup` rotalarında, MCP araçlarında (`lookup_tariff_measures`, `resolve_turkish_tariff_tree`, `calculate_import_landed_cost`, `lookup_trade_measures`, `lookup_import_controls`) ve ön değerlendirmede (`as_of_date`) o gün yürürlükte olan onaylı sürümü seçer; sonuçlar `as_of_date`, `validity_basis` (`current` bugün / `legal` yasal aralık / `observed` indirme tarihlerinden türetilmiş aralık / `unavailable` kapsayan sürüm yok) ve `snapshot_validity` alanlarını döner, önlem listelerinde süre değerlendirmesi de o güne göre yapılır. Geçmiş tarih sorgusu `temporal_query` özelliğine bağlıdır (Ekip ve üzeri); bugün ve boş tarih herkese açıktır. Tarife panelindeki "Yürürlük tarihi" alanı ve sonuçtaki geçerlilik rozeti aynı bilgiyi gösterir; sunucu açılışında eski sürümlerin açık aralıkları bir kez kapatılır (`backfill_validity`).

**KDV oranı önerisi**: 3065 sayılı KDV Kanunu md. 28 uyarınca yürürlükteki 2007/13033 sayılı Karar eki (I) sayılı liste %1, (II) sayılı liste %10, listelerde yer almayanlar %20'dir. `data/official/vat_lists.json` tohumu bu listelerdeki GTİP / pozisyon / fasıl atıflarını satır satır taşır (`verified:false` satırlar doğrulanmamış kalemlerdir); sunucu her gün (`VAT_LISTS_SYNC_INTERVAL_SECONDS`, 0 kapatır) mevzuat.gov.tr konsolide metnini yeniden ayrıştırmayı dener, başarısız olursa tohum kullanılır. Eşleme en uzun GTİP ön ekini fasıl aralığına tercih eder; "kullanılmış / toptan / perakende" gibi GTİP'ten okunamayan şartlarda ya da aynı özgüllükte farklı oranlarda sonuç **belirsiz** olarak iki adayla döner ve oran girilmez. Oranlar yalnız **öneridir**: tarife sonucunda "Öneriyi kullan" düğmesiyle kullanıcı onaylamadan KDV alanına yazılmaz (`/api/tariff/vat?gtip=`).

**Uyum gösterge paneli ve erken uyarı** (`compliance.py`, PRD Faz 2.6, `GET /api/account/compliance`, Hesabım → **Uyum** sekmesi): kullanıcının kendi kanıt dosyaları, izleme listesi, birleşik değişiklik defteri (son 90 gün), önlem listeleri ve kontrol tebliği kataloğundan **tamamen kural tabanlı** (yapay zekâ kullanılmadan) 0-100 arası bir uyum puanı üretir. Beş ağırlıklı bileşen: 12 haneli onaylı GTİP oranı (%25), belirsiz/çözülmemiş oran ve bekleyen KDV onayı olmayan dosya oranı (%20), izlenen kodlarda süresi dolmuş veya 90 gün içinde dolacak damping/korunma önlemi olmaması (%20), izlenen kodlarda son 90 günde resmî değişiklik olmaması (%20), kullanılmış eşya veya uzman eskalasyonu işareti olmaması (%15). Her uyarı önem derecesi (`high`/`medium`/`low`), GTİP, dosya kimliği, varsa tarih ve kaynak (`ledger:trade_measures:surveillance`, `trade_measures:anti_dumping`, `controls:year_rollover`, `dossier:kdv`…) ile döner; örnekler: "X dosyasındaki 8517… için gözetim tebliği değişti", "Damping önlemi 30 gün içinde bitiyor", "GTİP 8 haneli; 12 hane onaylanmadı", "KDV oranı kullanıcı onayı bekliyor", dosyadaki kontrol tebliğinin yıl geçişi. `change_alerts` yetkisi olan paketlerde yüksek öncelikli uyarılar için günde en fazla bir e-posta özeti gönderilir (yalnız başlık, GTİP ve tarih; dosya içeriği gönderilmez; aynı uyarı kümesi tekrar postalanmaz). Puan karar desteğidir, bağlayıcı tarife veya uygunluk görüşü değildir.

**TCMB kuru**: Tarife & Maliyet ve Gümrükçe'ye Sor formlarındaki "TCMB kurunu getir" düğmesi, tescil tarihinde yürürlükte olan döviz satış kurunu (4458 sayılı Gümrük Kanunu md. 30; tescil tarihinden önceki son iş gününün bülteni) TCMB arşivinden alır ve bülten tarihi/numarasıyla birlikte gösterir (`/api/tariff/exchange-rate`, MCP aracı `get_customs_exchange_rate`).

**Eylemio köprüsü**: `EYLEMIO_EMAIL` / `EYLEMIO_PASSWORD` (ve isteğe bağlı `EYLEMIO_ACCOUNT_ID`) tanımlandığında Gümrükçe'ye Sor sayfasındaki "Beyanname durumu" kutusu, Eylemio'daki gümrük konektörü üzerinden (müşavirin BİLGE web servis hesabıyla) beyanname detay ve durumunu salt okunur olarak getirir (`/api/customs/declaration`, MCP aracı `query_customs_declaration_status`). Beyan oluşturmaz ve tescil etmez; giriş yapmış kullanıcılara açıktır.

## ChatGPT ve Codex bağlantısı

Uzak MCP adresi: `https://gumruksor.com/mcp`

Tanıtım ve fiyatlandırma sayfası: `https://gumruksor.com/`

Web araştırma uygulaması: `https://gumruksor.com/app`

### Kalıcı hibrit arama indeksi (BM25 + embedding)

Resmî korpuslar tek bir kalıcı indekste toplanır (`hybrid_index.py`, `MEVZUAT_DATA_DIR/hybrid_index.sqlite3`):
ÜGD kontrol kapsam satırları (yalnız aktif/onaylı tebliğler), AB sınıflandırma tüzüğü sayfaları (1.200
karakterlik parçalar), ticaret önlemi ürün tanımları (damping/korunma/gözetim/kota), `customs_sources.json`
resmî sayfaları, ÖTV ve KDV liste satırları ve varsa tarife cetveli eşya tanımları. Sözlüksel katman SQLite
FTS5'tir (`unicode61 remove_diacritics 2`, BM25); anlamsal katman belge gömmelerini `embeddings` tablosunda
float32 olarak saklar ve RAM'de float16 matris üzerinde kosinüs benzerliğiyle arar (numpy yoksa saf Python'a
düşer). İki sıralama **Reciprocal Rank Fusion** ile birleştirilir; sorguda GTİP ön eki verilmişse eşleşen
belgeler ek puan alır. Sorgu gömmesi `embed_timeout` (varsayılan 0,45 sn) içinde dönmezse ya da sağlayıcı
hata verirse sonuç yalnız sözlüksel döner ve yanıt `mode` alanında `lexical` yazar (aksi hâlde `hybrid`).

Besleme idempotenttir: `source_sha256` değişmeyen belge yeniden yazılmaz ve yeniden gömülmez. Arka plan
döngüsü `hybrid-index-refresh` açılıştan 60 saniye sonra başlar ve `HYBRID_INDEX_REFRESH_SECONDS`
(varsayılan 1800) aralığıyla yalnız değişen belgeleri tazeler. `GET /api/search/hybrid?q=&gtip=&limit=`
(60/dk) hibrit sonuçları verir; `GET /api/admin/index-status` (editör/yönetici) belge, korpus ve embedding
sayılarıyla son yenilemeyi gösterir. `/api/tariff/autocomplete` ve `/api/search/unified` yanıtlarında mevcut
LIKE sonuçları korunur, hibrit eşleşmeler `mode` alanıyla eklenir.

**Yurt dışı tarife karşılaştırma** (`foreign_tariff.py`, PRD Faz 4): aynı eşya için Türk tarifesinin
yanında Birleşik Krallık, Avrupa Birliği ve İsviçre tarifesi gösterilir. Üç ülke veriyi aynı biçimde
yayımlamadığı için ürün bu farkı gizlemez:

* **Birleşik Krallık** — `trade-tariff.service.gov.uk` JSON:API'si anahtarsız ve makine okunurdur.
  **Nomenklatürün tamamı yereldedir**: 21 bölüm (`/goods_nomenclatures/section/{n}`) günlük
  eşitlenir ve kod ağacı tümüyle kendi veritabanımızda durur; aday kod eşleştirmesi ağa çıkmaz.
  Vergi oranları ise BK'de **yalnız emtia başına** yayımlanır (toplu ölçü ucu yoktur), bu yüzden
  kalıcı bir **oran arşivinde** tutulur: sorgulanan kod arşive yazılır, `uk-measures-archive`
  döngüsü eksik/yaşlanmış kodları koşu başına `UK_MEASURES_BATCH` kadar, istekler arasında
  `UK_MEASURES_DELAY_SECONDS` bekleyerek doldurur ve `UK_MEASURES_REFRESH_DAYS` sonra tazeler.
  Arşiv tazeyse ağa hiç çıkılmaz; **BK erişilemezse son bilinen oranlar "son alınan tarih"
  notuyla sunulur** (sessizce boş dönmez). Üçüncü ülke vergisi, menşeye özgü tercihli oran
  (coğrafi grup üyeliği ve istisna ülkeler dâhil), kota, damping ve yasaklar resmî ölçü
  satırlarından okunur.
* **İsviçre** — BAZG, tarife numarası yapısını (`TN_STRUCTURE`, ~28 MB CSV) açık veri olarak
  yayımlıyor: kod, Almanca/İngilizce/Fransızca eşya tanımı ve geçerlilik tarihleri. `ch_nomenclature`
  veri seti olarak günlük eşitlenir (aynı inceleme kapısı ve değişiklik defteri) ve sorguda HS-6 ile
  eşleşen İsviçre tarife numaraları tanımlarıyla döner. **Oran bu dosyada yayımlanmaz**; vergi için
  Tares sorgu ekranının bağlantısı verilir ve arayüzde bu ayrım açıkça yazılır.
* **Avrupa Birliği (TARIC)** — resmî açık uç nokta yayımlanmıyor: danışma ekranı oturum/POST ile
  çalışıyor (kod içeren GET sorgusu sonucu değil arama formunu döndürüyor); TARIC ham verisi CIRCABC
  üzerinden hesap gerektiriyor. Oran **çekilmez**; `data/official/foreign_tariff_links.json`
  kataloğundan sorguyu resmî ekranda hazır açan doğrulanmış derin bağlantılar üretilir ve arayüzde
  "otomatik oran alınamıyor" notu görünür. (AB'nin **EBTI karar verisi** ayrıdır ve alınır — bkz. altta.)

**AB vergi oranları (TARIC)** (`eu_taric.py`): Komisyon TARIC'i yalnız danışma ekranı olarak
yayımlıyor — o ekran robots politikasıyla otomatik erişime kapalı ve kod içeren GET sorgusuna
sonuç değil arama formu döndürüyor (canlı sınandı). Ham veri ise *TARIC & Quota Data and
Information* CIRCABC grubunda aylık XLSX çıkarımları hâlinde yayımlanıyor, fakat grup
listelemesi hesap istiyor. Bu boşluk, aynı **resmî aylık çıkarımı** işleyen Apify aktörü
(`nordicdataforge/eu-taric-customs-measures-monitor`) üzerinden kapatılır.

Kaynak **sorgu başına ücretli** olduğu için tasarım buna göredir: varsayılan **kapalı**
(`EU_TARIC_ENABLED=0`), arka planda kendiliğinden hiç çalışmaz, yalnız kullanıcı sorguladığında
çağrılır ve her sonuç `eu_taric.sqlite3` içindeki kalıcı arşive yazılır — alınan bir kod × ülke
çifti `EU_TARIC_REFRESH_DAYS` boyunca taze sayılır ve o süre dolmadan, hangi takvim ayında
olursa olsun, yeniden ücretlendirilmez.
Kaynak hata verirse arşivdeki son bilinen özet "son alınma" notuyla sunulur. `APIFY_TOKEN`
yalnız ortam değişkeninden okunur, `Authorization` başlığıyla gönderilir (URL'ye yazılmaz) ve
hiçbir hata metnine veya günlüğe sızmaz; başlıkta taşınamayacak bir jeton sessizce çökmek yerine
temiz bir "kapalı" durumu üretir.

**Tüm fasılları kapsayan toplu dolum** (`EU_TARIC_FILL_ENABLED=1`) aday kodları Türk tarife
cetvelinden türetir: GTİP'in ilk 8 hanesi AB Kombine Nomanklatürü, 9-10. haneleri AB'nin TARIC
alt açılımı, 11-12. haneleri ulusaldır — bu yüzden `hs10` düzeyinde ilk 10 hane doğrudan AB'de
sorgulanacak koddur (`hs6` düzeyi daha kaba ve daha ucuzdur). Dolum üç kapıdan geçer: döngü
açık olmalı, **aylık harcama tavanı** (`EU_TARIC_MONTHLY_BUDGET_USD`, varsayılan `0` = hiç
sorgu yok) aşılmamış olmalı ve kod × ülke çifti arşivde **taze** olmamalıdır. Her tur
`EU_TARIC_FILL_BATCH` kadar çift işler, aktöre tek çağrıda en fazla `EU_TARIC_MAX_CODES` kod
gönderir, sonucu arşive yazar ve harcamayı `fill_spend` tablosuna işler. Aktör bir grubu
`400` ile reddederse (grupta tek bir geçersiz kod bütün grubu düşürebiliyor) kodlar **tek tek**
yeniden denenir; geçerli olanlar kurtarılır, reddedilen kod kaydedilmeden bırakılır ve bir
sonraki turda yeniden denenir. Geçersiz kod ücretlendirilmediği için bu kurtarma ek maliyet
doğurmaz. `500` gibi geçici hatalarda tek tek deneme yapılmaz, tur boşuna uzamaz. Çağrılar
arasında `EU_TARIC_FILL_DELAY_SECONDS` kadar beklenir (BK arşivindeki
`UK_MEASURES_DELAY_SECONDS` deseni). Aktörün hata metni — jeton maskelenerek — dolum
hatalarına yazılır, böylece reddin sebebi görülebilir. AB'de beyana elverişli
olmayan kod `not_declarable` olarak işaretlenir (aktör bunları ücretlendirmez) ve
`EU_TARIC_NOT_DECLARABLE_RETRY_DAYS` (varsayılan 180 gün) geçmeden tekrar denenmez;
başarısız tur hiç kaydedilmez, bir sonraki turda yeniden denenir.

**Tazelik takvim ayına değil kaydın yaşına bakar.** Bir kez indirilen kod × ülke çifti
kalıcıdır; yalnız `EU_TARIC_REFRESH_DAYS` (varsayılan **90 gün**) geçtikten sonra yeniden
sorgulanır — hangi ayda alınmış olduğu fark etmez. Takvim ayına bakan bir kural, ayın 1'inde
tüm katalogu yeniden satın almak demekti: kaynak o ay yeni çıkarım yayımlamamışsa aynı satır
aynı anahtara yeniden yazılır, para gider ve tek bir yeni bilgi gelmezdi. Kuyruk
`foreign_tariff` arşivindeki desenle sıralanır: **önce hiç alınmamış kodlar, sonra tazelemesi
gelenler** — böylece bütçe önce kapsama harcanır, hiçbir kod açlığa düşmez. Harcama tavanı
aylık kalır (bütçe aylıktır), ama **iş kuyruğu ayla sıfırlanmaz**: $100'lük bir tavanla ilk
dolum birkaç ayda tamamlanır ve orada durur. Kullanıcı sorgusu bayat bir arşiv kaydına
düşerse sonuç yine **ücretsiz** arşivden döner, `stale: true` ve `age_days` ile hangi tarihte
alındığı bildirilir. Yönetici
`GET /api/admin/eu-taric/fill` ile aday sayısını, kalan işi ve tahmini maliyeti **ücret
doğurmadan** görebilir, `POST` ile tek turluk dolum çalıştırabilir; `/health` içinde
`eu_taric_fill_pending` ve `eu_taric_fill_total` alanları ilerlemeyi gösterir; plan çıktısı
`pending_pairs` (hiç alınmamış), `refresh_due_pairs` (tazelemesi gelen) ve
`estimated_monthly_usd` (kataloğun tazeleme payı) olarak ayrışır.

`resolve_rates()` ölçü satırlarından **koşullu** bir özet çıkarır: üçüncü ülke vergisi (ERGA
OMNES), menşeye özgü oran (gümrük birliği / tercihli / askıya alma), ek vergiler (damping,
telafi edici, korunma, tarım bileşeni) ve gereken belgeler (ör. A.TR için `N018`). **Tek bir
"nihai vergi" sayısı iddia edilmez** — TARIC'te oran ek koda, kotaya, belgeye ve nihai kullanıma
bağlıdır — ve hiçbir değer `calculate_landed_cost` girdisine aktarılmaz. Rotalar:
`GET /api/foreign/eu-taric?gtip=&origin=` ve `/status`; MCP'de `lookup_eu_taric_measures`.
Toplu kullanıma geçmeden önce `scripts/eu_taric_validation.py` 10 kodu çözümleyip her biri için
resmî TARIC ekran bağlantısını yazar; karşılaştırma elle yapılır.

**AB Bağlayıcı Tarife Bilgisi (EBTI) kararları** (`ebti_decisions.py`): Avrupa Komisyonu, üye
ülke gümrük idarelerinin verdiği BTB kararlarını `daily_publications.jsp` sayfasında her gün bir
ZIP/CSV dosyası olarak **herkese açık** yayımlar (giriş gerekmez). `ebti-sync` döngüsü listeyi okur,
henüz alınmamış günlük dosyaları en eskiden başlayarak indirir (koşu başına `EBTI_MAX_FILES_PER_RUN`
dosya), ZIP'i güvenli biçimde açar (yalnız `.csv` üye, zip-slip ve açılmış boyut denetimi) ve
kararları `ebti_decisions.sqlite3` içindeki FTS5 indeksine yazar. Her dosya bir anlık görüntüdür;
inceleme kapısından geçer (`review_service.engines["ebti"]`) ve yalnız onaylı yayınların kararları
aramada görünür. Her satır: karar referansı, veren ülke, geçerlilik aralığı, nomenklatür kodu,
**sınıflandırma gerekçesi** (GİR kuralları, fasıl notları, AS İzahnamesi, sınıflandırma tüzükleri),
eşya tanımı ve anahtar kelimeler.

İki kural koda gömülüdür: **`NAME_AND_ADDRESS` sütunu hiç saklanmaz** (sınıflandırma için gereksiz,
kişisel veriye komşu), ve her yanıt "bu kararlar Türkiye'de bağlayıcı değildir" notunu taşır.
Kararlar veren ülkenin dilinde yazılır; diller arası güvenilir anahtar nomenklatür kodudur.
Rotalar: `GET /api/foreign/ebti?gtip=&q=&limit=` ve `GET /api/foreign/ebti/status`; MCP tarafında
`search_eu_bti_decisions`. Kararlar hibrit indekse `ebti` korpusu olarak beslenir ve ön
değerlendirmede aday GTİP ile eşleşenler kanıt defterine `ebti_…` kimlikli kaynak olarak girer.

Eşleşme HS-6 düzeyindedir (Türk 12 haneli GTİP'inin ilk 6 hanesi ortaktır; sonraki haneler ulusaldır
ve eşleştirilmez). **Hiçbir yurt dışı oran `calculate_landed_cost` girdisine aktarılmaz**; Türkiye
maliyeti yalnız Türk resmî anlık görüntüleriyle hesaplanır. Her dış çağrı
`security_firewall.validate_outbound_url` ile yalnız `trade-tariff.service.gov.uk` alan adına,
her yönlendirme adımında yeniden doğrulanarak yapılır. Rotalar: `GET /api/foreign/tariff?gtip=&origin=
&jurisdiction=uk|eu|ch|all&as_of=` (30/dk, `foreign_tariff` özellik kilidi — Uzman paketi ve üstü) ve
`GET /api/foreign/tariff/status`; MCP tarafında `compare_foreign_tariff` aracı. UK nomenklatür
tanımları hibrit indekse `foreign_tariff` korpusu olarak beslenir, böylece İngilizce ürün ifadeleri de
sınıflandırma kanıtına girer. Ön değerlendirme kanıt defterine AB/İsviçre/BK resmî sorgu bağlantıları
`foreign_…` kimlikli kaynak olarak eklenir (ağ çağrısı yapılmadan).

Sınıflandırma ve ön değerlendirme bu indeksten **dipnotlu kanıt** alır. `classify_product` model
çağrısından önce ürün tanımı ve evsaf metniyle nomenklatür/tarife tanımları, AB tüzük sayfaları ve
önlem ürün tanımları korpuslarından en iyi 8 belgeyi çeker; belgeler isteme `official_evidence`
bloğu olarak `hyb_…` kimlikleriyle girer ve model yanıtındaki `evidence_ids` yalnız verilen kimlik
kümesine karşı temizlenir (uydurma kimlik düşer, aday başına en fazla 5). Buna ek olarak aday GTİP
ön ekiyle örtüşen indeks belgeleri deterministik `nomenclature_matches` olarak hesaplanır ve eşleşen
adayın güven puanı +10 artar. `evidence_pack` ise soru ve ürün tanımı için kanıt defterine en fazla
6 `hyb_…` kaynağı ekler; alıntılar modele `sanitize_untrusted_context` sonrası gider ve arayüzde bu
kaynaklar "anlamsal eşleşme" rozetiyle görünür. Hibrit indeks bağlı değilse ya da gömme sağlayıcısı
yoksa hiçbir kanıt eklenmez; istem ve çıktı bugünküyle birebir aynı kalır.

Arayüzde Ticaret Bakanlığının yedi bilgi katmanı canlı kayıt sayılarıyla ayrı gösterilir; kaynak, belge türü, yıl ve mülga durumu filtrelenebilir. Seçilen kaydın resmî kaynak zinciri, tam metni ve kopyalanabilir atfı aynı ekranda açılır. **Genel mevzuat** görünümü Bedesten resmî servisine bağlı ayrı arama alanıdır.

> Coolify dağıtımı v1.8.0 sağlık, web arayüzü ve MCP araç taramasıyla doğrulanır. Snapshot verilerini kalıcı tutmak için uygulamada `/data` hedefine persistent volume bağlayın; imaj `MEVZUAT_DATA_DIR=/data` ile hazır gelir.

### Google ile giriş ve SEO

Google OAuth istemcisinde **Web application** türü seçilir ve yetkili yönlendirme adresi olarak yalnızca şu tam adres eklenir:

```text
https://gumruksor.com/auth/google/callback
```

Coolify ortam değişkenleri:

```text
PUBLIC_BASE_URL=https://gumruksor.com
ADDITIONAL_ALLOWED_ORIGINS=<geçiş dönemi için ek adresler, virgülle ayrılmış, isteğe bağlı>
GOOGLE_CLIENT_ID=<Google OAuth web client ID>
GOOGLE_CLIENT_SECRET=<Google OAuth client secret>
AUTH_SESSION_SECRET=<en az 32 karakter kriptografik rastgele değer>
GOOGLE_SITE_VERIFICATION=<Search Console doğrulama kodu, isteğe bağlı>
```

`GOOGLE_CLIENT_SECRET` ve `AUTH_SESSION_SECRET` hiçbir zaman tarayıcıya gönderilmez. OAuth access/refresh tokenları saklanmaz; yalnız doğrulanmış profil alanları ve imzalı birinci taraf oturum çerezi kullanılır. Anahtarlar eklenmediyse Google düğmesi kurulum uyarısı gösterir ve misafir erişimi çalışmaya devam eder.

### Alan adı bağlama (Cloudflare + Coolify)

Uygulamanın birincil adresi `https://gumruksor.com`'dur. Alan adını sıfırdan bağlamak veya
değiştirmek için adım adım rehber: [`docs/gumruksor-alan-adi-kurulumu.md`](docs/gumruksor-alan-adi-kurulumu.md).

`PUBLIC_BASE_URL` uygulamanın kendini tanıttığı tek adrestir; canonical etiketleri, `sitemap.xml`,
Google OAuth dönüş adresi, Stripe dönüş adresleri ve e-posta bağlantıları bu değerden üretilir.
Alan adı değişince yalnız bu değişkeni güncellemek yeterlidir.

`ADDITIONAL_ALLOWED_ORIGINS`, geçiş döneminde eski adresin de çalışmasını sağlar. Tarayıcıdan gelen
POST istekleri normalde yalnız `PUBLIC_BASE_URL` kaynağından kabul edilir; bu değişkene virgülle
ayrılmış ek adresler yazıldığında onlar da güvenilir sayılır. Boş bırakılırsa davranış değişmez.
Geçiş tamamlanıp eski adres kapatıldığında bu değişken tekrar boşaltılmalıdır.

```text
ADDITIONAL_ALLOWED_ORIGINS=https://www.gumruksor.com,https://mevzuat-mcp.seymata.com
```

### Abonelik, kota ve kanıt dosyaları

Google hesabıyla giriş yapan kullanıcılar Başlangıç, Uzman, Ekip ve Kurumsal paketlerini; aylık kullanım sayaçlarını ve sunucuda saklanan kanıt dosyalarını **Hesabım** alanında görür. Paketler kotanın yanında **özellik kilitleri** de taşır (`account_service.PLANS` → `capabilities`; katalog `FEATURES`): Uzman paketi menşe senaryosu karşılaştırma, detaylı sorgu ve PDF raporu; Ekip paketi buna ek olarak toplu hesap, tarih bazlı sorgu ve uyum uyarılarını; Kurumsal paket ayrıca API erişimini açar. PRD katman adları (Essentials/Pro/Premium/Premium+) yalnız iç takma addır (`PLAN_ALIASES`); paket kodları ve Stripe fiyat kimlikleri değişmez. Kilitli bir uç `403 feature_required` ve özelliği içeren paket listesiyle yanıt verir; Google girişi yapılandırılmamış kurulumlarda kilitler açıktır. Kullanıcı rolleri `user | consultant | editor | admin` olarak `users.role` sütununda tutulur; yönetici e-posta listesi her zaman önceliklidir, editör rolü veri inceleme kuyruğunu görür. Rol yönetim panelindeki kullanıcı tablosundan atanır (`PUT /api/admin/users/{sub}/role`, denetim günlüğüne yazılır). Görsel kalıcı olarak saklanmaz. Kanıt dosyası analiz sonucunu; kontrol zamanı, GTİP, menşe, yürürlük referansı, resmî URL’ler ve etkin tarife/kontrol snapshot SHA-256 değerleriyle birlikte JSON olarak saklar ve dışa aktarır. Yönetici adresleri virgülle ayrılmış `ADMIN_EMAILS` değişkeninden alınır; `/admin` paket/durum değişikliklerini denetim günlüğüne yazar.

Kart verisini uygulamaya almayan Stripe Billing + hosted Checkout için Coolify’a aşağıdaki Secret değerlerini ekleyin. Stripe Dashboard’da Uzman ve Ekip ürünlerinin aylık/yıllık tekrar eden TRY fiyatlarını oluşturup gerçek `price_` kimliklerini kullanın:

```text
ADMIN_EMAILS=<yönetici Google e-posta adresleri, virgülle ayrılmış>
STRIPE_SECRET_KEY=<sk_test_ veya canlıda sk_live_ ile başlayan gizli anahtar>
STRIPE_WEBHOOK_SECRET=<whsec_ ile başlayan endpoint imza anahtarı>
STRIPE_PRICE_EXPERT_MONTHLY=<price_ kimliği>
STRIPE_PRICE_EXPERT_YEARLY=<price_ kimliği>
STRIPE_PRICE_TEAM_MONTHLY=<price_ kimliği>
STRIPE_PRICE_TEAM_YEARLY=<price_ kimliği>
STRIPE_AUTOMATIC_TAX=false
```

Stripe webhook adresi `https://gumruksor.com/api/billing/stripe/webhook` olmalı ve yalnız `checkout.session.completed`, `customer.subscription.updated`, `customer.subscription.deleted`, `invoice.paid`, `invoice.payment_failed` olaylarını göndermelidir. Müşteri Portalı, paket değişikliği/iptal ve ödeme yöntemi yönetimini Stripe’ın barındırdığı sayfada yapar. `STRIPE_AUTOMATIC_TAX` yalnız Stripe Tax kayıtları hazırlandıktan sonra `true` yapılmalıdır. Anahtar, webhook secret veya Price ID’lerden biri eksikse ödeme güvenli biçimde kapalı kalır; paket ve fiyat seçimi tarayıcıdan değil sunucudaki katalogdan doğrulanır.

Landing page; canonical, Open Graph/Twitter kartları, `SoftwareApplication` ve `FAQPage` yapılandırılmış verisi, `/robots.txt`, `/sitemap.xml`, manifest ve indekslenmeyen `/app` çalışma alanıyla hazırdır. Google Search Console tarafında alan adı doğrulandıktan sonra `https://gumruksor.com/sitemap.xml` gönderilmelidir; indeks kararı ve sıralama Google'a aittir.

ChatGPT'de geliştirici modu açıkken **Ayarlar → Uygulamalar → Oluştur** ekranında bu adresi endpoint olarak verin, kimlik doğrulamayı **Yok** seçin ve **Araçları tara** ile 44 aracı yükleyin. Tarife, maliyet, ithalat kontrolü ve Gümrükçe araçları MCP Apps yapılandırılmış sonuç görünümünü destekler. Codex için:

```bash
codex mcp add mevzuat-mcp --url https://gumruksor.com/mcp
```

Responses API örneği:

```python
from openai import OpenAI

client = OpenAI()
response = client.responses.create(
    model="gpt-5.4",
    input="4458 sayılı Gümrük Kanununa göre bu ithalat işlemini resmî kaynaklarıyla değerlendir.",
    tools=[{
        "type": "mcp",
        "server_label": "turkiye_mevzuat_ticaret",
        "server_url": "https://gumruksor.com/mcp",
        "require_approval": "never",
    }],
)
print(response.output_text)
```

---
🌐 **En Kolay Yol: Ücretsiz Remote MCP (Claude Desktop için)**

Hiçbir kurulum gerektirmeyen, doğrudan kullanıma hazır MCP sunucusu:

1. Claude Desktop'ı açın
2. **Settings > Connectors > Add custom connector**
3. Açılan pencerede:
   * **Name:** `Mevzuat MCP`
   * **URL:** `https://mevzuat.surucu.dev/mcp`
4. **Save** butonuna basın

Hepsi bu kadar! Artık Mevzuat MCP ile konuşabilirsiniz.

> **Not:** Bu ücretsiz sunucu topluluk için sağlanmaktadır. Yoğun kullanım için kendi sunucunuzu kurmanız önerilir.

---
🪐 **Google Antigravity ile Kullanım**

1. **Agent session** açın ve editörün yan panelindeki **"…"** dropdown menüsüne tıklayın
2. **MCP Servers** seçeneğini seçin - MCP Store açılacak
3. Üstteki **Manage MCP Servers** butonuna tıklayın
4. **View raw config** seçeneğine tıklayın
5. `mcp_config.json` dosyasına aşağıdaki yapılandırmayı ekleyin:

```json
{
  "mcpServers": {
    "mevzuat-mcp": {
      "serverUrl": "https://mevzuat.surucu.dev/mcp/",
      "headers": {
        "Content-Type": "application/json"
      }
    }
  }
}
```

> 💡 **İpucu:** Remote MCP sayesinde Python, uv veya herhangi bir kurulum yapmadan doğrudan Google Antigravity üzerinden Mevzuat Bilgi Sistemi'ne erişebilirsiniz!

### Lokal `uv` Kurulumu — Kopyala-Yapıştır

> **Ön Gereksinimler:** Bilgisayarınızda **Python**, **`uv`** ([kurulum](https://docs.astral.sh/uv/getting-started/installation/)) ve **Node.js** ([indir](https://nodejs.org/en/download)) kurulu olmalı. (Node.js yalnızca aşağıdaki kurulum komutunu çalıştırmak için gerekir; MCP'yi `uvx` çalıştırır.)

Aşağıdaki **bloğun tamamını** terminale yapıştırın. Komut, Antigravity'nin okuduğu `~/.gemini/config/mcp_config.json` dosyasını sizin yerinize oluşturur/günceller (varsa diğer sunucularınız korunur):

**macOS / Linux** (Terminal):

```bash
node - <<'MEVZUAT'
const fs=require("fs"),os=require("os"),path=require("path");
const dir=path.join(os.homedir(),".gemini","config"),file=path.join(dir,"mcp_config.json");
fs.mkdirSync(dir,{recursive:true});
let cfg={};try{cfg=JSON.parse(fs.readFileSync(file,"utf8"))}catch{}
if(typeof cfg!=="object"||cfg===null||Array.isArray(cfg))cfg={};
if(typeof cfg.mcpServers!=="object"||cfg.mcpServers===null)cfg.mcpServers={};
cfg.mcpServers["mevzuat-mcp"]={command:"uvx",args:["--from","git+https://github.com/saidsurucu/mevzuat-mcp","mevzuat-mcp"]};
fs.writeFileSync(file,JSON.stringify(cfg,null,2)+"\n");
console.log("mevzuat-mcp eklendi -> "+file);
MEVZUAT
```

**Windows** (PowerShell):

```powershell
@'
const fs=require("fs"),os=require("os"),path=require("path");
const dir=path.join(os.homedir(),".gemini","config"),file=path.join(dir,"mcp_config.json");
fs.mkdirSync(dir,{recursive:true});
let cfg={};try{cfg=JSON.parse(fs.readFileSync(file,"utf8"))}catch{}
if(typeof cfg!=="object"||cfg===null||Array.isArray(cfg))cfg={};
if(typeof cfg.mcpServers!=="object"||cfg.mcpServers===null)cfg.mcpServers={};
cfg.mcpServers["mevzuat-mcp"]={command:"uvx",args:["--from","git+https://github.com/saidsurucu/mevzuat-mcp","mevzuat-mcp"]};
fs.writeFileSync(file,JSON.stringify(cfg,null,2)+"\n");
console.log("mevzuat-mcp eklendi -> "+file);
'@ | node -
```

Komut `mevzuat-mcp eklendi -> ...` çıktısını verdiğinde kurulum tamamlanmıştır. Antigravity'yi (açıksa kapatıp) yeniden başlatın; `mevzuat-mcp` araçları otomatik yüklenir.

> 💡 **İpucu:** Lokal kurulumda mevzuat kaynaklarına erişim doğrudan bilgisayarınızda `uvx` ile çalışır; uzaktan sunucuya ihtiyaç duymaz.

---
🚀 **Claude Haricindeki Modellerle Kullanmak İçin Çok Kolay Kurulum (Örnek: 5ire için)**

Bu bölüm, Mevzuat MCP aracını 5ire gibi Claude Desktop dışındaki MCP istemcileriyle kullanmak isteyenler içindir.

* **Python Kurulumu:** Sisteminizde Python 3.11 veya üzeri kurulu olmalıdır. Kurulum sırasında "**Add Python to PATH**" (Python'ı PATH'e ekle) seçeneğini işaretlemeyi unutmayın. [Buradan](https://www.python.org/downloads/) indirebilirsiniz.
* **Git Kurulumu (Windows):** Bilgisayarınıza [git](https://git-scm.com/downloads/win) yazılımını indirip kurun. "Git for Windows/x64 Setup" seçeneğini indirmelisiniz.
* **`uv` Kurulumu:**
    * **Windows Kullanıcıları (PowerShell):** Bir CMD ekranı açın ve bu kodu çalıştırın: `powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"`
    * **Mac/Linux Kullanıcıları (Terminal):** Bir Terminal ekranı açın ve bu kodu çalıştırın: `curl -LsSf https://astral.sh/uv/install.sh | sh`
* **Microsoft Visual C++ Redistributable (Windows):** Bazı Python paketlerinin doğru çalışması için gereklidir. [Buradan](https://learn.microsoft.com/en-us/cpp/windows/latest-supported-vc-redist?view=msvc-170) indirip kurun.
* İşletim sisteminize uygun [5ire](https://5ire.app) MCP istemcisini indirip kurun.
* 5ire'ı açın. **Workspace -> Providers** menüsünden kullanmak istediğiniz LLM servisinin API anahtarını girin.
* **Tools** menüsüne girin. **+Local** veya **New** yazan butona basın.
    * **Tool Key:** `mevzuatmcp`
    * **Name:** `Mevzuat MCP`
    * **Command:**
        ```
        uvx --from git+https://github.com/saidsurucu/mevzuat-mcp mevzuat-mcp
        ```
    * **Save** butonuna basarak kaydedin.
![5ire ayarları](./5ire-settings.png)
* Şimdi **Tools** altında **Mevzuat MCP**'yi görüyor olmalısınız. Üstüne geldiğinizde sağda çıkan butona tıklayıp etkinleştirin (yeşil ışık yanmalı).
* Artık Mevzuat MCP ile konuşabilirsiniz.

---
⚙️ **Claude Desktop Manuel Kurulumu**


1.  **Ön Gereksinimler:** Python, `uv`, (Windows için) Microsoft Visual C++ Redistributable'ın sisteminizde kurulu olduğundan emin olun. Detaylı bilgi için yukarıdaki "5ire için Kurulum" bölümündeki ilgili adımlara bakabilirsiniz.
2.  Claude Desktop **Settings -> Developer -> Edit Config**.
3.  Açılan `claude_desktop_config.json` dosyasına `mcpServers` altına ekleyin:

    ```json
    {
      "mcpServers": {
        // ... (varsa diğer sunucularınız) ...
        "Mevzuat MCP": {
          "command": "uvx",
          "args": [
            "--from",
            "git+https://github.com/saidsurucu/mevzuat-mcp",
            "mevzuat-mcp"
          ]
        }
      }
    }
    ```
4.  Claude Desktop'ı kapatıp yeniden başlatın.

---
🔑 **API Anahtarları (Opsiyonel)**

### Semantik Arama - Gemini veya OpenRouter

Tüm `search_within_*` araçlarında `semantic=True` ile doğal dilde arama yapabilmek için bir gömme
(embedding) sağlayıcısı gerekir. Sağlayıcı `EMBEDDING_PROVIDER` ile seçilir (`gemini`, `openrouter`,
`none`); boş bırakılırsa `GEMINI_API_KEY` varsa Gemini, yoksa `OPENROUTER_API_KEY` varsa OpenRouter
kullanılır, hiçbiri yoksa gömme kapalıdır.

1. **Gemini (önerilen, doğrudan Google AI Studio):**
   ```bash
   GEMINI_API_KEY=your_api_key_here
   EMBEDDING_MODEL=gemini-embedding-001   # varsayılan
   EMBEDDING_DIM=768                      # outputDimensionality, varsayılan 768
   ```
   İstekler `generativelanguage.googleapis.com/v1beta/models/{model}:batchEmbedContents` ucuna
   `x-goog-api-key` başlığıyla gider; sorgularda `RETRIEVAL_QUERY`, belgelerde `RETRIEVAL_DOCUMENT`
   görev tipi kullanılır, 429/5xx yanıtlarında 1,5/3/6 sn aralıklarla yeniden denenir.
2. **OpenRouter:**
   ```bash
   OPENROUTER_API_KEY=your_api_key_here
   EMBEDDING_MODEL=google/gemini-embedding-001        # 3072 boyut (varsayılan)
   # EMBEDDING_MODEL=intfloat/multilingual-e5-large   # 1024 boyut
   ```
3. Anahtar olmadan da tüm araçlar çalışır, sadece `semantic=True` kullanılamaz.

### Mistral OCR

CB Kararı ve CB Genelgesi gibi PDF tabanlı mevzuatlar için Mistral OCR kullanılır:

1. [Mistral AI Console](https://console.mistral.ai/) üzerinden API anahtarı alın
2. Environment variable olarak ayarlayın:
   ```bash
   MISTRAL_API_KEY=your_api_key_here
   ```
3. API anahtarı olmadan da sistem çalışır, ancak PDF'ler markitdown ile işlenir (daha düşük kalite)

---
🛠️ **Kullanılabilir Araçlar (MCP Tools)**

Bu FastMCP sunucusu LLM modelleri için **43 araç** sunar (üç resmî kaynak ailesi).

### A. mevzuat.gov.tr Araçları (21 araç)

Türe özel arama ve içerik araçları. Her mevzuat türü için ayrı tool'lar.

#### Kanun (Laws)
* **`search_kanun`**: Kanun başlık ve içeriklerinde arama yapar
* **`search_within_kanun`**: Kanun maddelerinde anahtar kelime veya semantik arama yapar

#### KHK (Decree Laws)
* **`search_khk`**: KHK başlık ve içeriklerinde arama yapar
* **`search_within_khk`**: KHK maddelerinde anahtar kelime veya semantik arama yapar

#### Tüzük (Statutes)
* **`search_tuzuk`**: Tüzük başlık ve içeriklerinde arama yapar
* **`search_within_tuzuk`**: Tüzük maddelerinde anahtar kelime veya semantik arama yapar

#### Kurum Yönetmeliği (Institutional Regulations)
* **`search_kurum_yonetmelik`**: Kurum yönetmeliği başlık ve içeriklerinde arama yapar
* **`search_within_kurum_yonetmelik`**: Kurum yönetmeliği maddelerinde anahtar kelime veya semantik arama yapar

#### Cumhurbaşkanlığı Kararnamesi (Presidential Decrees)
* **`search_cbk`**: CB Kararnamesi başlık ve içeriklerinde arama yapar
* **`search_within_cbk`**: CB Kararnamesi maddelerinde anahtar kelime veya semantik arama yapar

#### Cumhurbaşkanı Kararı (Presidential Decisions)
* **`search_cbbaskankarar`**: CB Kararı başlık ve içeriklerinde arama yapar
* **`get_cbbaskankarar_content`**: CB Kararı tam içeriğini getirir (PDF - OCR destekli)
* **`search_within_cbbaskankarar`**: CB Kararı içeriğinde anahtar kelime veya semantik arama yapar

#### CB Yönetmeliği (Presidential Regulations)
* **`search_cbyonetmelik`**: CB Yönetmeliği başlık ve içeriklerinde arama yapar
* **`search_within_cbyonetmelik`**: CB Yönetmeliği maddelerinde anahtar kelime veya semantik arama yapar

#### CB Genelgesi (Presidential Circulars)
* **`search_cbgenelge`**: CB Genelgesi başlıklarında arama yapar
* **`get_cbgenelge_content`**: CB Genelgesi tam içeriğini getirir (PDF - OCR destekli)
* **`search_within_cbgenelge`**: CB Genelgesi içeriğinde anahtar kelime veya semantik arama yapar

#### Tebliğ (Communiqués)
* **`search_teblig`**: Tebliğ başlık ve içeriklerinde arama yapar
* **`get_teblig_content`**: Tebliğ tam içeriğini getirir
* **`search_within_teblig`**: Tebliğ maddelerinde anahtar kelime veya semantik arama yapar

#### mevzuat.gov.tr Ortak Parametreler

**Arama Tool'ları için:**
* `aranacak_ifade`: Aranacak kelime veya kelime grupları (AND, OR, NOT operatörleri desteklenir)
* `tam_cumle`: Tam cümle eşleşmesi (exact phrase)
* `baslangic_tarihi` / `bitis_tarihi`: Tarih aralığı filtreleme
* `page_number`, `page_size`: Sayfalama

**İçinde Arama Tool'ları için:**
* `mevzuat_no`: Mevzuat numarası (arama sonucundan alınır)
* `keyword`: Aranacak anahtar kelime veya doğal dilde sorgu
* `semantic`: `True` ise semantik arama, `False` ise anahtar kelime araması (varsayılan: `False`)
* `case_sensitive`: Büyük/küçük harf duyarlılığı (sadece keyword modunda)
* `max_results`: Maksimum sonuç sayısı

### B. bedesten.adalet.gov.tr Araçları (5 araç)

Tüm mevzuat türlerini tek araçla kapsayan birleşik araçlar. Gerekçe ve içindekiler gibi ek özellikler sunar.

#### **`search_mevzuat`** - Birleşik Mevzuat Arama
Tüm 12 mevzuat türünde başlık ve içerik araması yapar.
* `phrase`: İçerikte tam metin arama (Solr sözdizimi)
* `mevzuat_adi`: Mevzuat adı/başlığında arama
* `mevzuat_no`: Mevzuat numarası filtresi
* `mevzuat_tur`: Mevzuat türü filtresi (KANUN, KHK, TUZUK, YONETMELIK, CB_KARARNAME, CB_KARAR, CB_YONETMELIK, CB_GENELGE, KKY, UY, TEBLIGLER, MULGA)
* `basliktaAra`: Sadece başlıkta ara (varsayılan: true)
* `tamCumle`: Tam cümle eşleşmesi (varsayılan: false)
* `resmi_gazete_tarihi`: Resmi Gazete tarihi filtresi (GG/AA/YYYY)
* `resmi_gazete_sayisi`: Resmi Gazete sayısı filtresi
* `page`, `page_size`: Sayfalama

#### **`get_mevzuat_content`** - Tam Metin Getirme
Bir mevzuatın tam metnini Markdown formatında getirir.
* `mevzuat_id`: Mevzuat ID'si (`search_mevzuat` sonucundan alınır, mevzuat numarası değildir)

#### **`search_within_mevzuat`** - Madde Bazında Arama
Bir mevzuatın maddeleri içinde anahtar kelime araması yapar.
* `mevzuat_id`: Mevzuat ID'si (`search_mevzuat` sonucundan alınır)
* `keyword`: Aranacak kelime veya Boolean ifade (AND, OR, NOT)
* `case_sensitive`: Büyük/küçük harf duyarlılığı (varsayılan: false)
* `max_results`: Maksimum sonuç sayısı (varsayılan: 25)

#### **`get_mevzuat_gerekce`** - Kanun Gerekçesi
Bir kanunun gerekçesini getirir (amaç, komisyon raporları, madde gerekçeleri).
* `gerekce_id`: Gerekçe ID'si (`search_mevzuat` sonucundan alınır)

#### **`get_mevzuat_madde_tree`** - İçindekiler / Madde Ağacı
Bir mevzuatın bölüm-madde hiyerarşisini getirir.
* `mevzuat_id`: Mevzuat ID'si (`search_mevzuat` sonucundan alınır)

### Arama Modları

**Keyword Modu** (`semantic=False`, varsayılan):
```
keyword: "yatırımcı AND tazmin"
```
Boolean operatörler (AND, OR, NOT) ile kesin kelime eşleşmesi. Operatörler BÜYÜK HARF olmalıdır.

**Semantik Mod** (`semantic=True`, sadece mevzuat.gov.tr araçları):
```
keyword: "yatırımcının zararının tazmini"
```
Doğal dilde anlam tabanlı arama. Kelime eşleşmesi aramaz, kavramsal benzerlik ile sonuç döner. `OPENROUTER_API_KEY` gerektirir.

---
📜 **Lisans**

Bu proje MIT Lisansı altında lisanslanmıştır. Detaylar için `LICENSE` dosyasına bakınız.
