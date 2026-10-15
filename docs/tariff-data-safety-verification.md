# Tarife hesabı veri güvenliği — 15 Ekim 2026

## Başlangıç ve kapsam

- Doğrulanan remote: `https://github.com/hiktan44/gumruksor.git`.
- Güncel `main`: `19b1a0b24a49fdce1c6261bc4e23c6437217d4cf`; PR hazırlığından önce
  `git ls-remote origin refs/heads/main refs/heads/test/testsprite-suite` ile tekrar doğrulandı.
- Çalışma dalı: `fix/tariff-data-completeness`; boş görev klasörüne temiz klon alındı.
  Başka çalışma kopyalarındaki kullanıcı değişikliklerine dokunulmadı.
- [PR #101](https://github.com/hiktan44/gumruksor/pull/101) birleşmiş durumda.
  [PR #103](https://github.com/hiktan44/gumruksor/pull/103) açık; HEAD
  `2da5225dd055102ba8146ad6285b8d085184c1c3`, yalnız 50 test/rapor dosyası ekliyor.
  Üç uygulama hatası da başlangıç kodunda hâlâ vardı. Önceki raporların yeşil
  sonuçları bağımsız yürütme kanıtı olarak kullanılmadı.

## Düzeltilen davranışlar

| Risk | Önce | Sonra |
| --- | --- | --- |
| EMY | Motor/veri eksikliği, sorgu hatası ve kısmi kapsamdaki eşleşme yokluğu `%0` kararı üretebiliyordu. | Kapsam belirsiz kalır; oran eksikse hesap `partial`, EMY tutarı ve nihai toplam `null`. Açık, otomatik kullanılabilir resmî sıfır satırı ve kullanıcının doğruladığı oran/sıfır korunur. |
| Toplu gözetim | Döviz, çapraz kur ve iki birim temizlemede kayboluyordu; eksik birimler eşit varsayılıyordu. | JSON/CSV/XLSX alanları taşınır, çapraz kur ondalık olarak ayrıştırılır. İki birim de gerekir; eksik/uyumsuz birim, eksik kur veya belge durumu varken KDV matrahı ve nihai toplam üretilmez. |
| Yürürlük | Uygun eski sürüm yokken gelecekteki etkin sürüme dönülüyordu; etkin sürümün bitişi kontrol edilmiyordu. | Onay ve `[valid_from, valid_to)` aralığı her sorguda denetlenir. Kapsamayan etkin sürüm yerine yalnız günü kapsayan onaylı eski sürüm kullanılabilir; yoksa kaynak/tarih eksikliği açıkça bildirilir. |

Önlem sorgusu tek kez, sorgunun geçmiş/bugün/gelecek tarihiyle yapılır. Sorgu ve
kaynak metadata hataları muafiyet sayılmaz; kapsam matrisinde eksiklik taşınır.
Mevcut İGV uygulanmazlık kararı, A.TR iletimi, KKDF sınıflaması ve kullanıcı/resmî
oran çakışması davranışları korunur.

## Değişen dosyalar

- `tariff_engine.py`: EMY belirsizliği, kaynak hataları, gözetim matrahı ve yürürlük seçimi.
- `bulk_costing.py`: dört gözetim alanı, Türkçe/İngilizce başlıklar ve CSV şablonu.
- `tests/test_tariff_data_safety.py`: 19 yeni sentetik regresyon testi; alt senaryolar dahil.
- `tests/test_bulk_costing.py`, `tests/test_faz0_tax_fixes.py`, `tests/test_tariff_engine.py`:
  önceki güvensiz sıfır/birim varsayımlarının beklentileri güncellendi.
- `scripts/run_offline_tests.py`: yeniden çalıştırılabilir, geçici veri kullanan test çalıştırıcısı.
- `PROJECT_NOTES.md` ve bu belge: devir ve doğrulama kanıtı.

## Bağımsız yürütme

Python **3.12.14**; mevcut `uv.lock` ile `uv sync --frozen --offline` başarılı.
Bağımlılıklar değiştirilmedi. Çalıştırıcı ortamdan kimlik bilgilerini kaldırır,
`.env` yüklemesini kapatır, veri/önbelleği yeni geçici klasöre yönlendirir ve
Python dış ağ bağlantılarıyla DNS çözümlemesini engeller. HTTP testleri ASGI
TestClient üzerinde çalışır; loglardaki alan adları canlı istek değildir.

```sh
.venv/bin/python scripts/run_offline_tests.py discover -s tests -p test_tariff_data_safety.py -v
.venv/bin/python scripts/run_offline_tests.py discover -s tests -v
.venv/bin/python -m compileall -q tariff_engine.py bulk_costing.py tests/test_tariff_data_safety.py tests/test_tariff_engine.py tests/test_bulk_costing.py tests/test_faz0_tax_fixes.py scripts/run_offline_tests.py
node --check web/app.js
git diff --check
```

- Yeni testler, değişmemiş başlangıç commit'inin ayrı `git archive` kopyasında:
  **19 test; 35 başarısız assertion/alt senaryo, 1 hata**. Hata, kasıtlı olarak
  bozuk kaynak metadata sorgusunun uygulamadan dışarı taşmasıdır.
- Düzeltilmiş kodda yeni testler: **19/19 başarılı**.
- Tam paket: **1.590 test; 1.580 başarılı, 10 atlandı, hata yok** (12,044 saniye).
  Node 24.14.1 PATH'e eklenerek sözdizimi testi de çalıştırıldı.
- Atlamalar: 9 test eşitlenmiş canlı tarife/kontrol önbelleği gerektirir;
  1 PDF testi Chromium tarayıcısı bulunmadığı için atlanır.
- Python derleme, Node sözdizimi, eklenen satırlarda sır deseni kontrolü ve
  `git diff --check`: başarılı.
- Yerel kanıtlar görev klasörünün `verification/` dizinindedir:
  `regressions-before-final.log`, `regressions-final.log`, `full-suite-final.log`.

JSON, CSV ve XLSX aynı gerçek tarife sorgusu ve hesap işlevinden geçirilir.
Sentetik 100 kg × 20.000 USD/ton örneği **2.000 USD** taban; 0,9 çapraz kurla
**1.800 EUR** taban verir. Ters kg/ton yönü, aynı birim, diğer döviz yönü, eksik
birim/kur, doğrulanmış belge/sıfır, kısmi kaynak, sorgu hatası, geçmiş, bugün,
gelecek, yalnız gelecekte başlayan kaynak, süresi biten ve onaysız eski sürüm
senaryoları kapsanır. Bunlar aritmetik regresyon örnekleridir; hukuki oran önerisi değildir.
HTTP 402/422 gibi ret yanıtları başarılı hesap kanıtı sayılmamıştır.

## Kalan sınırlar

Resmî veri kapsamı bu kod değişikliğiyle genişletilmez; eksik EMY verisi için
doğrulama/girdi gerekir. Hesabın `complete` olması kullanıcının doğruladığı
oranlara da bağlıdır; bütün mevzuatın otomatik doğrulandığını göstermez.
Gerçek müşteri verisi, canlı ERP, ücretli LLM, üretim kaydı ve üretim dağıtımı
kullanılmadı. Container/Trivy ve canlı entegrasyon doğrulaması yerelde çalıştırılmadı.
Main'e birleştirme yapılmadı.
