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

`GEMINI_API_KEY` tanımlıysa birincil sağlayıcı doğrudan Google Gemini'dir (`gemini-3.8-flash` → `gemini-flash-latest`); yoksa `ZAI_API_KEY` ile Z.ai (görsel: `glm-5v-turbo`, `glm-4.6v`; metin: `glm-5.3`, `glm-5.3-flash`), o da yoksa OpenRouter kullanılır. Görsel modellerde "düşünme" adımı varsayılan olarak kapalıdır (`ZAI_VISION_THINKING=disabled`); bu, dakikalarca süren yanıtları önler. Her istek `LLM_REQUEST_TIMEOUT_SECONDS` (75 sn), birincil zincir `LLM_PRIMARY_BUDGET_SECONDS` (95 sn) ve tüm çağrı `LLM_TOTAL_DEADLINE_SECONDS` (150 sn) ile sınırlıdır; süre dolunca kullanıcı sağlayıcı adı içermeyen net bir hata görür. Birincil sağlayıcı yanıt vermezse kalan süre içinde sırasıyla Gemini (`GEMINI_MODELS`), kie.ai (`KIE_API_KEY`), Z.ai ve OpenRouter (`OPENROUTER_FALLBACK_MODELS`) yedek olarak denenir; birincil olan atlanır. **kie.ai** tek anahtarla çok sağlayıcılı, OpenAI uyumlu bir geçittir (GPT-5.x, Gemini 3.x, Claude). Diğerlerinden farkı: ortak bir `/chat/completions` yolu yoktur, her model kendi yolunda sunulur (`https://api.kie.ai/{model}/v1/chat/completions`) ve URL model başına üretilir; gövde OpenAI biçiminde kalır, şema JSON-nesne kipiyle sistem mesajında bildirilir (OpenRouter'a özgü `json_schema` ve `provider` blokları gönderilmez). Model kimlikleri kie'nin kendi düz adlarıdır (`gemini-3-8-flash-openai`, `gpt-5-2`); `vendor/model` biçimi burada geçerli değildir. OpenRouter yedeği varsayılan olarak kapalıdır (`LLM_FALLBACK_TO_OPENROUTER=1` ile açılır); `LLM_FALLBACK_TO_GEMINI=0` / `LLM_FALLBACK_TO_ZAI=0` diğerlerini kapatır. `LLM_PRIMARY_PROVIDER=zai|gemini|kie|openrouter` ile birincil sağlayıcı zorlanabilir. Gemini çağrıları, productanaliz projesinde canlıda doğrulanan yöntemle Google'ın yerel `generateContent` API'sine gider (sistem talimatı + `inlineData` görsel); 429/5xx yanıtları kısa aralıklarla yeniden denenir, 404 veya tekrarlayan 503'te sıradaki modele geçilir ve yanıt metnindeki JSON sunucu tarafında doğrulanır. Arayüzde sağlayıcı veya model adı gösterilmez; yalnızca "yapay zekâ analizi" ve güven düzeyi görünür. Z.ai tarafında 429 yanıtı **beyaz listeyle** değerlendirilir: yalnız bilinen geçici kod (`1302` eşzamanlılık sınırı) veya gövdesi okunamayan düz 429 yeniden denenir (3 ve 6 sn); bakiye (`1113`), abonelik ve yetki reddi gibi kalıcı hatalar **hiç beklemeden** zincirdeki sonraki modele düşer. Önceki kara liste kuralı bilinmeyen her hatayı geçici saydığı için kalıcı bir abonelik reddinde 9 saniye boşa gidiyordu. `GET /api/admin/llm-diagnostics` (yönetici; `?vision=1` görsel kipi, `?recent=1` yalnız son canlı çağrılar) her sağlayıcının zincirindeki **modelleri sırayla** dener — ilk başarılıdan sonrası atlanır, başarısız olan her model yine rapora yazılır (sağlayıcı başına en fazla 3). `healthy` yalnız "herhangi biri çalışıyor" demektir; asıl soruyu **`fallback_healthy`** cevaplar: birincil sağlayıcı çökerse tutacak başka bir model var mı. Başarısız her satır sağlayıcının yapısal hata kodunu (`error_code`) ve Z.ai'de o hatanın yeniden denenip denenmediğini (`retryable`) de taşır; böylece bir hatanın geçici mi kalıcı mı sayıldığı tahmin edilmez, okunur (`error_code: null` ise o sağlayıcı yapısal kod yayınlamıyor demektir ve hata geçici varsayılır). Rapor hiçbir koşulda anahtar değeri içermez, yalnız anahtarın tanımlı olup olmadığını (`keys`), model kimliğini, HTTP durumunu, gecikmeyi ve kısa hata metnini taşır.

Her çağrı katı JSON şeması ve `require_parameters=true` kullanır; bu nedenle görsel giriş veya yapılandırılmış çıktı desteği olmayan uçlar seçilmez. `data_collection=deny`, istemleri veri saklayabilen sağlayıcı uçlarına göndermemek için zorunludur. Nano Banana bir görsel üretim/düzenleme modelidir ve bu metin çıkarım akışında kullanılmaz. Model ürün adı, kategori, kapsamlı tanım, bileşim, kullanım, görünür menşe ibaresi, marka/model, ölçü, etiket, renk, fiziksel yapı, parçalar, çalışma mekanizması, ambalaj ve sınıflandırma sorularını ayrı alanlara çıkarır. Görselden belirlenemeyen menşe, teknik değer ve maliyet girdilerini uydurmak yerine kullanıcıya tamamlanacak bilgi olarak gösterir.

`OPENROUTER_API_KEY` yoksa arayüz uydurma yanıt üretmez; yalnızca güncel resmî kanıt paketini ve eksik bilgi listesini gösteren `evidence_only` modunda çalışır. Yüklenen JPEG/PNG/WebP görseli yeniden kodlanarak metaverisi temizlenir, kalıcı olarak saklanmaz ve istek başına 8 MB / 25 megapiksel sınırı uygulanır.

> Hukuki yorumlar bilgilendirme amaçlıdır. Sonuçlarda verilen resmî URL, tarih, sayı, mülga/yürürlük durumu ve varsa sonraki değişiklikler karar öncesinde doğrulanmalıdır.

### Resmî önlem listeleri, TCMB kuru ve günlük eşitleme

Tarife sorgusu ve maliyet hesabı, resmî listelerden okunan **ticaret politikası önlemlerini** GTİP ve menşe ile eşler ve tabloda gösterir:

* **Damping / sübvansiyon**: Ticaret Bakanlığı İthalat Genel Müdürlüğü'nün "Yürürlükteki Önlemler" çalışma kitabı (kesin ve geçici önlemler, ülke, oran/tutar, tebliğ ve bitiş tarihi).

**Tarife kontenjanında kapsam yalnız tarım ürünleridir ve ürün bunu açıkça söyler.** İndekslenen liste Ticaret Bakanlığı'nın *tarım ürünlerinde açılan tarife kontenjanları* sayfasıdır; **sanayi ürünleri tarife kontenjanları indekslenmemiştir**. Sebebi tahmin değil, ölçüm (16.09.2026): sanayi kontenjanı kararları Resmî Gazete'de, ek sayfaları gömülü fontla yazılmış PDF olarak yayımlanıyor — `pdfminer` metin yerine `(cid:60)(cid:63)…` döndürüyor ve **sıfır GTİP** çıkıyor; aynı karar Bedesten'den de ham PDF olarak dönüyor (`%PDF-1.5`, ikili veri), yani GTİP tablosu iki resmî kaynakta da makine tarafından okunamıyor. Tek teknik yol OCR'dır ve bir GTİP'te yanlış okunan tek rakam beyanname verecek kullanıcıya yanlış gümrük cevabı vermek demek olacağı için bilerek yapılmamıştır.

Bunun asıl riski veri eksikliği değil **yanlış negatiftir**: arayüz "Tarife kontenjanı — eşleşme yok" deseydi, sanayi ürünü ithal eden kullanıcı kontenjan olmadığını sanardı. Bu yüzden etiketler taranan listeyi adıyla söyler (*"Tarım ürünleri tarife kontenjanı"*), iş akışındaki korunma/kontenjan adımı sanayi kontenjanlarının taranmadığını yazar ve `customs_sources.json`'daki `industrial_tariff_quota` kaydı `access_mode: "manual_only"` ile listelenir — hiç çekilmez, resmî sayfaya yönlendirir. Bu kısıtlar testle kilitlidir.


**Önlem listelerinin dış bağlantı koruması** (`trade_measures.py`): bu modülün indirdiği adreslerin bir kısmı sabit değildir — bakanlık sayfasından **kazınan HTML'den** gelir (`discover_workbook_link`, `discover_quota_documents`). Yani hedef adres, üçüncü tarafın değiştirebileceği bir girdidir. Her istek ve **her yönlendirme adımı** `security_firewall.validate_outbound_url` ile `ticaret.gov.tr` ve `mevzuat.gov.tr` izin listesine karşı yeniden doğrulanır; yönlendirme istemciye bırakılmaz, çünkü izin listesindeki bir adres izin listesi dışına yönlendirebilirdi. İndirme boyutu 50 MB ile sınırlıdır ve sınır `content-length` başlığına değil **okunan bayta** uygulanır (başlık sunucunun iddiasıdır, ölçüm değil). İzin listesi dışı bir adres için hiç istek üretilmez.

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

#### Geçmiş sürümlerde arama (`as_of`)

Motor veritabanlarında geçmiş **zaten duruyordu** — her farklı sha256 ayrı bir anlık görüntü ve eski
sürümler hiç silinmiyor — ama arama katmanı onu görmüyordu: `unified_search` yalnız `active=1` satırları
sorguluyor, hibrit indeks besleme de yalnız aktif sürümü verdiği için her tazelemede eski sürümü indeksten
siliyordu. Artık ikisi de zaman boyutunu taşıyor.

`documents` tablosu korumalı `ALTER TABLE ... ADD COLUMN` ile `as_of_from`, `as_of_to` ve `snapshot_active`
sütunlarını aldı; besleyiciler (`hybrid_corpora.control_documents`, `classification_documents`)
`include_history=True` ile yürürlükten kalkmış sürümleri de veriyor. Belge kimliği zaten `snapshot_id`
taşıdığı için eski sürüm **ayrı bir belge** olarak yaşıyor; kimlik şeması değişmediğinden mevcut belgeler
yeniden gömülmüyor. Metin aynı kalıp yalnız yürürlük aralığı kapandığında belge yeniden gömülmez, yalnız
zaman sütunları güncellenir (`refresh` sayacında `retimed`).

Sorgu tarafında `as_of` **verilmezse davranış göç öncesiyle birebir aynıdır** (yalnız yürürlükteki sürüm);
bu bir gerileme kilidi testiyle korunuyor. `as_of` verilirse o güne ait sürüm döner. Uçlar:
`GET /api/search/unified`, `GET /api/tariff/autocomplete` ve `GET /api/search/hybrid` artık `as_of`
parametresi alıyor; bugün dışı bir tarih mevcut **`temporal_query`** yetenek kilidine tabidir (Ekip ve
üstü). MCP tarafında aynı yetenek `search_official_index` aracıyla kullanılabilir.

**Kesinlik rayı burada da geçerli:** geçmiş sonuç, geldiği anlık görüntünün `snapshot_id`,
`source_sha256` ve yürürlük aralığı künyesini taşır. Yürürlük aralığı bilinmeyen kayıt geçmiş
sorgusunda **elenir** — tarihi doğrulanamayan bir satırı "o gün yürürlükteydi" diye göstermek kanıtsız bir
iddia olurdu. AB sınıflandırma tüzüklerinde tablo `valid_from`/`valid_to` taşımadığı için sınırlar
**gözlemlenen** sınır olarak türetilir (bir sürüm, sonrakinin indirildiği güne kadar yürürlükte sayılır);
bu hukuki bir sınır iddiası değildir.

**Bu kapsamda olmayanlar (açıkça):** tarife eşya tanımları korpusunda geçmiş açılmadı — kimliği
`tariff:{gtip}` olduğu için geçmişi açmak ~20.000 belgenin kimliğini değiştirir ve tümünü yeniden gömmeye
zorlar; nomenklatür metni sürümler arasında neredeyse hiç değişmediği için bu maliyetin karşılığı yok.
Ayrıca **birleşik aramanın web arayüzü yoktur**: `/api/search/unified` uygulamada hiçbir yerden
çağrılmıyor, bu yüzden "yürürlük tarihi" alanı iliştirilecek bir arama kutusu da yok. Yetenek bugün API ve
MCP üzerinden kullanılabilir; arama sayfası ayrı bir iştir.

**Yurt dışı tarife karşılaştırma** (`foreign_tariff.py`, PRD Faz 4/8.7): aynı eşya için Türk
tarifesinin yanında Birleşik Krallık, Avrupa Birliği, İsviçre ve Amerika Birleşik Devletleri
tarifesi gösterilir. Dört ülke veriyi aynı biçimde yayımlamadığı için ürün bu farkı gizlemez:

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
* **Amerika Birleşik Devletleri (USITC HTS)** — `hts.usitc.gov/reststop/exportList` ucu anahtarsız
  ve ücretsizdir; BK'nin aksine **tek çağrıda** tüm tarife cetvelini (~36.000 satır, ~13 MB)
  General/Special/Other sütunlarıyla birlikte döner, bu yüzden ayrı bir emtia başına oran arşivi
  doldurma döngüsü gerekmez. `us_hts` veri seti günlük eşitlenir (aynı inceleme kapısı ve değişiklik
  defteri). Special sütunundaki parantez içi kısaltmalar (SPI, ör. `KR`, `AU`, `D`) **ISO ülke kodu
  değildir**; ABD Genel Not 3(c)'de tanımlı tercihli program göstergeleridir ve motor bunları
  yorumlamadan ham gösterir. Türkiye'nin ABD ile yürürlükte bir serbest ticaret anlaşması veya
  tercihli program ortaklığı yoktur (2018'de GSP kapsamından çıkarıldı); bu yüzden **Türk menşeli
  eşya için geçerli sütun her zaman "General" (NTR/MFN)'dir** ve sonuçta bu açıkça belirtilir.
  Column 2 (Küba/Kuzey Kore/Rusya/Belarus), ek vergi notu ve kota bilgisi ayrı ölçü satırları olarak
  gösterilir.

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
yeniden denenir ve geçerli olanlar kurtarılır. Bu kurtarma **eş zamanlı** çalışır
(`EU_TARIC_CONCURRENCY`, varsayılan 3, tavan 8): sıralı hâlinde 20 kodluk bir grup 20-30 dakika
sürüyor, tur bitene kadar hiçbir şey kaydedilmiyor ve kuyruk bozuk bir fasıla takılınca dolum
tamamen duruyordu (canlıda 45 dakika boyunca tek kod eklenmediği ölçüldü). Aktör **sonuç
başına** ücretlendirdiği için eş zamanlılık maliyeti değiştirmez, yalnız duvar saatini kısaltır;
`1` yazılırsa birebir eski sıralı davranışa dönülür. Kaynağı korumak için
`EU_TARIC_FILL_DELAY_SECONDS` beklemesi semaforun **içinde** yapılır, böylece eş zamanlılık
artsa da çağrılar arası aralık korunur. Kendi başına çalıştırılıp yine `400` alan kod
`actor_failed` olarak kaydedilir ve `EU_TARIC_FAILED_RETRY_DAYS` (varsayılan 7 gün) boyunca
yeniden denenmez — aktör bazı kodlarda çöküyor (`Actor run did not succeed … status: FAILED`)
ve kaydedilmezse bu kodlar her turda yeniden denenip kuyruğu tıkar; pencere kısa tutulur çünkü
çökme geçici de olabilir. Geçersiz kod ücretlendirilmediği için bu kurtarma ek maliyet
doğurmaz. `500` gibi geçici hatalarda tek tek deneme **yapılmaz** ve hiçbir şey kaydedilmez;
bütün grup bir sonraki turda yeniden denenir.

**Kuyruk katalog sırasında gezilmez.** Sıralı gezilseydi tek bir bozuk fasıl (canlıda 04 —
peynir kodları) arkasındaki her şeyi kilitlerdi. Hiç alınmamış çiftler kodun kararlı
BLAKE2s özetine göre sıralanır: bozuk bir bölge yalnız kendi payı kadar yavaşlatır ve arşiv
baştan itibaren bütün fasıllara yayılır, yani kullanıcı sorgularının isabet ihtimali erken
yükselir. Rastgelelik yoktur; aynı katalog her zaman aynı sırayı verir.

**Zaman aşımı ayrı ele alınır.** `run-sync` çağrısında yanıt hiç gelmezse (zaman aşımı,
bağlantı kopması) aktör sunucuda çalışmaya devam edip ücreti yazmış olabilir; bu yüzden grup
aynı turda **yeniden denenmez** ve grup boyutu (`chunk_size`) yarıya indirilir. Bir sonraki
tur daha küçük gruplarla dener, yani `EU_TARIC_MAX_CODES` fazla yüksek verilmişse sistem
kendi kendini düzeltir. Art arda `EU_TARIC_CHUNK_RECOVER_ROUNDS` (varsayılan 5) hatasız turun
ardından boyut kademeli olarak (×2, tavan `EU_TARIC_MAX_CODES`) geri büyür. Güncel değer
`fill.chunk_size` alanında, eş zamanlılık `fill.concurrency` alanında görünür. `fill.attempts`
çift başına **son** denemenin durum dökümünü verir (`ok` / `not_declarable` / `actor_failed`);
grup hâlinde sorgulamanın hâlâ işe yarayıp yaramadığı bu orana bakılarak karara bağlanır,
tahmin edilmez. Çağrılar
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

**Aynı verinin ücretsiz kaynağı: Access2Markets** (`access2markets.py`). Faz 4'te "AB'de
belgeli API yok" yazılmıştı; bu **yanlıştı** ve iki uydurma yol denenerek varılmış bir
sonuçtu. Portalın Angular paketi okunduğunda 58 gerçek uç çıktı ve 17.09.2026'da canlı
ölçüldü:

| Uç | Ne veriyor | Ölçülen |
|---|---|---|
| `api/tariffs/get/{kod}/{menşe}/{varış}` | üçüncü ülke vergisi, **gümrük birliği vergisi (Türkiye)**, tercihli tarife, askıya alma, kota, ek vergiler | 200 JSON, anahtarsız, 10/10 başarı, kod başına ~1,3 sn |
| `api/taxes/get/...` | varış ülkesinin iç vergileri | `VAT 19%`, `revisionDate 2026-07-01` (DE) |
| `api/v2/document/list?...` | ticaret koşulları: istenen belgeler | menşe şahadetnamesi, fatura, kıymet beyanı, navlun belgeleri |
| `webgate…/roo/public/v1/classic/chapter/{fasıl}/country/TR` | menşe kuralları (PEM Konvansiyonu) | 21,5 KB, ürün bazlı kural tablosu |

Bu kaynak ücretli yolun **yerine geçmez, yanında durur**: ikisi ortak
`eu_taric.summarise_measures` ile **aynı özet şeklini** üretir, bu yüzden arayüz, ihracat
dosyası ve beyanname tablosu tek bir şekil okumaya devam eder. Karar —"Apify durdurulsun
mu"— tahminle değil ölçümle verilir: `GET /api/admin/eu-taric/compare?limit=N` ücretli
arşivdeki çiftleri ücretsiz kaynakla karşılaştırır ve **ücret doğurmaz** (ücretli taraf
yalnız `archived()` ile okunur, aktör hiç çağrılmaz). Çıktı üç sayıyı ayırır: `agree`
(aynı oran), `disagree` (gerçek fark) ve `coverage_gap` (bir tarafta oran hiç yok — bu
"yanlış veri" değil "eksik veri"dir). Biçim farkı (`12.00 %` ↔ `12%`) uyuşmazlık sayılmaz.

Dürüst sınır: **varış ülkesi AB üyesi değilse bu uçtan oran okunmaz.** Kaynak o yönde ölçü
satırı yerine tarife şeması (`schemas`: GEN/MFN/tercihli) döndürüyor; onu oran diye okumak
"oran yalnız resmî anlık görüntüden" kuralını çiğnerdi. Sonuç `non_eu_destination` durumuyla
döner ve hiçbir sayı üretmez. Kodun AB'de karşılığı yoksa `not_found` yazılır — sıfır vergi
denmez. Değişkenler: `A2M_ENABLED` (varsayılan açık, ücretsiz), `A2M_REFRESH_DAYS` (45),
`A2M_FILL_ENABLED`, `A2M_FILL_BATCH`, `A2M_FILL_INTERVAL_SECONDS`, `A2M_DELAY_SECONDS`,
`A2M_DEFAULT_DESTINATION`. Uçlar: `GET /api/foreign/eu-a2m`, `GET /api/foreign/eu-a2m/roo`,
`GET /api/foreign/eu-a2m/status`, MCP aracı `lookup_eu_access2markets`.

**Kod düzeyi düşmesi (ölçümün zorunlu kıldığı dal).** 40 fasla yayılmış **120 gerçek
GTİP** ücretsiz kaynağa sorulduğunda **51'i (%42,5) 10 hanede boş döndü ama CN8'de veri
verdi.** Sebep yapısal: Türk GTİP'inin 9-10. haneleri AB'nin TARIC alt açılımıyla aynı
olmak zorunda değildir; karşılığı olmayan TARIC alt kodu AB'de yoktur, ama CN8 vardır.
Bu yüzden `lookup` sırayla 10 hane → CN8 → HS6 dener ve hangi düzeyden okuduğunu
`match_level` ile taşır, kullanıcıya da uyarı yazar: *"AB nomenklatüründe 5205410090
bulunamadı; oran 52054100 (CN8) düzeyinden okundu."* Sessizce daha kaba bir oran vermek,
bulunamadı demekten daha tehlikelidir. Düşmeyle birlikte ölçülen kapsam **120/120 (%100)**,
%97,5'inde üçüncü ülke vergisi ve %89,2'sinde Türkiye'ye özgü satır dolu.

**İhracat dosyasında sıra: önce ücretsiz, sonra ücretli arşiv.** AB'ye ihracatta
`customs_advisor._export_requirements` önce Access2Markets'i sorar; cevap alamazsa
ücretli TARIC **arşivine** düşer (`archive_only=True` — ücretli aktör ön değerlendirme
yolundan **asla** tetiklenmez, rota dakikada 20 isteğe açıktır). İkisi de veremezse
kademe dürüstçe düşer ve kullanıcıya ücretli canlı sorguyu kendi başlatma seçeneği
kalır. Ücretsiz çağrı `A2M_EXPORT_TIMEOUT_SECONDS` (varsayılan 12 sn) ile sınırlıdır:
kaynak yavaşlarsa dosya bekletilmez. Sıra ölçümle belirlendi — ücretsiz kaynak hem
daha geniş kapsıyor hem daha güncel (canlı portal ↔ aylık döküm).

**Toplu dolum ücretsiz olduğu için varsayılan açıktır** (`A2M_FILL_ENABLED=1`) ve
bütçe kapısı yoktur; yerine kaynağa saygı sınırları vardır. Ölçülen maliyet kod başına
**~2,3 sn** (istek 1,28 + kodların %42,5'inde CN8 düşmesi + 0,5 sn bekleme): 11.997
kodluk katalog **sıralı 7,7 saat**, `A2M_CONCURRENCY=3` ile **~2,6 saat** sürer.
Eş zamanlılık ücretsiz kaynakta maliyeti değiştirmez, yalnız duvar saatini kısaltır;
bekleme semaforun **içinde** yapılır, böylece eş zamanlılık artsa da kaynağa giden
istek sıklığı korunur. Döngü **iş varken** `A2M_FILL_BUSY_SECONDS` (10 sn) sonra
tekrar çalışır, **kuyruk boşalınca** `A2M_FILL_INTERVAL_SECONDS` (15 dk) aralığına
döner — sabit uzun aralık 200'lük turlarla kataloğu 15 güne yayıyordu. Bir kodun
hatası turu düşürmez; o kod bir sonraki turda yeniden denenir.

**Geçici hatada yeniden deneme, kalıcı hatada değil.** Canlı dağıtımdan sonraki ilk
ölçüm dolumun beklenenden çok yavaş ilerlediğini gösterdi (3,5 dakikada 5 kod) ve
`status().errors` alanında aralıklı HTTP hataları vardı — oysa aynı istekler başka bir
ağdan 24/24 başarılıydı (3 eş zamanlı, 4,2 sn). Yani sorun kodda değil, çıkış
yolunda ya da karşı tarafın anlık sınırındaydı. Teşhis kör kalmasın diye hata metni
artık **durum kodunu taşır** (`A2MHttpError(429)`; "HTTPStatusError" tek başına 403 mü
429 mu söylemiyordu) ve `429/500/502/503/504` kodları `A2M_RETRY_ATTEMPTS` (3) kez,
`Retry-After` başlığına uyarak, üstel geri çekilmeyle tekrarlanır. `403` gibi kalıcı
kodlarda **yeniden denenmez** — kaynağı boşuna zorlamak doğru değil.

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
`security_firewall.validate_outbound_url` ile yalnız izinli resmî alan adına (`trade-tariff.service.gov.uk`,
`hts.usitc.gov`, ...), her yönlendirme adımında yeniden doğrulanarak yapılır. Rotalar: `GET /api/foreign/tariff?gtip=&origin=
&jurisdiction=uk|eu|ch|us|all&as_of=` (30/dk, `foreign_tariff` özellik kilidi — Uzman paketi ve üstü) ve
`GET /api/foreign/tariff/status`; MCP tarafında `compare_foreign_tariff` aracı. UK ve ABD nomenklatür
tanımları hibrit indekse `foreign_tariff` korpusu olarak beslenir, böylece İngilizce ürün ifadeleri de
sınıflandırma kanıtına girer. Ön değerlendirme kanıt defterine AB/İsviçre/BK/ABD resmî sorgu bağlantıları
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

### İthalat / İhracat yönü

Çalışma masasındaki formun en üstünde **İthalat / İhracat** seçimi vardır ve tüm dosyayı belirler. İhracat seçilince hedef ülke alanı açılır; kullanıcı ülkeyi yazar yazmaz, **kota harcamadan**, o ülke için hangi veriye sahip olduğumuz rozet olarak gösterilir (`/api/tariff/countries` yanıtındaki `export_data_tier` / `export_data_note`).

Hedef ülkede açılacak beyanname yanlış doldurulursa ciddi zarar doğar. Bu yüzden `export_requirements.py` uydurmayı **yapısal olarak** engeller:

| Veri düzeyi | Ülkeler | Gösterilen |
|---|---|---|
| `rates` | AB-27 (TARIC arşivi), Birleşik Krallık (resmî API), **ABD (USITC HTS)** | Gerçek oran; kaynak URL'i, tarihi ve SHA-256'sı alanın yanında. ABD'de Türk menşeli eşya "General" (NTR/MFN) sütununu alır — Türkiye'nin ABD ile tercihli anlaşması yoktur |
| `nomenclature` | İsviçre | Tarife numarası ve eşya tanımı; **İsviçre oran yayımlamaz** |
| `agreement_only` | Kayıt defterindeki kalan ülkeler | Yalnız anlaşma ve menşe/belge kuralı; oran **gösterilmez** |
| `none` | Tanınmayan ülke adı | Yalnız Türkiye tarafı ihracat prosedürü |

`destination_duty` yalnız `rates` düzeyinde taşınabilir; `build_export_requirements` düşük kademede enjekte edilen bir oranı da düşürür.

Beyannameye girecek her kalem bir **emin olma düzeyi** taşır: `verified` (resmî anlık görüntüden okundu; 90 günden eski kaynak otomatik olarak düşürülür), `check_required` (kuraldan türetildi veya eksik; notu nedenini yazar) ve `unavailable` (veri yok; alan **hiçbir koşulda değer taşımaz**). Hedef ülkenin KDV oranı kaynaklarımızda ürün bazında olmadığı için hiçbir koşulda `verified` olmaz. Alıcının kayıt numarasını (EORI vb.) sistem **üretemez** ama kullanıcı **Beyanname bilgileri** kutusundan girebilir; girildiğinde alan `check_required` olur. Üstündeki **beyanname hazırlık kapısı** iki ayrı ölçüt uygular: resmî veriden gelmesi gereken alan `verified` olmalıdır, yalnız beyan sahibinin bilebileceği alan (eşya tanımı, menşe beyanı, fatura, alıcı kimliği) ise **dolu** olmalıdır. Bu ayrım olmadan hiçbir dosya asla "hazır" olamazdı. Oran verisi olmayan ülkede kapı açıkça "bu dosyayla beyanname doldurulmamalı" yazar.

AB tarafı ön değerlendirmede `archive_only=True` ile sorgulanır, yani **ücretli Apify aktörü tetiklenmez**; arşivde satır yoksa kademe düşürülür ve kullanıcıya tek kod için ücretli canlı sorguyu kendi başlatma seçeneği (Uzman paketi `foreign_tariff` kilidi) verilir.

İhracatta Türk ithalat vergileri (GV, İGV, EMY, KDV, ÖTV, KKDF, gözetim, damping) hesaplanmaz ve gösterilmez; `deterministic_cost` `null`'dır. İş akışı 18 adımlık ihracat listesine döner (`tr-export-workflow-v1`).

**Hedef ülkede gümrük yükü** (`export_costing.py`, FAZ 8.4): ihracatçı bir sayı görürse ona göre fiyat verir, bu yüzden hesap dört kapıdan geçer ve herhangi biri kapalıysa **sayı üretilmez, sebep yazılır**.

1. **Veri düzeyi.** Yalnız `tier == "rates"` (AB-27 arşiv isabeti, Birleşik Krallık). Diğer ülkede `status: rates_unavailable` ve ülke adıyla gerekçe döner.
2. **Oranın biçimi.** Yalnız **saf ad valorem** oran çözülür. `10.2 % MIN 1.6 EUR/kg` bileşiktir; 10,2 sayısını alıp kıymetle çarpmak gerçek yükü **olduğundan düşük** gösterirdi. `1.6 EUR/kg` spesifiktir, kıymetten hesaplanamaz. İkisinde de `status: rate_not_calculable` döner ve neden hesaplanamadığı yazılır — gümrük kıymeti yine de gösterilir, çünkü kullanıcı matrahı bilip oranı kendisi uygulayabilir. `Free`/`Muaf`/`0 %` sıfır demektir, "bilinmiyor" değil.
3. **Tercih ispata bağlıdır.** Tercihli oran yalnız kullanıcı **Menşe ispat belgesi düzenlenecek mi** sorusuna evet dediyse uygulanır (`export_preference_proof`); aksi hâlde kötümser üçüncü ülke oranı esas alınır ve tercihli oranın neden kullanılmadığı yazılır. Bu, ithalat tarafındaki "tevsik yoksa Diğer Ülkeler oranı" kuralının aynasıdır. Alan bilerek `atr_certificate`'ten ayrıdır: ispat her hedefte A.TR değildir (BK ve Kore'de menşe beyanı, BAE'de anlaşma belgesi).
4. **KDV toplama girmez.** Hedef ülke KDV'si bizde fasıl kuralından türetilmiş bir **tahmindir** ve KDV mükellefi alıcı için çoğu zaman **indirilebilir/iade edilebilir** bir kalemdir. Ayrı satırda, `included_in_total: false` ile ve iki notla gösterilir; başlık toplamı (`total_duties`, `landed_before_vat`) KDV hariçtir.

Gümrük kıymeti hedef ülkede **CIF** esaslıdır (fatura + navlun + sigorta); navlun ve sigorta girilmemişse matrahın eksik kalacağı uyarısı düşer. Hesaplanamayan ek ölçüler (ör. `62.6 EUR/ton` damping) listelenir ama **toplanmaz**, toplamdan düşürüldükleri ayrıca uyarıda söylenir. Kalemleri **alıcı** öder; DDP teklif verilecekse ihracatçının maliyetine eklenir. Tutar bağlayıcı tarife bilgisi veya vergi görüşü değildir.


**İhracat kontrol listeleri (kısmi indeks).** `control_engine` artık yön taşır: `control_snapshots.direction` (korumalı göç, varsayılan `import`) ve `lookup(gtip, direction="import"|"export")`. İhracatta yalnız ihracat listeleri taranır; ithalat ÜGD tebliği ihracat dosyasına **asla** karışmaz.

İki keşif yöntemi bilerek farklıdır ve sebebi ölçülmüştür: ithalat ÜGD tebliğleri tek bir yıllık pakette (31/12, aynı Resmî Gazete) yayımlandığı için tek geniş süpürme yeter — Bedesten'de `"Denetimi Tebliği"` + 31/12 filtresi **24 kayıt** döndürüyor. İhracat listeleri ise farklı yıllara ve farklı mevzuat türlerine (Tebliğ, Cumhurbaşkanı Kararı, Kurum Yönetmeliği) dağılmış; ayrıca Bedesten'in başlık araması **bitişiklik (ifade) tabanlıdır** — çok kelimeli bir `mevzuatAdi`, o kelimeler başlıkta **ardışık** geçmedikçe sıfır döner. Ölçüm (16.09.2026): gerçek başlık *"DOĞAL ÇİÇEK SOĞANLARININ 2026 YILI İHRACAT LİSTESİ…"* iken `"Çiçek Soğanlarının"` (ardışık) **5 kayıt**, `"Çiçek İhracat"` ve `"Soğanlarının İhracat"` (ikisi de başlıkta var ama ardışık değil) **0 kayıt** döndürüyor. Bu yüzden her ihracat kaydı `control_sources.json` içinde kendi `discovery.terms` listesini taşır; **yalnız ilk terim** resmî aramaya gönderilir, kalanlar dönen başlıklar üzerinde yerel filtre olarak uygulanır. Böylece geniş tek bir kelimeyle arayıp doğru belgeyi başlıkta tüm terimleri arayarak seçmek mümkün olur.

`list_kind` üçüncü bir değer aldı: `scope` (kapsamda) · `prohibited` (yasak) · `licence_required` (ön izne bağlı). Bilinmeyen bir ek türü **en zayıf iddiaya** (`scope`) düşer.

**Ek listesi nerede duruyor: ölçülen gerçek.** İlk sürüm, ihracat eklerini ithalat tebliğlerindeki gibi konsolide metnin içinde aradı ve her turda `Ek-1 GTİP kapsamı ayrıştırılamadı` verdi. Canlı ölçüm (16.09.2026) sebebi gösterdi: **Bedesten'in konsolide metni eki içermez.** Her iki ihracat tebliği de "Ekleri için tıklayınız" diyen tek bir bağlantıyla biter ve Bedesten ek adresini **bazı** belgelerde `ekler` alanında mutlak URL olarak verir (`https://www.mevzuat.gov.tr/MevzuatMetin/yonetmelik/9.5.42768-Ek.docx`) — ama **hepsinde değil**: Doğal Çiçek Soğanları tebliğinde (`mevzuatId` 350040) `ekler` alanı hiç yoktur ve konsolide HTML yalnız göreli bir `href` taşır, yani çözülebilir bir adres yoktur. Bu yüzden **üç** ayrı yol kullanılıyor:

| Kayıt | Kapsam nereden | Liste türü | Neden |
|---|---|---|---|
| `IHR/OZON` | Madde 5 tablosu, tebliğ metninin içinde (`scope_table`) | `prohibited` | Metin birebir *"…malların ihracatı **yasaktır**"* diyor |
| `IHR/CICEK-SOGANI` | Maddelerde tek tek sayılan GTİP'ler (`scope_literal`) | `prohibited` + `licence_required` | Ek indirilemiyor (yukarıda) **ve** Ek-1 botanik familya/cins/tür bazlı olduğu için hiç GTİP içermiyor; kapsam tebliğin kendi metnindedir |

`scope_literal` yapılandırmadan kod okur ama **yapılandırma kaynak değil, iddiadır**: eşitlemede her kod resmî metinde gerçekten geçip geçmediğine bakılarak doğrulanır; geçmeyen kod satır üretmez ve hata olarak kaydedilir. Böylece tebliğ değişip bir kod çıkarıldığında ürün eski kodu göstermeye devam edemez. Çiçek soğanı kaydında: `0714.90.20.00.12` ve `1106.20.90.00.11` (salepgiller yumru ve drogları) Madde 4/1-a'nın *"ihracatı yapılamaz"* hükmü gereği `prohibited`; `0601.10.90.10.00` ise Madde 5/1'in kapsam kodudur ve `licence_required` olarak raporlanır — eşyanın türünün Ek-1'in (I) yasak, (II) kotalı veya (III) serbest sütunundan hangisine girdiği **indekslenmemiştir** ve resmî ekten teyit edilmelidir.

İki teknik sonuç: (1) ek artık tek bir `.docx` olabilir — bir `.docx` teknik olarak ZIP'tir ama içinde belge üyesi bulunmadığı için eski arşiv yolu aynı dosyada **sıfır** satır döndürüyordu, bu bir testle kilitlendi; (2) madde içi tablo artık iddia gücünü yapılandırmadan alır (`scope_table.list_kind`), varsaymaz — bir tablonun "kapsam" mı "yasak" mı olduğu resmî metinden okunur. Ek indirme yolu değişmedi ve korumaları aynen geçerli: her yönlendirme adımında yeniden `validate_outbound_url` (`mevzuat.gov.tr`), 50 MB sınırı; kabul edilen tür listesi kasten dardır (`.zip`, `.docx`, `.doc`, `.xlsx`, `.xls`, `.pdf`). Bilinmeyen bir ek türü sessizce boş liste değil, açık bir hata üretir — sessiz boş liste "bu üründe yükümlülük yok" gibi okunurdu.

Ozon tebliğinde **Madde 5/2** farklı bir yükümlülük tanımlar: `2903.76.10.00.00` ve `2903.76.20.00.00` içeren `8424.10` GTİP'li malların ihracatı Çevre, Şehircilik ve İklim Değişikliği Bakanlığı **iznine tabidir — yasak değil**. Bu, tablo sınırının nerede biteceğini belirledi ve dağıtım öncesi ölçümle yakalandı: segment bir sonraki madde başlığına kadar uzatılınca **17 kod** çıkıyor ve 17'ncisi `8424.10` oluyordu; yani izne tabi bir eşya "ihracatı yasak" diye etiketlenecekti. Bitiş deseni `^\(2\)` yapılınca tablo tam olarak Madde 5/1'in **16 satırında** kapanıyor. Bu bir gerileme testiyle kilitlendi ve test elle yazılmış bir desen değil, **sevk edilen yapılandırmanın kendisini** ölçüyor.

**Hazırlık ölçütü yöne göre farklıdır ve bu kasıtlıdır.** İthalatta `ready` **tam kapsam** ister (yıllık ÜGD paketi bütündür; eksik bir tebliğ "kontrole tabi değil" yanlış sonucunu doğurur). İhracatta `ready_export` **kısmi kapsam** yeterlidir — elimizdeki listelerden cevap verilir. Yönler birbirini kilitlemez: bir ihracat belgesi çekilemediğinde ithalat yolu çalışmaya devam eder, bu bir gerileme testiyle korunuyor.

**Her ihracat sonucu, eşleşme olsun olmasın, indeksin kısmi olduğunu yazar.** İhracı yasak ve ön izne bağlı malların tamamı, ikili kullanım ve yaptırım listeleri indekslenmemiştir — bunlar GTİP'e göre değil teknik/askerî kategoriye göre düzenlendiği için GTİP sorgusuna cevap veremezler. Eşleşme çıkmaması yükümlülük olmadığını göstermez. İş akışında `export_prohibitions` ve `export_product_control` eşleşme varsa kanıtlı sonuç yazar, yoksa `pending` kalır; `exporter_registration`, `dual_use_control` ve `vat_exemption_refund` her dosyada `pending` kalır ve "kapsam dışıdır" denmez.

**Değerlendirme cümlesi yönden türetilir.** Aynı liste türü ithalatta ve ihracatta farklı bir hukuki sonuç doğurur, bu yüzden cümle sabit yazılmaz: `prohibited` bir satır ihracat dosyasında *"…ihracı yasak eşya listesinde… bu eşya ihraç edilemez"*, ithalat dosyasında *"…ithali yasak… ithalat izni verilmez"* der. `licence_required` artık kendi cümlesini taşır (*"ön izne/ruhsata bağlı… yetkili kurumdan alınacak izne bağlıdır"*) ve kendi uyarısını ekler; önceden bu satır genel kapsam cümlesine düşüyordu, yani bir ön izin şartı sıradan bir kapsam bilgisine indirgenerek yükümlülük gizleniyordu.

**Yan düzeltme:** birleşik aramada yasak/ön izin satırı artık normal kapsam satırından ayrılıyor — kart `list_kind`, `list_label` ve `direction` taşıyor. Önceden üç sorgunun hiçbiri `list_kind` seçmediği için yasak listesindeki bir satır aramada sıradan bir kapsam satırı gibi görünüyordu.

Menşe/dolaşım belgeleri `countries.py` kayıt defterinden türetilir ama ifade tersine çevrilir: ithalatta belge *ibraz edilir*, ihracatta Türkiye *düzenler* (AB'de fasıla göre A.TR veya EUR.1, Birleşik Krallık ve Kore'de fatura üzeri menşe beyanı, BAE ve Katar'da anlaşmaya özgü belge, tercihsiz ülkede menşe şahadetnamesi). Hedef pazar uygunluk ipuçları (CE/UKCA, tekstil etiketleme, LVD/EMC, ISPM-15) **kullanıcının onayladığı görsel evsaflardan** türetilir ve her ipucu hangi alandan çıktığını gösterir; bunlar ipucudur, bulgu değildir.

**GTS (Genelleştirilmiş Tercihler Sistemi) kapsam teşhisi** (`GET /api/tariff/gts`): yürürlükteki İthalat Rejimi Kararı eki, gümrük vergisinden muaf veya indirimli GTS ülkelerini üç grupta sayar (EAGÜ, ÖTDÜ, GYÜ) ve motor bu tabloyu sorguda zaten kullanıyordu — ama tablo **hiçbir uçta görünmüyordu**. Bu, belirti vermeyen bir hata sınıfı doğuruyordu: resmî ekteki ülke adı `countries.py` kayıt defterinde çözülemiyorsa, kullanıcı o ülkeyi yaygın bir başka yazımla girdiğinde eşleşme olmaz, sorgu "Diğer Ülkeler" sütununa düşer ve vergi **olduğundan yüksek** çıkar. Fazla vergi eksik vergiden sessizdir: beyan reddedilmez, kimse şikâyet etmez, yalnız ithalatçı fazla öder.

**İlk canlı ölçüm hatayı hemen buldu (16.09.2026):** resmî ekte **62** GTS ülkesi var, bunların **yalnız 9'u** kayıt defterinde çözülüyordu — **53'ü çözülmüyordu**. Zarar `gumruksor.com` üzerinde rakamla doğrulandı (GTİP `610910000000`):

| Girilen menşe | Gümrük vergisi |
|---|---|
| `Burma/Myanmar` (resmî ekteki yazım) | **%0** |
| `Myanmar` (kullanıcının yazacağı hâl) | **%12** |
| `Kongo Demokratik Cum.` | %0 |
| `Demokratik Kongo Cumhuriyeti` | %12 |
| `Timor-Leste` | %0 |
| `Doğu Timor` | %12 |

Yani ekte muafiyeti olan bir menşe, adı farklı yazıldığı için **12 puan fazla** vergilendiriliyor ve hiçbir uyarı çıkmıyordu. Eksik 53 ülke `countries.py`'ye resmî yazımları ve yaygın Türkçe/İngilizce varyantlarıyla eklendi (94 → 147 kayıt). Hepsi düz `mfn` kaydıdır ve `column_1` açılmaz: bunlar anlaşma ülkesi değildir, tavizi Türkiye tek taraflı verir; GTS sütununu `gts_countries` tablosu seçer, kayıt yalnız adı çözer. Gerileme testi altı yazımın da aynı sütuna gittiğini kilitliyor.

Uç artık her satırı adıyla verir ve her biri için `resolved` bayrağı taşır; `?unresolved=1` yalnız çözülemeyenleri süzer. Rapor ayrıca grup sayılarını ve sektör istisnası satır sayısını gösterir. `resolved` yalnız **adın kayıt defterine bağlandığını** gösterir, oranın doğruluğunu değil; resmî ekteki yazımla yapılan sorgu her hâlükârda çalışır, çünkü tablo o yazımla anahtarlanmıştır. Sektör istisnaları (ör. `S-11a`) yorumlanmaz, sorguda aynen uyarı olarak gösterilir.

**İhracatta GSP (Form A / REX) için tablo tutulmuyor ve bu bilinçli bir karardır.** Bir ülkenin hangi ülkelere GSP tanıdığı o ülkenin *kendi* mevzuatıdır, ürün ve dönem bazında değişir ve Türkiye'de bunu gösteren resmî bir kayıt defteri yayımlanmaz. Elle derlenmiş bir yararlanıcı tablosu, doğrulanamayan yabancı hukuk iddiasını ürüne sokmak olurdu — deponun "oran/kural yalnız resmî kaynaktan" ilkesine aykırı. Bu yüzden tercihli ticaret anlaşması olmayan hedeflerde belge **koşullu** olarak listelenir ve teyidin alıcıdan veya hedef ülkenin gümrük idaresinden alınması gerektiği yazılır. Bu belge hiçbir dosyanın hazırlık durumunu kilitlemez.

**Hedef ülke KDV oranı** (`eu_vat.py`, `data/official/eu_vat_rates.json`, `GET /api/foreign/vat?iso2=&gtip=`): AB-27 için standart, indirimli, süper indirimli ve park oranları tutulur. AB'nin resmî veritabanı **TEDB** bu tarihte başsız sorguya kapalıdır — arama ucu her istekte 500 dönüyor ve Excel dışa aktarımı sunucu oturumundaki son aramayı verdiği için anonim istekte boş şablon geliyor (canlı olarak ölçüldü). Bu yüzden veri **elle derlenmiş bir tohumdan** gelir. Tohum **iki ayrı kanıt düzeyi** taşır ve bunlar birbirinin yerine geçmez: `verified` yalnız *resmî anlık görüntüden makine tarafından okundu* demektir ve TEDB başsız çalışmadığı için bugün **hiçbir satır** bu düzeyde değildir; `expert_confirmed` ise tablonun uygulamanın sahibi olan gümrük müşaviri tarafından teyit edildiğini gösterir — **27 satırın tamamı 15.09.2026 tarihinde teyit edilmiştir**. Teyit süresiz değildir: `EU_VAT_CONFIRMATION_MAX_AGE_DAYS` (varsayılan 180 gün) geçince düşer, "doğrulanmamış tohum" uyarısı geri gelir ve oranı son iki yılda değişen ülkeler (`verify_first`: Çekya, Estonya, Finlandiya, Malta, Romanya, Slovakya) yeniden öncelikli doğrulama listesine girer. Her satır o ülkenin resmî vergi idaresine bağlantı taşır. Günlük eşitleme TEDB'yi yoklar; TEDB 27 satırlık gerçek veri döndürdüğü gün tohum kendiliğinden resmî veriyle değişir — eksik veya boş yanıt tohumu **asla** ezmez.

İndirimli oran, kullanıcı tercihi gereği **GTİP faslından otomatik seçilir**: AB KDV Direktifi Ek-III'ün fasıla güvenilir biçimde eşlenebilen mal kalemleri (gıda 1-23 fasıl, alkollü içki ve tütün hariç; ilaç 30; kitap 49; canlı bitki 0601-0604; yakacak odun 4401; çocuk oto koltuğu, bisiklet, güneş paneli) kural tablosunda tutulur. **Bu bir öneridir, tespit değildir**: Ek-III kategorileri GTİP faslıyla birebir örtüşmez ve her üye devlet Ek-III'ü farklı uygular. Bu yüzden kural tetiklendiğinde sonuç her zaman `ambiguous` döner, standart oran adaylar arasında kalır ve beyanname alanı **hiçbir koşulda "doğrulandı"** olmaz — en fazla "kontrol gerekir" amber rozetiyle görünür.

### Beyanname taslağı (Tek İdari Belge / BİLGE kutuları)

`declaration_draft.py` ön değerlendirme sonucunu gümrük beyannamesinin kutularına eşler; `POST /api/customs/declaration-draft` JSON, CSV veya XML olarak döndürür (`declaration_draft` özellik kilidi: Ekip ve Kurumsal). Üretim **saf ve ağsızdır**, kota tüketmez ve taslak kanıt dosyasıyla birlikte `dossiers.draft_json` sütununda saklanır.

**Neden taslak, neden tescil değil:** beyanname tescili hukuken bağlayıcı bir işlemdir ve yanlış tescil ceza doğurur. Bu yüzden hedef "tek tuşla tescil" değil, beyan sahibinin inceleyip kendi tescil edeceği eksiksiz bir taslaktır. Eylemio köprüsü bugün **salt okunurdur** — istemcinin tanıdığı dört uç (`/api/auth/login`, `/api/connectors/accounts`, `.../read`, `/api/health`) arasında yazma ucu yoktur ve Eylemio yayımlanmış bir API belgesi sunmaz. Yazma ucunun belgesi geldiği gün bu taslak, gönderilecek gövde olarak kullanılır.

Kutular altı bölümde toplanır: beyan ve taraflar (kutu 1/37, 2, 8, 14, A) · sevkiyat ve taşıma (11, 15, 17, 18/21, 19, 20, 25, 29) · eşya (31, 32, 33, 34, 35, 38, 41) · kıymet ve fatura (22, 28, 44, 46) · vergilerin hesaplanması (47) · sunulan belgeler (44). Kullanıcı kendi payına düşen kutuları formdaki **Beyanname bilgileri** kutusundan girer; bu alanlar maliyet hesabına **girmez**.

Kesinlik rayı burada da aynıdır ve **hiçbir kutu uydurulmaz**:

* GTİP (kutu 33) yalnız kullanıcı 12 haneyi onayladıysa **ve** aynı kod resmî cetvel anlık görüntüsünde birebir eşleştiyse `verified` olur; kutu o anlık görüntünün URL'ini, tarihini ve SHA-256'sını taşır. Ön ek eşleşmesi veya onaylanmamış kod yeterli değildir.
* Vergi tutarları (kutu 47) **hiçbir koşulda** `verified` olmaz: tescil günündeki GK md. 30 kuru ve o gün yürürlükteki oran farklı olabilir. İhracat taslağında Türk ithalat vergileri hiç yer almaz.
* Değeri olmayan kutu `unavailable` olur ve **değer taşımaz**; boş bir kutu asla "kontrol gerekir" diye işaretlenmez.
* Taslak tek kalem varsayımıyla üretilir; çok kalemli beyannamede kutu 31-46 her kalem için ayrı doldurulur.

### ERP / dış sistem API anahtarları

`api_access` özellik kilidi (Kurumsal paket) artık gerçek bir erişim yolu açar. Kullanıcı Hesabım → **API anahtarları** sekmesinden anahtar üretir; anahtar `gsk_<önek>_<gizli>` biçimindedir ve **açık değeri yalnız üretildiği yanıtta bir kez** görünür. Veritabanında (`api_keys` tablosu) yalnızca SHA-256 özeti durur, yani bir veritabanı kopyası çalınsa bile anahtarlar geri üretilemez; denetim günlüğüne de yalnız etiket ve ön ek yazılır.

İstemci anahtarı `X-API-Key` başlığıyla (veya `Authorization: Bearer gsk_…` olarak) gönderir. Anahtar **kapalı bir beyaz listeyle** sınırlıdır — rota `_api_key_identity` çağırmıyorsa anahtarı hiç görmez:

| Uç | Ek kilit |
|---|---|
| `POST /api/customs/precheck` | `precheck` kotası |
| `POST /api/customs/declaration-draft` | `declaration_draft` |
| `POST /api/tariff/bulk` | `bulk_costing` |
| `GET /api/dossiers`, `POST /api/dossiers`, `GET /api/dossiers/{id}` | `dossier` kotası (POST) |

Beyaz liste dışındaki her şey anahtara kapalıdır: ödeme ve abonelik, hesap silme, yönetim panelleri, kanıt dosyası silme — ve **anahtar yönetiminin kendisi**. Anahtar üretme/listeleme/iptal rotaları bilerek yalnız çerez oturumuyla çalışır; aksi hâlde çalınan bir anahtar kendini yenileyerek kalıcı hâle getirebilirdi.

Kurallar her istekte yeniden okunur: paket düşerse anahtar aynı anda 403 `feature_required` vermeye başlar, iptal edilen anahtar bir sonraki istekte 401 alır. Her anahtarlı istek ayrıca `api_call` kotasına yazılır (Kurumsal pakette sınırsızdır) ve hesap panelinde "API çağrısı" sayacı olarak görünür; rotanın kendi kotası bundan bağımsız olarak işlemeye devam eder. Hesap başına en fazla **10 etkin anahtar** tutulabilir. `AgentTokenVerifier` (kısa ömürlü ajan JWT'si) bu yoldan **etkilenmez**; iki kimlik birbirine karışmasın diye yalnız `gsk_` ön ekli değerler anahtar sayılır.

Hazırlık kapısı üç sonuç verir: `ready` (tüm zorunlu kutular karşılandı), `needs_check` (yalnız kullanıcının dolduracağı kutular eksik) ve `blocked` (resmî veriden gelmesi gereken bir kutu — bugün GTİP — doğrulanamadı).

### Veri diski, veritabanları ve yedek

Ürünün kalıcı verisi ayrı bir veritabanı sunucusunda değil, `MEVZUAT_DATA_DIR` altındaki **SQLite dosyalarında** durur (`users`, `tariff`, `controls`, `changes`, `foreign_tariff`, `eu_taric`, `trade_measures`, `ebti_decisions`, `classification-evidence`, `hybrid_index`). Bu ölçekte (okuma ağırlıklı, tek konteyner, ~150 bin tarife satırı) WAL kipindeki SQLite yeterlidir ve Postgres bilinçli olarak eklenmemiştir; birden fazla sunucuya çıkıldığı gün bu karar yeniden değerlendirilmelidir (SQLite tek yazarlıdır).

Veri **her açılışta yeniden indirilmez**: her kaynak günde en fazla bir kez yoklanır, indirilen dosyanın SHA-256'sı alınır ve değişmemişse yeni anlık görüntü hiç yazılmaz. Dağıtımlar veriyi silmez — kalıcı disk konteynerden bağımsızdır.

İki risk `storage.py` ile görünür ve yönetilebilir hâle getirildi:

* **Disk doluluğu.** Disk dolarsa SQLite yazamaz ve eşitleme sessizce başarısız olur. `/health` artık `disk_percent_used`, `disk_free_bytes`, `data_bytes`, `backup_bytes` ve `last_backup_at` alanlarını taşır (yalnız toplam sayılar; dosya adı veya yol herkese açık uçta verilmez), yönetim panelindeki **Depolama & Yedek** sekmesi ise veritabanı başına boyutu, uyarıları ve yedekleri gösterir. %80'de uyarı, %90'da hangi veritabanlarının silinerek yer açılabileceğini söyleyen uyarı çıkar.
* **Yeri doldurulamaz veri.** Veritabanlarının bir kısmı kaybolursa geri getirilemez: `users` (hesaplar, kanıt dosyaları), `eu_taric` (ücret ödenerek toplanan arşiv), `changes` (değişiklik defteri), `tariff` ve `controls` (resmî sitelerin artık yayımlamadığı geçmiş sürümler — tarihli sorgunun dayanağı). Buna karşılık BK/ABD/İsviçre tarifesi, AB tüzükleri, EBTI kararları ve hibrit indeks resmî kaynaktan **ücretsiz** yeniden kurulur. Otomatik yedek bu ayrımı esas alır ve varsayılan olarak yalnız birinci grubu kopyalar; yeniden indirilebilenleri de yedeklemek diskteki yeri iki katına çıkarır, yani önlemeye çalıştığımız riski büyütürdü.

Kopya `VACUUM INTO` ile alınır (SQLite'ın kendi atomik komutu; açık yazarlar varken bile tutarlı tek dosya üretir — düz dosya kopyası WAL yüzünden bozuk kopya verebilirdi), veritabanı başına en yeni `BACKUP_KEEP` kopya saklanır ve **diskte yer yoksa yedek alınmaz** (sebebi rapora yazılır; zorlamak, korunmaya çalışılan hatayı üretirdi).

Aynı diskteki yedek kazara silme, bozulma ve hatalı göçe karşı korur; **diskin tamamen kaybolmasına karşı korumaz.** Sunucu dışına kopya almak için panelde her yedeğin yanındaki **indir** bağlantısı kullanılır (`GET /api/admin/storage/backup/{ad}`, yalnız yönetici; dosya adı doğrulanır, yedek dizininin dışına çıkan hiçbir ad kabul edilmez). Bu, ek ücret doğurmayan gerçek dış yedektir. Değişkenler: `BACKUP_ENABLED`, `BACKUP_INTERVAL_SECONDS`, `BACKUP_KEEP`, `BACKUP_DATASETS`.

### Abonelik, kota ve kanıt dosyaları

Google hesabıyla giriş yapan kullanıcılar Başlangıç, Uzman, Ekip ve Kurumsal paketlerini; aylık kullanım sayaçlarını ve sunucuda saklanan kanıt dosyalarını **Hesabım** alanında görür. Paketler kotanın yanında **özellik kilitleri** de taşır (`account_service.PLANS` → `capabilities`; katalog `FEATURES`): Uzman paketi menşe senaryosu karşılaştırma, detaylı sorgu ve PDF raporu; Ekip paketi buna ek olarak toplu hesap, tarih bazlı sorgu, uyum uyarıları ve beyanname taslağını; Kurumsal paket ayrıca API erişimini açar. PRD katman adları (Essentials/Pro/Premium/Premium+) yalnız iç takma addır (`PLAN_ALIASES`); paket kodları ve Stripe fiyat kimlikleri değişmez. Kilitli bir uç `403 feature_required` ve özelliği içeren paket listesiyle yanıt verir; Google girişi yapılandırılmamış kurulumlarda kilitler açıktır. Kullanıcı rolleri `user | consultant | editor | admin` olarak `users.role` sütununda tutulur; yönetici e-posta listesi her zaman önceliklidir, editör rolü veri inceleme kuyruğunu görür. Rol yönetim panelindeki kullanıcı tablosundan atanır (`PUT /api/admin/users/{sub}/role`, denetim günlüğüne yazılır). Yönetici e-postaları (`ADMIN_EMAILS`) aylık kotalardan muaftır; kullanım yine sayaçlara yazılır, yalnız sınır uygulanmaz. Kotası dolan kullanıcıya dönen `429 quota_exceeded` yanıtı hangi işlemin dolduğunu (`operation`) ve bir üst paketlerin kodunu, adını, aylık/yıllık fiyatını, o işlem için kotasını ve doğrudan satın alınabilir olup olmadığını (`upgrade[]`, `account_service.upgrade_options`) taşır; arayüz bu bilgiyle somut paketi ve "Paketi yükselt" düğmesini gösterir, ödeme dönüşünde hesap yeniden okunduğu için yeni kota anında geçerli olur. Görsel kalıcı olarak saklanmaz. Kanıt dosyası analiz sonucunu; kontrol zamanı, GTİP, menşe, yürürlük referansı, resmî URL’ler ve etkin tarife/kontrol snapshot SHA-256 değerleriyle birlikte JSON olarak saklar ve dışa aktarır. Yönetici adresleri virgülle ayrılmış `ADMIN_EMAILS` değişkeninden alınır; `/admin` paket/durum değişikliklerini denetim günlüğüne yazar.

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
