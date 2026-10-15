# Gümrüksor — TestSprite Test Raporu

**Tarih:** 2026-10-15
**Hedef ortam:** `https://gumruksor.com` (canlı)
**Araç:** TestSprite CLI
**Backend projesi:** `gumruksor-api` (`5cbccf6d-394e-4961-93ea-a49a554e158b`)
**Frontend projesi:** `gumruksor.com` (`497b5c81-c4ee-4439-94eb-34dbcfaf2f3c`)
**Dashboard:** https://www.testsprite.com/dashboard/tests/5cbccf6d-394e-4961-93ea-a49a554e158b

---

## 1. Özet

| Katman | Test | Geçti | Kaldı | Bekliyor |
|--------|-----:|------:|------:|---------:|
| Backend API | 39 | **39** | 0 | 0 |
| Frontend (public) | 4 | **4** | 0 | 0 |
| **Toplam** | **43** | **43** | **0** | **0** |

**Sonuç:** Canlı backend'in tüm ana özellik grupları ve public frontend sayfaları doğrulandı — **43/43 geçti**, hiç hata yok.

> **Önemli kısıt:** Uygulama girişi yalnızca **Google OAuth** ile. TestSprite form-tabanlı login yapar; Google'ın OAuth redirect/2FA akışını otomatikleştiremez. Bu yüzden login gerektiren `/app` SPA akışları (hesap, dosya, asistan, toplu hesap panelleri) TestSprite ile uçtan uca test **edilemedi** — API seviyesinde bu özelliklerin yetki kontrolü (401/403 doğru reddi) doğrulandı. Bkz. §4.

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

## 4. Kapsanamayan alanlar ve neden

Aşağıdaki özellikler **login gerektirdiği** ve giriş yalnızca Google OAuth ile olduğu için TestSprite ile uçtan uca test edilemedi. API seviyesinde yetki reddi (§2.6) doğrulandı ama kimliği doğrulanmış akışlar değil:

- Hesap / abonelik / API anahtarı yönetimi (`/api/account*`)
- Dosyalar (`/api/dossiers*`)
- Gümrük asistanı — soru/akış/paylaşım (`/api/customs/assistant*`)
- Toplu hesap ve tasarruf (`/api/tariff/{bulk,scenarios,savings}`)
- Danışman / danışma talepleri (`/api/consultants*`, `/api/consultation-requests*`)
- Admin paneli (`/api/admin/*`)
- Ürün sınıflandırma / görsel tanıma / ön kontrol (`/api/customs/{classify,describe-image,precheck}`)
- İzleme listesi (`/api/watchlist`)
- Faturalama / Stripe (`/api/billing/*`)

**Bu alanları da kapsamak için seçenekler:**
1. **Test-only oturum/dev-login** eklemek (staging'de, sadece test için kısa ömürlü bir giriş yolu) → TestSprite form-login ile `/app` akışları test edilebilir.
2. Backend için **auto-auth (recurring-token)** yapılandırması (Pro) → kimliği doğrulanmış API testleri enjekte edilen token ile çalışır.
3. Bu özellikler için **mevcut Python unittest paketi** (1553 test) zaten yerel/CI'da kapsıyor; TestSprite'ı tamamlayıcı olarak kullanmak.

---

## 5. Yeniden çalıştırma

```bash
# Backend — tüm 39 test (dalga sıralı)
testsprite test run --all --project 5cbccf6d-394e-4961-93ea-a49a554e158b

# Frontend — her test id ile (FE'de --all yok)
testsprite test run <testId> --wait
```

Test kaynakları repoda: `testsprite_be/*.py` (39 backend), `testsprite_fe/*.json` (4 frontend plan).
