"""İhracat kontrol listeleri (FAZ 8.2) — yön boyutu ve kısmi indeks dürüstlüğü.

Üç kritik değişmez:

1. **Gerileme kilidi:** ithalat sorgusu ve `status().ready` göç öncesiyle birebir aynı.
2. **Yönler birbirini kilitlemez:** bir ihracat belgesi çekilemediğinde ithalat yolu
   çalışmaya devam eder; ithalat ÜGD tebliği ihracat dosyasına asla karışmaz.
3. **İhracat indeksi kısmidir ve her sonuç bunu söyler:** eşleşme çıkmaması
   "yükümlülük yok" anlamına gelmez.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from control_engine import ImportControlEngine, annex_plan, rule_direction  # noqa: E402

SNAPSHOT_COLUMNS = (
    "id, code, title, category, mevzuat_id, source_url, official_gazette_date, official_gazette_number, "
    "document_sha256, retrieved_at, valid_from, scope_count, authority, system, risk_based, "
    "physical_inspection_possible, laboratory_test_possible, required_documents_excerpt, active, direction"
)


def _snapshot(snapshot_id: str, code: str, title: str, *, direction: str = "import") -> tuple:
    return (
        snapshot_id, code, title, "test", "m-" + snapshot_id, "https://mevzuat.adalet.gov.tr/",
        "2025-12-31", "33124", "sha-" + snapshot_id, "2026-01-01T00:00:00+03:00", "2026-01-01",
        1, "Ticaret Bakanlığı", "TAREKS", 0, 1, 0, None, 1, direction,
    )


class DirectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.engine = ImportControlEngine(data_dir=self.tmp.name)
        # Tek zorunlu ithalat kaydı + bir ihracat kaydı (ihracat kayıtları optional).
        self.engine.rules_config = [
            {"code": "2026/9", "title": "Oyuncak İthalat Denetimi Tebliği", "category": "oyuncak"},
            {
                "code": "IHR/TEST",
                "direction": "export",
                "optional": True,
                "title": "Test İhracat Tebliği",
                "category": "ihracat",
                "discovery": {"terms": ["Test"]},
            },
        ]
        with self.engine._connect() as db:
            db.execute(
                f"INSERT INTO control_snapshots ({SNAPSHOT_COLUMNS}) VALUES ({','.join('?' * 20)})",
                _snapshot("imp", "2026/9", "Oyuncak İthalat Denetimi Tebliği"),
            )
            db.execute(
                f"INSERT INTO control_snapshots ({SNAPSHOT_COLUMNS}) VALUES ({','.join('?' * 20)})",
                _snapshot("exp", "IHR/TEST", "Test İhracat Tebliği", direction="export"),
            )
            db.executemany(
                "INSERT INTO control_scope (snapshot_id, gtip_prefix, description, source_line, "
                "source_offset, excluded, list_kind) VALUES (?,?,?,?,?,?,?)",
                [
                    ("imp", "950300", "Tekerlekli oyuncaklar", "95.03 oyuncak", 1, 0, "scope"),
                    ("exp", "950300", "İhracatta izne tabi oyuncak", "95.03 ihracat", 1, 0, "licence_required"),
                ],
            )

    def tearDown(self) -> None:
        asyncio.run(self.engine.close())
        self.tmp.cleanup()

    def _lookup(self, **kwargs):
        return asyncio.run(self.engine.lookup("950300000000", **kwargs))

    def test_import_lookup_is_unchanged(self) -> None:
        result = self._lookup()
        self.assertEqual(result.direction, "import")
        self.assertEqual([m.rule.code for m in result.matches], ["2026/9"])

    def test_export_lookup_only_sees_export_lists(self) -> None:
        result = self._lookup(direction="export")
        self.assertEqual(result.direction, "export")
        self.assertEqual([m.rule.code for m in result.matches], ["IHR/TEST"])
        self.assertEqual(result.matches[0].matched_scope.list_kind, "licence_required")

    def test_an_import_communique_never_leaks_into_an_export_file(self) -> None:
        codes = {m.rule.code for m in self._lookup(direction="export").matches}
        self.assertNotIn("2026/9", codes)

    def test_every_export_result_admits_the_index_is_partial(self) -> None:
        for result in (self._lookup(direction="export"), asyncio.run(self.engine.lookup("999999999999", direction="export"))):
            self.assertTrue(
                any("indeksimiz kısmidir" in warning for warning in result.warnings),
                result.warnings,
            )

    def test_an_import_result_does_not_carry_the_export_caveat(self) -> None:
        self.assertFalse(any("indeksimiz kısmidir" in w for w in self._lookup().warnings))

    def test_an_unknown_direction_falls_back_to_import(self) -> None:
        self.assertEqual(self._lookup(direction="belirsiz").direction, "import")


class ReadinessTests(unittest.TestCase):
    """Bir yönün eksikliği diğerini kilitlememeli."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.engine = ImportControlEngine(data_dir=self.tmp.name)
        self.engine.rules_config = [
            {"code": "2026/9", "title": "Oyuncak İthalat Denetimi Tebliği", "category": "oyuncak"},
            {"code": "IHR/TEST", "direction": "export", "optional": True, "title": "Test", "category": "ihracat",
             "discovery": {"terms": ["Test"]}},
        ]

    def tearDown(self) -> None:
        asyncio.run(self.engine.close())
        self.tmp.cleanup()

    def _insert(self, snapshot_id: str, code: str, direction: str) -> None:
        with self.engine._connect() as db:
            db.execute(
                f"INSERT INTO control_snapshots ({SNAPSHOT_COLUMNS}) VALUES ({','.join('?' * 20)})",
                _snapshot(snapshot_id, code, "Başlık", direction=direction),
            )

    def test_import_is_ready_even_when_no_export_document_was_fetched(self) -> None:
        # Asıl korunan şey bu: tek bir ihracat belgesi eksik diye ithalat yolu düşmemeli.
        self._insert("imp", "2026/9", "import")
        status = self.engine.status()
        self.assertTrue(status.ready)
        self.assertFalse(status.ready_export)

    def test_export_becomes_ready_with_a_partial_index(self) -> None:
        # İhracat ölçütü kısmi kapsamdır: elimizde olan listelerden cevap verilir.
        self._insert("exp", "IHR/TEST", "export")
        self.assertTrue(self.engine.status().ready_export)

    def test_export_alone_does_not_make_the_import_side_ready(self) -> None:
        self._insert("exp", "IHR/TEST", "export")
        self.assertFalse(self.engine.status().ready)

    def test_export_lookup_is_unavailable_before_any_export_document(self) -> None:
        self._insert("imp", "2026/9", "import")
        result = asyncio.run(self.engine.lookup("950300000000", direction="export"))
        self.assertEqual(result.status, "unavailable")
        self.assertIn("indekslenmedi", result.warnings[0])


class ConfigTests(unittest.TestCase):
    def test_existing_records_default_to_import(self) -> None:
        self.assertEqual(rule_direction({"code": "2026/9"}), "import")

    def test_direction_is_read_case_insensitively(self) -> None:
        self.assertEqual(rule_direction({"direction": "EXPORT"}), "export")
        self.assertEqual(rule_direction({"direction": "ihracat"}), "import")

    def test_licence_required_is_an_accepted_annex_kind(self) -> None:
        plan = annex_plan({"scope_annexes": [{"annex": 1, "kind": "licence_required"}]})
        self.assertEqual(plan, [{"annex": 1, "kind": "licence_required"}])

    def test_an_unknown_annex_kind_falls_back_to_the_weakest_claim(self) -> None:
        # "kapsamda" demek "yasak" demekten daha zayıf bir iddiadır.
        plan = annex_plan({"scope_annexes": [{"annex": 1, "kind": "uydurma"}]})
        self.assertEqual(plan[0]["kind"], "scope")

    def test_shipped_config_keeps_every_import_record_and_adds_export_ones(self) -> None:
        config = json.loads(Path("control_sources.json").read_text(encoding="utf-8"))
        rules = config["rules"]
        imports = [r for r in rules if rule_direction(r) == "import"]
        exports = [r for r in rules if rule_direction(r) == "export"]
        self.assertEqual(len(imports), 25, "mevcut ithalat kayıtları korunmalı")
        self.assertTrue(exports)
        for rule in exports:
            # İhracat kaydı zorunlu olsaydı, çekilemediğinde ithalat 'ready' bayrağını düşürürdü.
            self.assertTrue(rule.get("optional"), rule["code"])
            self.assertTrue((rule.get("discovery") or {}).get("terms"), rule["code"])


class MigrationTests(unittest.TestCase):
    def test_a_database_without_the_direction_column_migrates_and_reads_as_import(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "controls.sqlite3"
            with sqlite3.connect(path) as db:
                db.executescript(
                    """
                    CREATE TABLE control_snapshots (
                        id TEXT PRIMARY KEY, code TEXT NOT NULL, title TEXT NOT NULL, category TEXT NOT NULL,
                        mevzuat_id TEXT NOT NULL, source_url TEXT NOT NULL, official_gazette_date TEXT,
                        official_gazette_number TEXT, document_sha256 TEXT NOT NULL, retrieved_at TEXT NOT NULL,
                        valid_from TEXT NOT NULL, scope_count INTEGER NOT NULL, authority TEXT NOT NULL,
                        system TEXT NOT NULL, risk_based INTEGER NOT NULL,
                        physical_inspection_possible INTEGER NOT NULL, laboratory_test_possible INTEGER NOT NULL,
                        required_documents_excerpt TEXT, active INTEGER NOT NULL DEFAULT 0);
                    CREATE TABLE control_scope (
                        snapshot_id TEXT NOT NULL, gtip_prefix TEXT NOT NULL, description TEXT,
                        source_line TEXT NOT NULL, source_offset INTEGER NOT NULL,
                        PRIMARY KEY (snapshot_id, gtip_prefix));
                    INSERT INTO control_snapshots VALUES ('old','2026/9','Eski','oyuncak','m','https://x',
                        '2025-12-31','33124','sha','2026-01-01T00:00:00+03:00','2026-01-01',1,'Ticaret','TAREKS',0,1,0,NULL,1);
                    INSERT INTO control_scope VALUES ('old','950300','Oyuncak','95.03',1);
                    """
                )
            engine = ImportControlEngine(data_dir=directory)
            engine.rules_config = [{"code": "2026/9", "title": "Eski", "category": "oyuncak"}]
            try:
                with engine._connect() as db:
                    row = db.execute("SELECT direction FROM control_snapshots WHERE id='old'").fetchone()
                self.assertEqual(row["direction"], "import")
                result = asyncio.run(engine.lookup("950300000000"))
                self.assertEqual([m.rule.code for m in result.matches], ["2026/9"])
            finally:
                asyncio.run(engine.close())


if __name__ == "__main__":
    unittest.main()
