# Kayıtlı tarifeleri iki tarihte karşılaştırma

Gümrükçe’ye Sor → Sohbetle sor bölümüne vergi, tarih karşılaştırması ve kontrol sorgusu örnekleri eklendi. Örnekler soruyu doldurur; sorgu otomatik gönderilmez. Tarih örneğinde iki tarihi kullanıcı tamamlar.

Yeni `compare_tariff_dates` asistan aracı doğrulanmış 12 haneli GTİP, menşe ve iki geçmiş tarih alır. Mevcut tarife motoruyla aynı şartlarda iki kayıt okur. Bu araç arşiv eşitlemesi başlatmaz. Oran farkı yüzde puan olarak hesaplanır; kaynak arşiv adresi/hash ve tarih dayanağı taşınır.

Eksik geçmiş kayıt sıfır veya değişmedi sayılmaz. İndirme gözlemine dayalı tarih sınırı kesin yasal yürürlük değişikliği olarak sunulmaz. Başlangıç ikinci tarihten önce olmalı; gelecek tarihler reddedilir. Geçmiş arşiv yoksa karşılaştırma eksik olarak döner.

Yeni servis, veritabanı göçü veya BigQuery gerekmez. Mevcut asistanın kimlik doğrulaması, istek sınırı ve kaynak doğrulaması kullanılır.

Doğrulama: `TZ=UTC uv run python -m unittest tests.test_tariff_comparison tests.test_assistant -q` ve tüm unittest paketi.
