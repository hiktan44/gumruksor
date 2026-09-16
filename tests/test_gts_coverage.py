"""GTS ülke tablosunun görünürlüğü ve sessiz fazla-vergi riskinin teşhisi.

Resmî İthalat Rejimi Kararı eki, gümrük vergisinden muaf veya indirimli GTS
ülkelerini sayar. Motor bu tabloyu yıllardır okuyor ama **hiçbir uçta
göstermiyordu**, dolayısıyla şu hata sınıfı belirti vermeden yaşıyordu:

    resmî ekteki ad ``countries.py`` kayıt defterinde çözülemiyorsa, kullanıcı o
    ülkeyi yaygın bir başka yazımla girdiğinde eşleşme olmaz, sorgu "Diğer
    Ülkeler" sütununa düşer ve vergi **olduğundan yüksek** çıkar.

Fazla vergi, eksik vergiden daha sessiz bir hatadır: beyan reddedilmez, kimse
şikâyet etmez, yalnız ithalatçı fazla öder. Bu testler teşhisin o satırı
gerçekten yakaladığını ve saymayı şişirmediğini kilitler.
"""

import json
import tempfile
import unittest
from pathlib import Path

from tariff_engine import TariffEngine

SNAPSHOT_SQL = (
    "INSERT INTO tariff_snapshots (id, source_id, source_title, landing_url, archive_url, archive_sha256, "
    "retrieved_at, checked_at, valid_from, measure_count, active, metadata_json, status) "
    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)"
)


def _metadata(countries: dict, sectors: dict | None = None) -> str:
    return json.dumps({"gts_countries": countries, "gts_sectors": sectors or {}}, ensure_ascii=False)


class GtsCoverageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.engine = TariffEngine(data_dir=self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _seed(self, countries: dict, sectors: dict | None = None) -> None:
        with self.engine._connect() as db:
            db.execute(
                SNAPSHOT_SQL,
                ("snap", "3350", "İthalat Rejimi", "https://x", "https://x.zip", "sha",
                 "2026-01-01T00:00:00+03:00", "2026-01-01T00:00:00+03:00", "2026-01-01",
                 1, 1, _metadata(countries, sectors), "approved"),
            )

    def test_an_empty_table_reports_zero_rather_than_failing(self) -> None:
        self._seed({})
        report = self.engine.gts_coverage()
        self.assertEqual(report["total"], 0)
        self.assertEqual(report["unresolved"], [])

    def test_a_country_the_registry_knows_is_reported_resolved(self) -> None:
        self._seed({"banglades": {"name": "Bangladeş", "group": "EAGÜ", "exclusions": ""}})
        report = self.engine.gts_coverage()
        self.assertEqual(report["total"], 1)
        self.assertEqual(report["resolved"], 1)
        self.assertEqual(report["unresolved"], [])
        row = report["countries"][0]
        self.assertTrue(row["resolved"])
        self.assertEqual(row["iso2"], "BD")

    def test_a_country_the_registry_does_not_know_is_named_not_hidden(self) -> None:
        # Asıl mesele bu: sessiz kalırsa kimse fazla vergiyi fark etmez.
        self._seed({"filanistan": {"name": "Filanistan", "group": "GYÜ", "exclusions": ""}})
        report = self.engine.gts_coverage()
        self.assertEqual(report["resolved"], 0)
        self.assertEqual(report["unresolved"], ["Filanistan"])
        self.assertFalse(report["countries"][0]["resolved"])
        self.assertIsNone(report["countries"][0]["registry_key"])

    def test_resolved_and_unresolved_are_counted_separately(self) -> None:
        self._seed({
            "banglades": {"name": "Bangladeş", "group": "EAGÜ", "exclusions": ""},
            "filanistan": {"name": "Filanistan", "group": "GYÜ", "exclusions": ""},
        })
        report = self.engine.gts_coverage()
        self.assertEqual(report["total"], 2)
        self.assertEqual(report["resolved"], 1)
        self.assertEqual(report["unresolved"], ["Filanistan"])

    def test_group_counts_follow_the_official_three_groups(self) -> None:
        self._seed({
            "a": {"name": "Bangladeş", "group": "EAGÜ", "exclusions": ""},
            "b": {"name": "Pakistan", "group": "ÖTDÜ", "exclusions": ""},
            "c": {"name": "Hindistan", "group": "GYÜ", "exclusions": ""},
        })
        report = self.engine.gts_coverage()
        self.assertEqual(report["groups"], {"EAGÜ": 1, "ÖTDÜ": 1, "GYÜ": 1})

    def test_the_official_exclusion_text_is_carried_through_verbatim(self) -> None:
        # İstisna metni yorumlanmaz; sorguda uyarı olarak aynen gösterilecek.
        self._seed({"banglades": {"name": "Bangladeş", "group": "EAGÜ", "exclusions": "S-11a, S-11b"}})
        self.assertEqual(self.engine.gts_coverage()["countries"][0]["exclusions"], "S-11a, S-11b")

    def test_sector_exclusion_rows_are_counted(self) -> None:
        self._seed(
            {"banglades": {"name": "Bangladeş", "group": "EAGÜ", "exclusions": "S-11a"}},
            {"ek-4.xls:610910000000": "S-11a", "ek-4.xls:610990200011": "S-11a"},
        )
        self.assertEqual(self.engine.gts_coverage()["sector_exclusion_rows"], 2)

    def test_rows_are_ordered_by_official_name_so_the_report_is_stable(self) -> None:
        self._seed({
            "z": {"name": "Zambiya", "group": "EAGÜ", "exclusions": ""},
            "a": {"name": "Afganistan", "group": "EAGÜ", "exclusions": ""},
        })
        names = [row["official_name"] for row in self.engine.gts_coverage()["countries"]]
        self.assertEqual(names, sorted(names))

    def test_an_inactive_snapshot_is_not_read(self) -> None:
        # Yürürlükten kalkmış bir ek, bugünkü GTS listesi gibi gösterilemez.
        with self.engine._connect() as db:
            db.execute(
                SNAPSHOT_SQL,
                ("eski", "3350", "Eski", "https://x", "https://x.zip", "sha-eski",
                 "2025-01-01T00:00:00+03:00", "2025-01-01T00:00:00+03:00", "2025-01-01",
                 1, 0, _metadata({"filanistan": {"name": "Filanistan", "group": "GYÜ"}}), "approved"),
            )
        self.assertEqual(self.engine.gts_coverage()["total"], 0)

    def test_the_note_explains_what_an_unresolved_row_costs(self) -> None:
        # Rapor okuyanın "resolved=false" ne demek diye sormasına gerek kalmamalı.
        self._seed({})
        note = self.engine.gts_coverage()["note"]
        self.assertIn("Diğer Ülkeler", note)
        self.assertIn("yüksek", note)


class GtsLookupRegressionTests(unittest.TestCase):
    """Teşhis eklenirken sorgu davranışı değişmemeli."""

    LABELS = {"AB/BK", "EAGÜ", "ÖTDÜ", "GYÜ", "DÜ"}

    def test_a_gts_country_still_selects_its_own_column(self) -> None:
        metadata = {"gts_countries": {"banglades": {"name": "Bangladeş", "group": "EAGÜ", "exclusions": ""}}}
        group, _ = TariffEngine._matching_group("Bangladeş", self.LABELS, metadata, "610910000000")
        self.assertEqual(group, "EAGÜ")

    def test_a_sector_exclusion_still_falls_back_to_the_residual_column(self) -> None:
        metadata = {
            "gts_countries": {"banglades": {"name": "Bangladeş", "group": "EAGÜ", "exclusions": "S-11a"}},
            "gts_sectors": {"ek-4.xls:610910000000": "S-11a"},
        }
        group, warnings = TariffEngine._matching_group("Bangladeş", self.LABELS, metadata, "610910000000")
        self.assertEqual(group, "DÜ")
        self.assertTrue(any("istisnasında" in item for item in warnings))

    def test_a_country_missing_from_the_table_is_not_given_a_preference(self) -> None:
        group, _ = TariffEngine._matching_group("Çin", self.LABELS, {"gts_countries": {}}, "610910000000")
        self.assertEqual(group, "DÜ")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
