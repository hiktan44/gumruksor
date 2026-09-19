"""GTİP bazlı ÖTV kapsam sorgusu (4760 sayılı Kanun ekli (I)-(IV) sayılı listeler).

Veri kaynağı ``data/official/excise_tax_lists.json``; 4760 sayılı Özel Tüketim Vergisi
Kanununun Resmî Gazete'de yayımlanan güncel metninin ekli listelerinden çıkarılmıştır.

Listelerde iki oran sütunu vardır: kanun metnindeki oran/tutar ve Cumhurbaşkanı
kararlarıyla yeniden tespit edilen "uygulanacak" oran/tutar. İkinci sütun boşsa
kanuni değer uygulanır.

(III) sayılı liste (alkollü içecekler, tütün mamulleri, kolalı gazozlar) 19.09.2026'da
``excise_lists.py`` ile resmî PDF'in **tablo kılavuz çizgilerinden** yeniden okundu ve
``rates_verified`` oldu; önceki metin-akışı çıkarımında satırlar kaymış, dipnot
üstsimgeleri oranlara yapışmıştı (``4559`` → ``45`` + ``59`` dipnotu) ve bu yüzden o
iki cetvelde hiç oran gösterilemiyordu. ``rates_verified`` olmayan bir bölüm hâlâ
yalnız kapsam bilgisi verir, oran göstermez — bu davranış bilinçlidir.

KDV tarafı: ``estimate_vat_rate`` fasıl bazlı sezgisel bir tahmindir. 2007/13033 sayılı
Kararın ekli (I)/(II) sayılı listelerinden satır bazlı öneri ``vat_lists.VatRateIndex``
ile üretilir; ``vat_rate_for`` verilen dizini kullanır, dizin yoksa sezgisele düşer.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any

from trade_measures import code_matches, normalise_code

logger = logging.getLogger(__name__)

DATA_FILE = "excise_tax_lists.json"
LEGAL_BASIS = "4760 sayılı Özel Tüketim Vergisi Kanunu ekli (I)-(IV) sayılı listeler"

# Listeye göre matrah/uygulama notu – beyanname hazırlarken müşavirin bilmesi gerekenler.
LIST_NOTES = {
    "I": "Petrol ürünleri; vergi maktu tutar olarak (birim başına TL) alınır, ithalatta gümrük idaresince tahsil edilir.",
    "II": "Motorlu taşıtlar; vergi ilk iktisapta oransal olarak alınır, matrah ÖTV hariç satış bedelidir.",
    "III": "Alkollü içecekler, tütün mamulleri ve kolalı gazozlar; oransal vergi ile asgari maktu vergi birlikte uygulanır.",
    "IV": "Lüks tüketim ve dayanıklı tüketim malları; vergi ithalatta oransal olarak alınır.",
}

# 2022 WCO Armonize Sistem revizyonu ile değişen ve 4760 sayılı ÖTV Kanunu ekli listelerinde
# eski kodlarla yer alan malların güncel Türk Gümrük Tarife Cetveli (TGTC) korelasyon haritası
HS_EXCISE_CORRELATIONS: dict[str, tuple[str, str]] = {
    "851713": ("851712", "Akıllı telefonlar (2022 öncesi 8517.12 karşılığı; IV Sayılı Liste)"),
    "851714": ("851712", "Diğer hücresel ağ telefonları (2022 öncesi 8517.12 karşılığı; IV Sayılı Liste)"),
    "851771": ("851770", "Antenler ve yansıtıcılar (eski 8517.70 karşılığı)"),
    "851779": ("851770", "Telefon aksam ve parçaları (eski 8517.70 karşılığı)"),
    "8524": ("8528", "Düz panel ekran modülleri"),
    "8806": ("8802", "İnsansız hava araçları (İHA / Drone)"),
}

def estimate_vat_rate(gtip: str) -> dict[str, Any]:
    """3065 sayılı KDV Kanununa göre GTİP bazlı tahmini KDV oranını ve dayanağını döndürür."""
    code = normalise_code(gtip)
    if not code:
        return {"rate": 20.0, "list": "Genel Oran", "legal_basis": "3065 sayılı KDV Kanunu md. 28"}
    
    chapter = code[:2]
    heading = code[:4]
    
    # %1 Oranlı (I Sayılı Liste): Tohumluk, hububat tohumları vb.
    if code.startswith(("070110", "100111", "100191", "100510", "120110", "120510", "12060010")):
        return {
            "rate": 1.0,
            "list": "I Sayılı Liste (İndirimli)",
            "legal_basis": "2007/13033 sayılı BKK eki (I) sayılı liste (Tohumluk ve tarımsal hammaddeler)",
        }
    
    # %10 Oranlı (II Sayılı Liste): Temel gıda maddeleri, tekstil, giyim, ayakkabı, ilaç, tıbbi cihaz
    food_chapters = {f"{i:02d}" for i in range(1, 25)}
    textile_chapters = {f"{i:02d}" for i in range(50, 64)}
    
    if chapter in food_chapters:
        # Özel tüketim / alkol / tütün genel orana tabidir
        if chapter in {"22", "24"} and not code.startswith(("2201", "2202")):
            return {"rate": 20.0, "list": "Genel Oran", "legal_basis": "3065 sayılı KDV Kanunu (Alkollü içecekler/tütün)"}
        return {
            "rate": 10.0,
            "list": "II Sayılı Liste (İndirimli)",
            "legal_basis": "2007/13033 sayılı BKK eki (II) sayılı liste (Temel gıda maddeleri)",
        }
    
    if chapter in textile_chapters or chapter == "64":  # Tekstil, konfeksiyon, ayakkabı
        return {
            "rate": 10.0,
            "list": "II Sayılı Liste (İndirimli)",
            "legal_basis": "2007/13033 sayılı BKK eki (II) sayılı liste (Tekstil, giyim eşyası ve ayakkabı)",
        }
    
    if chapter == "30" or heading in {"9018", "9019", "9021"}:  # İlaç ve tıbbi cihaz
        return {
            "rate": 10.0,
            "list": "II Sayılı Liste (İndirimli)",
            "legal_basis": "2007/13033 sayılı BKK eki (II) sayılı liste (Tıbbi ürünler ve cihazlar)",
        }
    
    return {
        "rate": 20.0,
        "list": "Genel Oran",
        "legal_basis": "3065 sayılı KDV Kanunu md. 28 (Genel Mal ve Hizmetler)",
    }

def heuristic_vat_lookup(gtip: str) -> dict[str, Any]:
    """``VatRateIndex.lookup`` ile aynı şekilde sezgisel KDV önerisi (resmî liste eşleşmesi yok)."""
    code = normalise_code(gtip)
    estimate = estimate_vat_rate(code)
    return {
        "gtip": code,
        "rate": estimate["rate"],
        "basis": "heuristic",
        "list": estimate["list"],
        "legal_basis": estimate["legal_basis"],
        "matched_expression": None,
        "row_text": None,
        "conditions": [],
        "ambiguous": False,
        "candidates": [],
        "verified": None,
        "source": "fasıl bazlı sezgisel kural (tax_lists.estimate_vat_rate)",
        "source_url": None,
        "retrieved_at": None,
        "note": "Sezgisel tahmindir; resmî listede satır eşleşmedi. Beyanname öncesi doğrulayın.",
    }


def vat_rate_for(gtip: str, index: Any = None) -> dict[str, Any]:
    """Resmî liste dizini (``vat_lists.VatRateIndex``) varsa onu, yoksa sezgiseli kullanır."""
    if index is not None:
        try:
            return index.lookup(gtip)
        except Exception:  # noqa: BLE001 – KDV önerisi arama sonucunu düşürmemeli
            logger.exception("VAT list lookup failed for %s", gtip)
    return heuristic_vat_lookup(gtip)


_VALUE_LABELS = {
    "tax_rate": "Kanuni vergi oranı (%)",
    "applied_tax_rate": "Uygulanacak vergi oranı (%)",
    "tax_amount": "Kanuni vergi tutarı (TL)",
    "applied_tax_amount": "Uygulanacak vergi tutarı (TL)",
    "minimum_specific_tax": "Asgari maktu vergi tutarı (TL)",
    "applied_minimum_specific_tax": "Uygulanacak asgari maktu vergi tutarı (TL)",
    "specific_tax": "Maktu vergi tutarı (TL)",
    "applied_specific_tax": "Uygulanacak maktu vergi tutarı (TL)",
    "unit": "Birim",
}


def _default_data_dir() -> Path:
    override = os.environ.get("OFFICIAL_DATA_DIR")
    if override:
        return Path(override)
    candidates = [
        Path(__file__).resolve().parent / "data" / "official",
        Path(sys.prefix) / "data" / "official",
        Path.cwd() / "data" / "official",
    ]
    for candidate in candidates:
        if (candidate / DATA_FILE).exists():
            return candidate
    return candidates[0]


def list_label(list_name: str, cetvel: str | None) -> str:
    return f"({list_name}) sayılı liste" + (f" ({cetvel}) cetveli" if cetvel else "")


# Resmî listede eşya tanımı kod satırının hem üstüne hem altına sarar; ayrıştırma sırasında
# bir önceki satırın kuyruğu başa eklenebiliyor. Cümle başı büyük harften önceki parçayı atarız.
_SENTENCE_START_RE = re.compile(r"(?:^|\s)(\(?[A-ZÇĞİÖŞÜ0-9])")


def clean_description(text: str) -> str:
    """Açıklamanın başındaki, önceki satırdan sarkan küçük harfli parçayı ayıklar."""
    value = (text or "").strip()
    if not value or value[:1].isupper() or value[:1].isdigit():
        return value
    match = _SENTENCE_START_RE.search(value)
    return value[match.start(1):].strip() if match else value


class ExciseTaxIndex:
    """GTİP -> ÖTV listesi eşlemesi; salt okunur, tohum dosyasından yüklenir."""

    def __init__(self, data_dir: str | Path | None = None) -> None:
        self._path = Path(data_dir or _default_data_dir()) / DATA_FILE
        self._payload: dict[str, Any] = {}
        self._entries: list[dict[str, Any]] = []
        self._load()

    def _load(self) -> None:
        try:
            self._payload = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            logger.warning("ÖTV liste dosyası bulunamadı: %s", self._path)
            return
        except (OSError, ValueError):
            logger.exception("ÖTV liste dosyası okunamadı: %s", self._path)
            return
        for section in self._payload.get("sections", []):
            columns = list(section.get("value_columns") or [])
            for row in section.get("rows", []):
                code = normalise_code(row.get("code", ""))
                if not code:
                    continue
                self._entries.append({
                    "code": code,
                    "raw_code": row.get("code", ""),
                    "description": clean_description(row.get("description")),
                    "list": section.get("list"),
                    "cetvel": section.get("cetvel"),
                    "rates_verified": bool(section.get("rates_verified")),
                    "values": {key: row[key] for key in columns if row.get(key)},
                })

    @property
    def ready(self) -> bool:
        return bool(self._entries)

    def status(self) -> dict[str, Any]:
        return {
            "ready": self.ready,
            "entry_count": len(self._entries),
            "legal_basis": LEGAL_BASIS,
            "source_url": self._payload.get("source_url"),
            "sections": [
                {
                    "list": section.get("list"),
                    "cetvel": section.get("cetvel"),
                    "row_count": section.get("row_count"),
                    "rates_verified": section.get("rates_verified"),
                }
                for section in self._payload.get("sections", [])
            ],
        }

    def lookup(self, gtip: str) -> dict[str, Any]:
        """Sorgulanan GTİP'in ÖTV kapsamını döndürür.

        En uzun (en özel) kod eşleşmesi öncelikli sıralanır; kısa pozisyon satırları
        (ör. 87.03) daha uzun sorgular için de kapsam bildirir.
        """
        code = normalise_code(gtip)
        if not code:
            return {"gtip": gtip, "in_scope": False, "matches": [], "warnings": [], "legal_basis": LEGAL_BASIS}
        matches = [entry for entry in self._entries if code_matches(entry["code"], code)]
        matches.sort(key=lambda entry: len(entry["code"]), reverse=True)

        correlated_info: tuple[str, str] | None = None
        if not matches:
            for prefix, (target, desc) in HS_EXCISE_CORRELATIONS.items():
                if code.startswith(prefix):
                    target_matches = [entry for entry in self._entries if code_matches(entry["code"], target)]
                    if target_matches:
                        matches = target_matches
                        matches.sort(key=lambda entry: len(entry["code"]), reverse=True)
                        correlated_info = (target, desc)
                        break

        results: list[dict[str, Any]] = []
        warnings: list[str] = []
        for entry in matches:
            label = list_label(entry["list"], entry["cetvel"])
            values = {
                _VALUE_LABELS.get(key, key): value
                for key, value in entry["values"].items()
                if entry["rates_verified"] or key == "unit"
            }
            res_item: dict[str, Any] = {
                "matched_code": entry["raw_code"],
                "list": entry["list"],
                "cetvel": entry["cetvel"],
                "list_label": label,
                "description": entry["description"],
                "rates_verified": entry["rates_verified"],
                "values": values,
                "note": LIST_NOTES.get(entry["list"], ""),
            }
            if correlated_info:
                res_item["correlation_note"] = f"Armonize Sistem korelasyonu: {correlated_info[1]}"
            results.append(res_item)

        neighbours: list[dict[str, str]] = []
        if not results and len(code) >= 4:
            # Kanun metnindeki kodlar Armonize Sistem revizyonlarıyla yeniden numaralandırılmış
            # olabilir (ör. cep telefonu 8517.12 -> 8517.13). Aynı pozisyondaki satırları uyarı
            # olarak bildiririz; sessizce "kapsam dışı" demek beyanname hatasına yol açar.
            for entry in self._entries:
                if entry["code"][:4] == code[:4]:
                    neighbours.append({
                        "matched_code": entry["raw_code"],
                        "list_label": list_label(entry["list"], entry["cetvel"]),
                        "description": entry["description"][:120],
                    })
            if neighbours:
                warnings.append(
                    f"Bu GTİP listelerde yok, ancak aynı pozisyonda ({code[:4]}) "
                    f"{len(neighbours)} ÖTV satırı var. Kanun metnindeki kodlar Armonize Sistem "
                    "revizyonlarıyla yeniden numaralandırılmış olabilir; eşyanızın karşılığını doğrulayın."
                )

        if results:
            first = results[0]
            if correlated_info:
                warnings.append(
                    f"Eşya korelasyon uyarınca ÖTV kapsamındadır: {first['list_label']}, eski kod {first['matched_code']}. "
                    f"GİB/TGTC 2022 korelasyonu ({correlated_info[1]}). {first['note']}"
                )
            else:
                warnings.append(
                    f"Eşya ÖTV kapsamındadır: {first['list_label']}, {first['matched_code']}. {first['note']}"
                )
            if not first["rates_verified"]:
                warnings.append(
                    "Bu listede oran sütunları resmî metinde birleşik basıldığı için otomatik okunmadı; "
                    "oran ve asgari maktu vergi tutarını Kanun ekinden doğrulayın."
                )
            else:
                warnings.append(
                    "Kanundaki oran/tutarlar Cumhurbaşkanı kararlarıyla değiştirilebilir; "
                    "beyanname öncesi yürürlükteki değeri doğrulayın."
                )
        vat_info = estimate_vat_rate(code)
        return {
            "gtip": code,
            "in_scope": bool(results),
            "matches": results[:10],
            "match_count": len(results),
            "related_positions": neighbours[:10],
            "warnings": warnings,
            "vat_estimate": vat_info,
            "legal_basis": LEGAL_BASIS,
            "source_url": self._payload.get("source_url"),
        }


def summary_lines(report: dict[str, Any]) -> list[str]:
    """MCP araç çıktısı için insan okunur özet."""
    if not report.get("in_scope"):
        lines = ["ÖTV: Bu GTİP 4760 sayılı Kanunun ekli listelerinde bulunamadı (ÖTV'ye tabi görünmüyor)."]
        for related in report.get("related_positions", [])[:5]:
            lines.append(f"  ? Aynı pozisyonda: {related['matched_code']} · {related['list_label']}")
        lines.extend(f"  ! {warning}" for warning in report.get("warnings", []))
        return lines
    lines = [f"ÖTV kapsamı [{report['legal_basis']}]:"]
    for match in report["matches"][:5]:
        parts = [f"  - {match['matched_code']} · {match['list_label']}"]
        if match["description"]:
            parts.append(f" · {match['description'][:80]}")
        if match["values"]:
            rendered = ", ".join(f"{key}: {value}" for key, value in match["values"].items())
            parts.append(f" · {rendered}")
        lines.append("".join(parts))
    lines.extend(f"  ! {warning}" for warning in report.get("warnings", []))
    return lines
