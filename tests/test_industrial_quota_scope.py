"""Sanayi tarife kontenjanı: kapsam beyanının dürüstlüğü (FAZ 8.5-A).

Ölçüm (16.09.2026): sanayi tarife kontenjanı kararları hem Resmî Gazete PDF'inde
(ek sayfaları gömülü fontla yazılmış, pdfminer `(cid:60)…` döndürüyor) hem de
Bedesten'de (belge ham PDF olarak dönüyor) **makine tarafından okunamıyor** —
her iki kaynaktan da sıfır GTİP çıkarılabiliyor. Rakamları tahminle okumak yanlış
bir GTİP üretebileceği için indekslemiyoruz.

Buradaki asıl risk, veri eksikliği değil **yanlış negatif**: arayüz "Tarife
kontenjanı — eşleşme yok" derse, sanayi ürünü ithal eden kullanıcı kontenjan
olmadığını sanar. Oysa yalnız **tarım** listesine bakılmıştır. Bu testler, ürünün
elindekinden fazlasını iddia etmemesini kilitler.
"""

from __future__ import annotations

import json
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import trade_measures as tm  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def sources() -> list[dict]:
    config = json.loads((ROOT / "customs_sources.json").read_text(encoding="utf-8"))
    return config["sources"] if isinstance(config, dict) else config


class ScopeLabelTests(unittest.TestCase):
    """Etiketler taranan listeyi söylemeli, "tarife kontenjanı" genelini değil."""

    def test_the_engine_label_names_the_agricultural_list(self) -> None:
        self.assertIn("Tarım", tm.KIND_LABELS["tariff_quota"])

    def test_the_stored_file_is_the_agricultural_one(self) -> None:
        """Etiketin dürüst olduğunun kanıtı: arkasındaki tohum dosyası tarım listesi."""
        source = (ROOT / "trade_measures.py").read_text(encoding="utf-8")
        self.assertIn('"tariff_quota": "agricultural_quotas.json"', source)

    def test_the_ui_never_claims_plain_tariff_quota(self) -> None:
        """Asıl korunan şey: 'Tarife kontenjanı' demek sanayiyi de kapsar gibi okunur."""
        text = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
        for match in re.finditer(r'"(Tar[^"]*tarife kontenjan[^"]*)"', text, flags=re.IGNORECASE):
            self.assertIn("Tarım", match.group(1), match.group(1))
        self.assertNotIn('tariff_quota: "Tarife kontenjanı"', text)

    def test_the_workflow_step_names_the_agricultural_list(self) -> None:
        text = (ROOT / "customs_workflow.py").read_text(encoding="utf-8")
        self.assertIn("Korunma önlemi / tarım ürünleri tarife kontenjanı", text)

    def test_the_workflow_step_tells_the_user_industrial_quotas_are_not_indexed(self) -> None:
        text = (ROOT / "customs_workflow.py").read_text(encoding="utf-8")
        self.assertIn("Sanayi ürünleri tarife kontenjanları indekslenmemiştir", text)


class ManualSourceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source = next(s for s in sources() if s["id"] == "industrial_tariff_quota")

    def test_the_industrial_quota_source_exists(self) -> None:
        self.assertTrue(self.source["url"].startswith("https://ticaret.gov.tr/"))

    def test_it_is_never_fetched_automatically(self) -> None:
        # İndeksleyemediğimiz bir kaynağı çekmek, çekmiş gibi görünmeye yol açardı.
        self.assertEqual(self.source["access_mode"], "manual_only")

    def test_the_note_says_it_was_not_scanned_and_why(self) -> None:
        note = self.source["note"]
        self.assertIn("İNDEKSLENMEMİŞTİR", note)
        self.assertIn("taranmamıştır", note)
        self.assertIn("PDF", note)

    def test_the_note_does_not_promise_a_future_date(self) -> None:
        # "yakında eklenecek" demek, olmayan bir taahhüt üretirdi.
        self.assertNotIn("yakında", self.source["note"].casefold())

    def test_every_manual_only_source_carries_a_note(self) -> None:
        for source in sources():
            if source.get("access_mode") == "manual_only":
                self.assertTrue(source.get("note"), source["id"])


class RegressionTests(unittest.TestCase):
    def test_the_agricultural_quota_pipeline_is_untouched(self) -> None:
        rows = json.loads((ROOT / "data" / "official" / "agricultural_quotas.json").read_text(encoding="utf-8"))
        self.assertTrue(rows)
        self.assertTrue(any(doc.get("items") for doc in rows))

    def test_tariff_quota_stays_a_known_measure_kind(self) -> None:
        self.assertIn("tariff_quota", tm.KINDS)


if __name__ == "__main__":
    unittest.main()
