"""Gümrükçünün Başucu Programı için birleşik arama ve GTİP fihristi motoru.

Tarife cetveli (154k satır), ÜGD tebliğleri (2026/1 - 2026/32, 2.3k kapsam),
ticaret önlemleri (damping, gözetim, kota), ÖTV ve KDV listelerini tek bir
sorgu katmanında birleştirir.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import unicodedata
from pathlib import Path
from typing import Any

from tax_lists import ExciseTaxIndex, vat_rate_for

logger = logging.getLogger(__name__)

ROOT_DIR = Path(__file__).resolve().parent
DEFAULT_CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "mevzuat-mcp"
DATA_DIR = Path(os.environ.get("MEVZUAT_DATA_DIR", DEFAULT_CACHE_DIR))

# Türk Gümrük Tarife Cetveli (TGTC) Resmî 97 Fasıl İsimleri
CHAPTER_NAMES: dict[str, str] = {
    "01": "Canlı hayvanlar",
    "02": "Etler ve yenilen sakatat",
    "03": "Balıklar, kabuklu hayvanlar, yumuşakçalar ve diğer su omurgasızları",
    "04": "Süt ürünleri, kuş yumurtaları, tabii bal, diğer hayvansal ürünler",
    "05": "Tarifenin başka yerinde belirtilmeyen hayvansal menşeli ürünler",
    "06": "Canlı ağaçlar ve diğer bitkiler, yumrular, kökler, kesme çiçekler",
    "07": "Yenilen sebzeler ve bazı kök ve yumrular",
    "08": "Yenilen meyveler ve yenilen sert kabuklu meyveler, turunçgil kabukları",
    "09": "Kahve, çay, paraguay çayı ve baharat",
    "10": "Hububat (buğday, mısır, çavdar, arpa, pirinç vb.)",
    "11": "Değirmencilik ürünleri, malt, nişasta, inülin, buğday gluteni",
    "12": "Yağlı tohum ve meyveler, muhtelif dane, tohum, saman ve kaba yem",
    "13": "Şellak, sakızlar, reçineler ve diğer bitkisel özsu ve hülasalar",
    "14": "Örülmeye elverişli bitkisel maddeler ve diğer bitkisel ürünler",
    "15": "Hayvansal veya bitkisel katı ve sıvı yağlar (zeytinyağı, ayçiçek vb.)",
    "16": "Et, balık, kabuklu hayvan veya diğer su omurgasızlarının müstahzarları",
    "17": "Şeker ve şeker mamulleri",
    "18": "Kakao ve kakao müstahzarları (çikolata vb.)",
    "19": "Hububat, un, nişasta veya süt müstahzarları, pastacılık ürünleri",
    "20": "Sebzeler, meyveler veya bitkilerin diğer kısımlarının müstahzarları",
    "21": "Çeşitli yenilen gıda müstahzarları",
    "22": "Meşrubat, alkollü içkiler ve sirke",
    "23": "Gıda sanayiinin kalıntı ve döküntüleri, hayvanlar için yemler",
    "24": "Tütün ve tütün yerine geçen işlenmiş maddeler",
    "25": "Tuz, kükürt, topraklar ve taşlar, alçılar, kireçler ve çimento",
    "26": "Metal cevherleri, cüruf ve küller",
    "27": "Mineral yakıtlar, mineral yağlar, petrol, taşkömürü",
    "28": "Anorganik kimyasallar, kıymetli metallerin bileşikleri",
    "29": "Organik kimyasallar",
    "30": "Eczacılık ürünleri (ilaçlar, aşılar, tıbbi malzemeler)",
    "31": "Gübreler",
    "32": "Debagatte ve boyacılıkta kullanılan hülasalar, boyalar, vernikler",
    "33": "Uçucu yağlar ve rezinoitler, parfümeri, kozmetik veya tuvalet müstahzarları",
    "34": "Sabunlar, yüzeyaktif maddeler, yıkama ve yağlama müstahzarları, mumlar",
    "35": "Albüminoid maddeler, değiştirilmiş nişasta esaslı tutkallar, enzimler",
    "36": "Barut ve patlayıcı maddeler, pirotekni mamulleri, kibritler",
    "37": "Fotoğrafçılıkta veya sinemacılıkta kullanılan malzemeler",
    "38": "Muhtelif kimyasal ürünler (biyodizel, dezenfektan vb.)",
    "39": "Plastikler ve mamulleri",
    "40": "Kauçuk ve mamulleri",
    "41": "Ham postlar, deriler (kürkler hariç) ve köseleler",
    "42": "Deri eşya, saraciye eşyası, eyer ve koşum takımları, seyahat eşyası",
    "43": "Kürkler, taklit kürkler ve bunların mamulleri",
    "44": "Ağaç ve ahşap eşya, odun kömürü",
    "45": "Mantar ve mantardan eşya",
    "46": "Hasırdan, sazdan veya diğer örülmeye elverişli maddelerden mamuller",
    "47": "Odun veya diğer lifli selülozik maddelerin hamurları, kağıt/karton hurdası",
    "48": "Kağıt ve karton, kağıt hamurundan, kağıttan veya kartondan eşya",
    "49": "Basılı kitaplar, gazeteler, resimler, baskı sanayii ürünleri",
    "50": "İpek",
    "51": "Yün, ince veya kaba hayvan kılı, at kılı ipliği ve dokunmuş mensucat",
    "52": "Pamuk",
    "53": "Diğer bitkisel dokuma lifleri, kağıt ipliği ve mensucatı",
    "54": "Sentetik ve suni filamentler, şeritler ve benzeri sentetik dokuma maddeleri",
    "55": "Sentetik ve suni devamsız lifler",
    "56": "Vatka, keçe ve dokunmamış mensucat, özel iplikler, sicim, kordon",
    "57": "Halılar ve diğer dokumaya elverişli maddelerden yer kaplamaları",
    "58": "Özel dokunmuş mensucat, tufte edilmiş dokuma mamulleri, dantela, goblen",
    "59": "Emdirilmiş, sıvanmış, kaplanmış veya lamine edilmiş dokumaya elverişli mensucat",
    "60": "Örme mensucat (kumaş)",
    "61": "Örme giyim eşyası ve aksesuarı (tişört, kazak, hırka, çorap vb.)",
    "62": "Örülmemiş giyim eşyası ve aksesuarı (takım elbise, kaban, pantolon vb.)",
    "63": "Dokunabilir maddelerden diğer hazır eşya, takımlar, kullanılmış giyim",
    "64": "Ayakkabılar, getrler, tozluklar ve benzeri eşya ve aksamı",
    "65": "Başlıklar ve aksamı (kasket, şapka, kask)",
    "66": "Şemsiyeler, güneş şemsiyeleri, bastonlar, kırbaçlar",
    "67": "Hazırlanmış ince ve kalın kuş tüyleri, yapma çiçekler",
    "68": "Taş, alçı, çimento, asbest, mika veya benzeri maddelerden eşya",
    "69": "Seramik mamulleri (porselen, fayans, seramik sofra eşyası)",
    "70": "Cam ve cam eşya",
    "71": "Tabii veya kültür inciler, kıymetli veya yarı kıymetli taşlar, takılar",
    "72": "Demir ve çelik",
    "73": "Demir veya çelikten eşya (borular, cıvatalar, profiller)",
    "74": "Bakır ve bakırdan eşya",
    "75": "Nikel ve nikelden eşya",
    "76": "Alüminyum ve alüminyumdan eşya",
    "78": "Kurşun ve kurşundan eşya",
    "79": "Çinko ve çinkodan eşya",
    "80": "Kalay ve kalaydan eşya",
    "81": "Diğer adi metaller, sermetler ve bunlardan eşya",
    "82": "Adi metallerden aletler, bıçakçı eşyası, sofra takımları",
    "83": "Adi metallerden çeşitli eşya (kilitler, menteşeler, kasalar)",
    "84": "Nükleer reaktörler, kazanlar, makineler, mekanik cihazlar (CNC torna vb.)",
    "85": "Elektrikli makine ve cihazlar, ses/görüntü cihazları (telefon, TV, motor)",
    "86": "Demiryolu lokomotifleri, vagonlar, demiryolu aksamı",
    "87": "Motorlu kara taşıtları, traktörler, bisikletler (otomobiller vb.)",
    "88": "Hava taşıtları, uzay taşıtları ve aksamı (İHA / Drone dahil)",
    "89": "Gemiler, botlar ve yüzen yapılar",
    "90": "Optik, fotoğraf, sinema, ölçü, kontrol, ayar, tıbbi veya cerrahi aletler",
    "91": "Saatler ve aksamı (kol saatleri, duvar saatleri)",
    "92": "Müzik aletleri ve aksamı",
    "93": "Silahlar ve mühimmat, bunların aksam ve parçaları",
    "94": "Mobilyalar, yatak takımları, aydınlatma cihazları, prefabrik yapılar",
    "95": "Oyuncaklar, oyun ve spor malzemeleri, aksam ve aksesuarları",
    "96": "Çeşitli mamul eşya (kalemler, çakmaklar, fermuarlar, süpürgeler)",
    "97": "Sanat eserleri, koleksiyon eşyası ve antikalar",
}

# Sık aranan ticari terimlerin yaygın pozisyon açılımları
COMMODITY_KEYWORDS: list[dict[str, Any]] = [
    {"keyword": "akıllı telefon", "gtip": "851713000011", "title": "Akıllı telefonlar (hücresel ağlar için)", "category": "elektronik"},
    {"keyword": "cep telefonu", "gtip": "851713000019", "title": "Diğer hücresel ağ telefonları", "category": "elektronik"},
    {"keyword": "akıllı saat", "gtip": "851762000000", "title": "Akıllı saatler ve veri iletim cihazları", "category": "elektronik"},
    {"keyword": "dizüstü bilgisayar", "gtip": "847130000000", "title": "Taşınabilir otomatik bilgi işlem makineleri (laptop/notebook)", "category": "elektronik"},
    {"keyword": "tablet", "gtip": "847130000000", "title": "Dokunmatik ekranlı tablet bilgisayarlar", "category": "elektronik"},
    {"keyword": "televizyon", "gtip": "852872400000", "title": "Renkli LCD/LED/OLED televizyon alıcı cihazları", "category": "elektronik"},
    {"keyword": "cnc torna", "gtip": "845811200011", "title": "Yatay torna tezgahları (sayısal kontrollü / CNC)", "category": "makine"},
    {"keyword": "torna tezgahı", "gtip": "845819000000", "title": "Metallerin talaş kaldırılarak işlenmesine mahsus diğer torna tezgahları", "category": "makine"},
    {"keyword": "asansör", "gtip": "842810200000", "title": "İnsan ve yük asansörleri (elektrikli)", "category": "makine"},
    {"keyword": "pamuklu tişört", "gtip": "610910000000", "title": "Pamuktan örme tişörtler, atletler ve diğer fanilalar", "category": "tekstil"},
    {"keyword": "sentetik tişört", "gtip": "610990200012", "title": "Sentetik veya suni liflerden örme tişörtler", "category": "tekstil"},
    {"keyword": "kadın pantolon", "gtip": "620462310000", "title": "Kadın veya kız çocuk için pamuktan pantolonlar (dokuma)", "category": "tekstil"},
    {"keyword": "kadın elbise", "gtip": "610442000000", "title": "Kadın veya kız çocuk için pamuktan örme elbiseler", "category": "tekstil"},
    {"keyword": "çikolata", "gtip": "180631000000", "title": "Dolgulu çikolata ve kakao içeren diğer müstahzarlar", "category": "gıda"},
    {"keyword": "zeytinyağı", "gtip": "150920000000", "title": "Natürel sızma zeytinyağı", "category": "gıda"},
    {"keyword": "kahve", "gtip": "090121000000", "title": "Kavrulmuş kahve (kafeini alınmamış)", "category": "gıda"},
    {"keyword": "parfüm", "gtip": "330300100000", "title": "Parfümler ve tuvalet suları", "category": "kozmetik"},
    {"keyword": "cilt bakım kremi", "gtip": "330499001000", "title": "Güzellik veya makyaj müstahzarları ve cilt bakımı kremleri", "category": "kozmetik"},
    {"keyword": "şampuan", "gtip": "330510000000", "title": "Saç şampuanları", "category": "kozmetik"},
    {"keyword": "porselen tabak", "gtip": "691110000011", "title": "Porselenden sofra eşyası (tabaklar, fincanlar vb.)", "category": "seramik"},
    {"keyword": "seramik karo", "gtip": "690721000000", "title": "Sırlı seramik yer ve duvar karoları, fayanslar", "category": "seramik"},
    {"keyword": "elektrikli otomobil", "gtip": "870380100000", "title": "Yalnız elektrik motorlu binek otomobilleri", "category": "otomotiv"},
    {"keyword": "binek otomobil", "gtip": "870322100000", "title": "Kıvılcım ateşlemeli binek otomobiller (1000-1500 cc)", "category": "otomotiv"},
    {"keyword": "oyuncak", "gtip": "950300100000", "title": "Tekerlekli oyuncaklar, oyuncak bebekler ve diğer oyuncaklar", "category": "oyuncak"},
    {"keyword": "cerrahi eldiven", "gtip": "401511000000", "title": "Vulkanize kauçuktan cerrahi eldivenler", "category": "sağlık"},
    {"keyword": "güneş gözlüğü", "gtip": "900410100000", "title": "Optik olarak işlenmiş camlı güneş gözlükleri", "category": "aksesuar"},
    {"keyword": "lityum iyon pil", "gtip": "850760000000", "title": "Lityum-iyon akümülatörler / piller", "category": "elektronik"},
]


def normalise_search_text(text: str) -> str:
    """Türkçe karakterleri ve arama terimlerini küçük harfli, temiz arama dizgisine dönüştürür."""
    if not text:
        return ""
    lowered = text.replace("I", "i").replace("ı", "i").replace("İ", "i").lower()
    decomposed = unicodedata.normalize("NFKD", lowered)
    cleaned = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", cleaned).strip()


def format_gtip(digits: str | None) -> str:
    """12 haneli veya daha kısa GTİP kodunu Türk standart formatında (xxxx.xx.xx.xx.xx) noktalarla biçimlendirir."""
    if not digits:
        return ""
    clean = re.sub(r"\D", "", digits)
    if len(clean) == 12:
        return f"{clean[:4]}.{clean[4:6]}.{clean[6:8]}.{clean[8:10]}.{clean[10:12]}"
    if len(clean) == 8:
        return f"{clean[:4]}.{clean[4:6]}.{clean[6:8]}"
    if len(clean) == 6:
        return f"{clean[:4]}.{clean[4:6]}"
    if len(clean) == 4:
        return clean
    return clean


class UnifiedSearchEngine:
    """Bütünleşik gümrük arama, otomatik tamamlama ve mevzuat fihristi."""

    def __init__(self, data_dir: Path | None = None, vat_index: Any = None) -> None:
        self.data_dir = data_dir or DATA_DIR
        self.tariff_db = self.data_dir / "tariff.sqlite3"
        self.controls_db = self.data_dir / "controls.sqlite3"
        self.trade_db = self.data_dir / "trade_measures.sqlite3"
        self.excise_index = ExciseTaxIndex()
        self.vat_index = vat_index  # vat_lists.VatRateIndex; yoksa sezgisel fasıl kuralı

    def _connect_tariff(self) -> sqlite3.Connection | None:
        if not self.tariff_db.exists():
            return None
        conn = sqlite3.connect(self.tariff_db, timeout=15)
        conn.row_factory = sqlite3.Row
        return conn

    def _connect_controls(self) -> sqlite3.Connection | None:
        if not self.controls_db.exists():
            return None
        conn = sqlite3.connect(self.controls_db, timeout=15)
        conn.row_factory = sqlite3.Row
        return conn

    def autocomplete(self, query: str, limit: int = 12) -> list[dict[str, Any]]:
        """Arama kutusuna yazıldıkça (search-as-you-type) anında GTİP, tanım ve vergi önerileri sunar."""
        text = str(query or "").strip()
        if not text:
            return []
        
        bounded_limit = max(1, min(limit, 30))
        digits = re.sub(r"\D", "", text)
        results: list[dict[str, Any]] = []
        seen_gtips: set[str] = set()

        # 1. Öncelik: Hızlı ticari anahtar kelime eşleşmesi
        norm_query = normalise_search_text(text)
        for entry in COMMODITY_KEYWORDS:
            if norm_query in normalise_search_text(entry["keyword"]) or norm_query in normalise_search_text(entry["title"]):
                gtip = entry["gtip"]
                if gtip not in seen_gtips:
                    seen_gtips.add(gtip)
                    results.append(self._enrich_gtip_card(gtip, entry["title"], source="keyword"))
                    if len(results) >= bounded_limit:
                        return results

        # 2. Öncelik: Tarife veri tabanından rakamsal ön ek veya açıklama araması
        conn = self._connect_tariff()
        if conn:
            try:
                if digits and len(digits) >= 2:
                    rows = conn.execute(
                        """
                        SELECT gtip, list_name, description, rate
                        FROM tariff_measures
                        WHERE gtip LIKE ?
                        GROUP BY gtip
                        ORDER BY gtip ASC
                        LIMIT ?
                        """,
                        (f"{digits}%", bounded_limit),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        """
                        SELECT gtip, list_name, description, rate
                        FROM tariff_measures
                        WHERE description LIKE ? AND description IS NOT NULL AND description != ''
                        GROUP BY gtip
                        ORDER BY gtip ASC
                        LIMIT ?
                        """,
                        (f"%{text}%", bounded_limit),
                    ).fetchall()

                for r in rows:
                    gtip = r["gtip"]
                    if gtip not in seen_gtips:
                        seen_gtips.add(gtip)
                        desc = r["description"] or ""
                        results.append(self._enrich_gtip_card(gtip, desc, list_name=r["list_name"], source="tariff_db"))
                        if len(results) >= bounded_limit:
                            break
            except Exception:
                logger.exception("Tariff autocomplete database lookup failed")
            finally:
                conn.close()

        # 3. Öncelik: Kontrol veri tabanından açıklama eşleşmesi (ürün güvenliği ve denetimi)
        if len(results) < bounded_limit:
            c_conn = self._connect_controls()
            if c_conn:
                try:
                    c_rows = c_conn.execute(
                        """
                        SELECT s.gtip_prefix, s.description, d.title, d.code
                        FROM control_scope s
                        JOIN control_snapshots d ON d.id=s.snapshot_id
                        WHERE d.active=1 AND s.excluded=0 AND s.description LIKE ?
                        LIMIT ?
                        """,
                        (f"%{text}%", bounded_limit - len(results)),
                    ).fetchall()
                    for cr in c_rows:
                        prefix = cr["gtip_prefix"]
                        if prefix not in seen_gtips:
                            seen_gtips.add(prefix)
                            results.append(self._enrich_gtip_card(prefix, cr["description"], source="control_db"))
                            if len(results) >= bounded_limit:
                                break
                except Exception:
                    logger.exception("Controls autocomplete database lookup failed")
                finally:
                    c_conn.close()

        return results

    def _enrich_gtip_card(
        self,
        gtip: str,
        description: str = "",
        list_name: str = "",
        source: str = "",
    ) -> dict[str, Any]:
        """Bir GTİP için fasıl adı, vergi rozetleri, TAREKS ve ÖTV durumunu toplar."""
        clean = re.sub(r"\D", "", gtip)
        chapter_code = clean[:2] if len(clean) >= 2 else ""
        chapter_name = CHAPTER_NAMES.get(chapter_code, "")
        
        # Eğer açıklama boşsa veya sayıysa, fasıl adını veya pozisyon bilgisini koy
        if not description or description.isdigit():
            description = f"{chapter_name} ({clean[:4]} pozisyonu)" if chapter_name else f"GTİP {clean}"

        # ÖTV sorgusu
        excise_res = self.excise_index.lookup(clean)
        has_excise = bool(excise_res.get("in_scope"))
        excise_note = excise_res.get("matches", [{}])[0].get("list_label") if has_excise else None

        # KDV önerisi (resmî liste dizini varsa satır bazlı, yoksa sezgisel); otomatik uygulanmaz
        vat = vat_rate_for(clean, self.vat_index)
        vat_label = "belirsiz (şarta bağlı)" if vat.get("ambiguous") else (vat.get("list") or "Genel Oran")

        # TAREKS / Denetim kontrolü
        has_tareks = False
        control_titles: list[str] = []
        c_conn = self._connect_controls()
        if c_conn:
            try:
                c_rows = c_conn.execute(
                    """
                    SELECT DISTINCT d.code, d.system, d.title
                    FROM control_scope s
                    JOIN control_snapshots d ON d.id=s.snapshot_id
                    WHERE d.active=1 AND s.excluded=0 AND (
                        substr(?, 1, length(s.gtip_prefix)) = s.gtip_prefix
                        OR substr(s.gtip_prefix, 1, length(?)) = ?
                    )
                    LIMIT 3
                    """,
                    (clean, clean, clean),
                ).fetchall()
                if c_rows:
                    has_tareks = True
                    control_titles = [f"{r['code']} ({r['system']})" for r in c_rows]
            except Exception:
                pass
            finally:
                c_conn.close()

        return {
            "gtip": clean,
            "code": clean,
            "name": description,
            "formatted_gtip": format_gtip(clean),
            "description": description,
            "chapter_code": chapter_code,
            "chapter_name": chapter_name,
            "list_name": list_name,
            "has_excise": has_excise,
            "excise_label": excise_note,
            "vat_rate": vat.get("rate"),
            "vat_label": vat_label,
            "vat_basis": vat.get("basis"),
            "vat_ambiguous": bool(vat.get("ambiguous")),
            "has_controls": has_tareks,
            "control_badges": control_titles,
            "source": source,
        }

    def search_all(self, query: str, category: str = "all", limit: int = 25) -> dict[str, Any]:
        """Tüm resmi kaynaklarda (Tarife, Kontroller, Önlemler, ÖTV, Mevzuat) kategorize arama."""
        text = str(query or "").strip()
        if not text:
            return {"query": query, "total_count": 0, "categories": {}}

        results: dict[str, list[dict[str, Any]]] = {
            "tarife": [],
            "denetim": [],
            "otv": [],
            "onlemler": [],
        }

        # 1. Tarife & Eşya Arama
        if category in {"all", "tarife"}:
            results["tarife"] = self.autocomplete(text, limit=min(limit, 10))

        # 2. Denetim & TAREKS/TSE Arama
        if category in {"all", "denetim"}:
            c_conn = self._connect_controls()
            if c_conn:
                try:
                    wildcard = f"%{text}%"
                    digits = re.sub(r"\D", "", text)
                    c_rows = c_conn.execute(
                        """
                        SELECT s.gtip_prefix, s.description, d.code, d.title, d.authority, d.system, d.source_url
                        FROM control_scope s
                        JOIN control_snapshots d ON d.id=s.snapshot_id
                        WHERE d.active=1 AND s.excluded=0 AND (
                            s.description LIKE ?
                            OR d.title LIKE ?
                            OR d.code LIKE ?
                            OR (? != '' AND s.gtip_prefix LIKE ?)
                        )
                        ORDER BY d.code ASC
                        LIMIT ?
                        """,
                        (wildcard, wildcard, wildcard, digits, f"{digits}%", limit),
                    ).fetchall()
                    for cr in c_rows:
                        results["denetim"].append({
                            "gtip": cr["gtip_prefix"],
                            "formatted_gtip": format_gtip(cr["gtip_prefix"]),
                            "description": cr["description"],
                            "communique_code": cr["code"],
                            "communique_title": cr["title"],
                            "authority": cr["authority"],
                            "system": cr["system"],
                            "source_url": cr["source_url"],
                        })
                except Exception:
                    logger.exception("Unified search in controls failed")
                finally:
                    c_conn.close()

        # 3. ÖTV Arama
        if category in {"all", "otv"}:
            digits = re.sub(r"\D", "", text)
            if digits:
                otv_res = self.excise_index.lookup(digits)
                if otv_res.get("in_scope"):
                    for m in otv_res.get("matches", []):
                        results["otv"].append({
                            "gtip": digits,
                            "matched_code": m.get("matched_code"),
                            "list_label": m.get("list_label"),
                            "description": m.get("description"),
                            "values": m.get("values"),
                            "correlation_note": m.get("correlation_note"),
                            "note": m.get("note"),
                        })

        total = sum(len(items) for items in results.values())
        return {
            "query": text,
            "total_count": total,
            "categories": results,
            "chapters": [
                {"code": ch, "name": name}
                for ch, name in CHAPTER_NAMES.items()
                if normalise_search_text(text) in normalise_search_text(name) or text == ch
            ][:5],
        }
