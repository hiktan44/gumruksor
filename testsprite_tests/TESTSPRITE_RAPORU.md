# Gümrüksor — TestSprite Test Raporu

**Tarih:** 2026-10-15
**Hedef ortam:** `https://gumruksor.com` (canlı)
**Araç:** TestSprite CLI
**Backend projesi:** `gumruksor-api` (`5cbccf6d-394e-4961-93ea-a49a554e158b`)
**Frontend projesi:** `gumruksor.com` (`497b5c81-c4ee-4439-94eb-34dbcfaf2f3c`)
**Dashboard:** https://www.testsprite.com/dashboard/tests/5cbccf6d-394e-4961-93ea-a49a554e158b

---

## 1. Özet

| Katman | Test | Geçti |
|--------|-----:|------:|
| TestSprite — Backend (public) | 39 | **39** |
| TestSprite — Frontend (public) | 4 | **4** |
| TestSprite — Backend (kimlik doğrulamalı / API anahtarı) | 6 | **6** |
| **TestSprite toplam** | **49** | **49** |
| Python unittest (oturum-korumalı özellikler) | 1571 | **1571** (1 skip) |

**Sonuç:** TestSprite'ta **49/49 geçti**; kimlik doğrulamalı ERP uçları API anahtarıyla kapsandı. Google oturumu gerektiren özellikler (hesap, admin, danışman, asistan, senaryo, tasarruf, izleme, faturalama) **1571 Python unittest** ile kapsanıyor (bugün yeniden koşuldu, hepsi yeşil). Tüm katmanlarda hata yok.

> **Kalan sınır:** Google oturumu isteyen `/app` SPA akışlarının *tarayıcı tarafı* TestSprite ile uçtan uca test edilemez (form-login OAuth redirect'i otomatikleştiremez). Bu özelliklerin iş mantığı unittest paketinde; tarayıcı akışını da TestSprite'a almak için test-only dev-login gerekir (bkz. §4).

---

## 2. Backend API — 39/39 PASSED

Testler canlı API'ye gerçek HTTP istekleriyle, somut assertion'larla (status kodu + yanıt gövdesi alanları) koşturuldu. Yüklemeden önce 39'u da yerel olarak doğrulandı.

### 2.1 Durum / sağlık uçları (10)
| Test | Doğrulanan |
|------|-----------|
| `test_health` | `/health` → 200 |
| `test_tariff_status_ready` | `/api/tariff/status` → `ready` + `active_snapshots` alanları |
| `test_classification_status` | `/api/classification/status` → 200 |
| `test_controls_status` | `/api/controls/status` → 200 |
| `test_ticaret_status` | `/api/ticaret/status` → 200 |
| `test_ictihat_status` | `/api/ictihat/status` → 200 |
| `test_gazette_status` | `/api/gazette/status` → 200 |
| `test_nomenclature_status` | `/api/tariff/nomenclature/status` → 200 |
| `test_declaration_status` | `/api/customs/declaration/status` → 200 |
| `test_foreign_statuses` | `/api/foreign/{eu-a2m,ebti,tariff}/status` → 3×200 |

### 2.2 Tarife motoru (11)
| Test | Doğrulanan |
|------|-----------|
| `test_tariff_lookup_textile` | GTİP 6109.10 → `matched`, mal tanımı dolu |
| `test_tariff_lookup_electronics` | GTİP 8517.13 → `matched/ambiguous`, 8517 prefiksi |
| `test_tariff_lookup_invalid_gtip` | geçersiz GTİP → `matched` dönmez (200/422) |
| `test_tariff_excise_cosmetics` | ÖTV: 3304.99 → `in_scope`, Liste IV |
| `test_tariff_excise_out_of_scope` | ÖTV: 6109.10 → `in_scope=false` |
| `test_tariff_vat_valid` | KDV: 6104.62 → kaynaklı oran |
| `test_tariff_vat_invalid` | KDV: geçersiz GTİP → 422 |
| `test_tariff_gts` | `/api/tariff/gts` → 200 |
| `test_tariff_countries` | ülke listesi Çin içerir |
| `test_tariff_autocomplete` | `q=telefon` → 8517 önerisi |
| `test_tariff_communiques` | tebliğ sayısı > 0 |

### 2.3 Gözetim / kontrol (4)
| Test | Doğrulanan |
|------|-----------|
| `test_controls_lookup_textile` | 6104.42 → `matched`, tebliğ 2026/18 |
| `test_controls_search_toy` | `q=oyuncak` → sonuç var |
| `test_controls_communiques_consistency` | `count == len(liste)`, ≥24 ithalat tebliği (2026/*) |
| `test_controls_lookup_unknown` | bilinmeyen GTİP → tutarlı durum döner |

### 2.4 Arama (4)
| Test | Doğrulanan |
|------|-----------|
| `test_search_mevzuat` | `/api/search` → belgeler, ilk sonucun başlığı |
| `test_ticaret_search` | `/api/ticaret/search` → belgeler |
| `test_search_hybrid` | `/api/search/hybrid` → 200 |
| `test_gazette_search` | `/api/gazette/search` → 200 |

### 2.5 AB / yabancı (2)
| Test | Doğrulanan |
|------|-----------|
| `test_foreign_vat_de` | AB KDV `iso2=DE` → Almanya, standart %19 |
| `test_foreign_vat_missing_iso2` | eksik `iso2` → 422 |

### 2.6 Yetkilendirme / plan (8)
| Test | Doğrulanan |
|------|-----------|
| `test_auth_me_unauthenticated` | `authenticated=false`, `google_enabled=true` |
| `test_plans_public` | plan listesi `starter` içerir |
| `test_account_requires_auth` | `/api/account` girişsiz → 401 |
| `test_admin_requires_auth` | `/api/admin/overview` girişsiz → 403 |
| `test_dossiers_requires_auth` | `/api/dossiers` girişsiz → 401 |
| `test_scenarios_requires_auth` | `/api/tariff/scenarios` girişsiz → 401 |
| `test_savings_requires_auth` | `/api/tariff/savings` girişsiz → 401 |
| `test_assistant_shared_notfound` | geçersiz paylaşım token → 404/410 |

---

## 3. Frontend (public sayfalar)

| Test | Durum | Doğrulanan |
|------|-------|-----------|
| Ana sayfa yüklenir + GTİP önerisi | ✅ passed | başlıkta "Gümrükçe", "GTİP" metni, giriş/başla çağrısı |
| `/app` girişsiz → Google ile giriş istenir | ✅ passed | korumalı içerik görünmez, giriş ekranı |
| `/gizlilik` açılır | ✅ passed | gizlilik/KVKK metni görünür |
| `/kullanim-kosullari` açılır | ✅ passed | kullanım koşulları metni görünür |

---

## 3.5 Backend — kimlik doğrulamalı ERP uçları (API anahtarı) — 6/6 PASSED

Ayrı projede (`gumruksor-api-auth`) koşuldu; proje düzeyinde **Bearer** credential (bir `gsk_` API anahtarı) enjekte edildi. Anahtar test koduna/commit'e girmez — kod başına TestSprite'ın enjekte ettiği `__AUTH_HEADERS__` kullanılır. API anahtarı yalnızca bu "ERP beyaz listesi" uçlarında kabul edilir.

| Test | Doğrulanan |
|------|-----------|
| `dossiers_list_authed` | `GET /api/dossiers` + Bearer → 200, liste |
| `dossier_by_id_notfound_authed` | `GET /api/dossiers/{yok}` → 404 (kimlik doğrulanmış, 401 değil) |
| `dossier_create_authed` | `POST /api/dossiers` → 201 + `id` |
| `bulk_authed` | `POST /api/tariff/bulk` (JSON rows) → 200 |
| `precheck_authed` | `POST /api/customs/precheck` → kimlik kabul (200/402), LLM+kota |
| `declaration_draft_authed` | `POST /api/customs/declaration-draft` → kimlik kabul (200/402/422) |

> **Yan etki notu:** `dossier_create_authed` her koşuda prod hesabında bir dosya oluşturur (API anahtarı `DELETE` edemez, yalnızca oturum silebilir). `precheck`/`declaration-draft` LLM çağırır ve kota harcar. Bu testleri her regresyonda değil, bilinçli smoke olarak koşmanız önerilir.

---

## 4. Yalnızca Google oturumu gerektiren özellikler (TestSprite tarayıcı kapsamı dışında)

Aşağıdaki uçlar API anahtarı kabul etmez; yalnızca Google oturum çereziyle erişilir. TestSprite form-login OAuth akışını otomatikleştiremediği için bunların *tarayıcı tarafı* uçtan uca test edilmedi. Hepsinin **iş mantığı 1571 Python unittest ile kapsanıyor** (bugün yeşil), ve API seviyesinde yetki reddi (§2.6) doğrulandı:

- Hesap / abonelik / API anahtarı yönetimi (`/api/account*`) — unittest: 21 dosya
- Admin paneli (`/api/admin/*`) — unittest: 25 dosya
- Gümrük asistanı — soru/akış/paylaşım (`/api/customs/assistant*`) — unittest: 3 dosya
- Senaryo & tasarruf (`/api/tariff/{scenarios,savings}`) — unittest: 4+2 dosya
- Danışman / danışma talepleri (`/api/consultants*`, `/api/consultation-requests*`) — unittest: 2 dosya
- Ürün sınıflandırma / görsel tanıma (`/api/customs/{classify,describe-image}`)
- İzleme listesi (`/api/watchlist`), Faturalama / Stripe (`/api/billing/*`)

> ERP beyaz listesindeki uçlar (dossiers, bulk, precheck, declaration-draft) API anahtarı kabul ettiği için **§3.5'te TestSprite ile kapsandı**.

**Tarayıcı akışını da TestSprite'a almak için — senin kararın:**
1. **Test-only dev-login** eklemek (staging'de, sadece test için kısa ömürlü giriş yolu). Üretim güvenlik yüzeyi olduğu için ayrı ve bilinçli bir karar gerektirir.
2. Mevcut **1571 Python unittest** paketini bu özelliklerin kapsama katmanı olarak sürdürmek (önerilen; TestSprite'ı public + ERP yüzeyi için kullanmak).

---

## 5. Yeniden çalıştırma

```bash
# Public backend — 39 test
testsprite test run --all --project 5cbccf6d-394e-4961-93ea-a49a554e158b

# Kimlik doğrulamalı ERP — 6 test (önce: project credential ile bir gsk_ anahtarı Bearer olarak ayarlanmalı)
testsprite test run --all --project 08a74690-6454-4f79-b841-ef1556bf824a

# Frontend — her test id ile (FE'de --all yok)
testsprite test run <testId> --wait

# Oturum-korumalı özellikler — Python unittest (yerel/CI)
uv run python -m unittest discover -s tests
```

Test kaynakları repoda: `testsprite_be/*.py` (39 public backend), `testsprite_be_auth/*.py` (6 kimlik doğrulamalı ERP), `testsprite_fe/*.json` (4 frontend plan).

> **Gizlilik:** API anahtarı (`gsk_…`) hiçbir test dosyasında/commit'te yoktur; TestSprite proje credential'ına enjekte edilir, kod başına `__AUTH_HEADERS__` olarak gelir. Yerel koşuda `TESTSPRITE_AUTH_CREDENTIAL` env değişkeninden okunur. Test bitince anahtarı `/app → API anahtarları`'ndan iptal etmeniz önerilir.
