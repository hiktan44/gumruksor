"""Disk görünürlüğü ve yedek: sessiz veri kaybının iki yolunu kapatır.

Bu paketin kilitlediği iki davranış:

1. **Yedek, koruduğu hatayı üretmemeli.** Diskte yer yoksa kopya alınmaz ve
   sebebi rapora yazılır; zorlasaydık diski biz doldururduk.
2. **İndirme ucu yedek dizininin dışına çıkamamalı.** Yedek dosyaları kullanıcı
   hesaplarını ve kanıt dosyalarını içerir; ad doğrulaması sızıntıyı önleyen tek
   engeldir.
"""

import asyncio
import sqlite3
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import storage


def _make_db(path: Path, rows: int = 5) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE IF NOT EXISTS t (id INTEGER PRIMARY KEY, v TEXT)")
        connection.executemany("INSERT INTO t(v) VALUES(?)", [(f"satır {i}",) for i in range(rows)])


class _FakeUsage:
    def __init__(self, total: int, free: int) -> None:
        self.total = total
        self.free = free
        self.used = total - free


def _usage(total: int = 1_000_000_000, free: int = 800_000_000):
    return lambda _path: _FakeUsage(total, free)


class InventoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def _service(self, **kwargs) -> storage.StorageService:
        kwargs.setdefault("disk_usage", _usage())
        kwargs.setdefault("datasets", storage.DEFAULT_BACKUP_DATASETS)
        return storage.StorageService(self.dir, **kwargs)

    def test_every_known_database_is_listed_even_before_it_exists(self) -> None:
        rows = self._service().databases()
        self.assertEqual(len(rows), len(storage.DATABASES))
        self.assertTrue(all(row["exists"] is False for row in rows))
        self.assertTrue(all(row["total_bytes"] == 0 for row in rows))

    def test_an_existing_database_reports_its_size(self) -> None:
        _make_db(self.dir / "users.sqlite3")
        row = next(item for item in self._service().databases() if item["name"] == "users")
        self.assertTrue(row["exists"])
        self.assertGreater(row["bytes"], 0)
        self.assertIsNotNone(row["modified_at"])

    def test_the_irreplaceable_databases_are_the_ones_backed_up_by_default(self) -> None:
        # Asıl karar bu: ücretli AB TARIC arşivi ve kullanıcı verisi yedeklenir,
        # resmî kaynaktan ücretsiz yeniden kurulabilenler yedeklenmez.
        self.assertEqual(
            set(storage.DEFAULT_BACKUP_DATASETS),
            {"users", "eu_taric", "changes", "tariff", "controls"},
        )
        for spec in storage.DATABASES:
            if spec.name in storage.DEFAULT_BACKUP_DATASETS:
                self.assertFalse(spec.replaceable, spec.name)

    def test_the_derived_search_index_is_never_backed_up(self) -> None:
        # Hibrit indeks tümüyle türetilmiş ve en büyük dosyalardan biri.
        spec = next(item for item in storage.DATABASES if item.name == "hybrid_index")
        self.assertTrue(spec.replaceable)
        self.assertNotIn("hybrid_index", storage.DEFAULT_BACKUP_DATASETS)

    def test_disk_usage_is_reported_as_a_percentage(self) -> None:
        disk = self._service(disk_usage=_usage(total=1000, free=250)).disk()
        self.assertTrue(disk["available"])
        self.assertEqual(disk["percent_used"], 75.0)
        self.assertEqual(disk["free_bytes"], 250)

    def test_an_unreadable_disk_does_not_raise(self) -> None:
        def broken(_path):
            raise OSError("disk yok")

        disk = self._service(disk_usage=broken).disk()
        self.assertFalse(disk["available"])
        self.assertEqual(disk["percent_used"], 0.0)

    def test_a_nearly_full_disk_produces_a_warning_naming_the_remedy(self) -> None:
        report = self._service(disk_usage=_usage(total=1000, free=50)).report()
        self.assertTrue(report["warnings"])
        self.assertIn("%95.0 dolu", report["warnings"][0])
        self.assertIn("silinerek yer açılabilir", report["warnings"][0])

    def test_a_healthy_disk_warns_only_about_missing_backups(self) -> None:
        _make_db(self.dir / "users.sqlite3")
        report = self._service(disk_usage=_usage(total=1000, free=900)).report()
        self.assertTrue(all("dolu" not in item for item in report["warnings"]))
        self.assertTrue(any("henüz yedeği yok" in item for item in report["warnings"]))

    def test_the_report_says_plainly_what_a_same_disk_backup_cannot_protect(self) -> None:
        note = self._service().report()["backup"]["note"]
        self.assertIn("diskin tamamen", note)


class BackupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        _make_db(self.dir / "users.sqlite3", rows=20)
        _make_db(self.dir / "eu_taric.sqlite3", rows=7)
        _make_db(self.dir / "hybrid_index.sqlite3", rows=50)

    def _service(self, **kwargs) -> storage.StorageService:
        kwargs.setdefault("disk_usage", _usage())
        kwargs.setdefault("datasets", ("users", "eu_taric"))
        kwargs.setdefault("keep", 2)
        return storage.StorageService(self.dir, **kwargs)

    def test_backup_copies_only_the_configured_databases(self) -> None:
        result = self._service().create_backup()
        self.assertEqual({item["dataset"] for item in result["created"]}, {"users", "eu_taric"})
        names = {path.name for path in (self.dir / storage.BACKUP_DIR_NAME).iterdir()}
        self.assertFalse(any(name.startswith("hybrid_index") for name in names))

    def test_the_copy_is_a_readable_database_with_the_same_rows(self) -> None:
        # VACUUM INTO'nun asıl gerekçesi: kopya gerçekten açılabilir olmalı.
        service = self._service()
        service.create_backup()
        copy = next((self.dir / storage.BACKUP_DIR_NAME).glob("users.*.sqlite3"))
        with sqlite3.connect(copy) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM t").fetchone()[0], 20)

    def test_a_missing_database_is_skipped_with_a_reason_not_an_error(self) -> None:
        result = self._service(datasets=("users", "changes")).create_backup()
        self.assertEqual([item["dataset"] for item in result["skipped"]], ["changes"])
        self.assertEqual(result["errors"], [])

    def test_backup_is_refused_when_the_disk_is_nearly_full(self) -> None:
        # Asıl kural: yedek, korumaya çalıştığı hatayı üretmemeli.
        service = self._service(disk_usage=_usage(total=1000, free=100))
        result = service.create_backup()
        self.assertEqual(result["created"], [])
        self.assertTrue(all("boş alan" in item["reason"] for item in result["skipped"]))
        self.assertTrue(result["errors"])

    def test_rotation_keeps_only_the_newest_copies_per_database(self) -> None:
        service = self._service(keep=2)
        base = datetime(2026, 9, 16, 10, 0, tzinfo=UTC)
        for index in range(4):
            service.create_backup(now=base + timedelta(hours=index))
        copies = sorted(path.name for path in (self.dir / storage.BACKUP_DIR_NAME).glob("users.*.sqlite3"))
        self.assertEqual(len(copies), 2)
        self.assertIn("users.20260916T120000Z.sqlite3", copies)
        self.assertIn("users.20260916T130000Z.sqlite3", copies)

    def test_rotation_reports_what_it_removed(self) -> None:
        service = self._service(keep=1)
        base = datetime(2026, 9, 16, 10, 0, tzinfo=UTC)
        service.create_backup(now=base)
        result = service.create_backup(now=base + timedelta(hours=1))
        self.assertTrue(any(name.startswith("users.20260916T100000Z") for name in result["removed"]))

    def test_listing_backups_returns_newest_first(self) -> None:
        service = self._service(keep=5)
        base = datetime(2026, 9, 16, 10, 0, tzinfo=UTC)
        service.create_backup(now=base)
        service.create_backup(now=base + timedelta(hours=1))
        rows = service.backups()
        self.assertEqual(rows[0]["created_at"], "2026-09-16T11:00:00+00:00")
        self.assertGreater(rows[0]["bytes"], 0)

    def test_unrelated_files_in_the_backup_directory_are_ignored(self) -> None:
        service = self._service()
        service.create_backup()
        (self.dir / storage.BACKUP_DIR_NAME / "not-a-backup.txt").write_text("x", encoding="utf-8")
        self.assertTrue(all(row["name"].endswith(".sqlite3") for row in service.backups()))

    def test_the_report_stops_warning_once_a_backup_exists(self) -> None:
        service = self._service()
        self.assertTrue(any("henüz yedeği yok" in item for item in service.report()["warnings"]))
        service.create_backup()
        self.assertFalse(any("henüz yedeği yok" in item for item in service.report()["warnings"]))

    def test_a_database_name_with_an_underscore_is_listed_and_rotated(self) -> None:
        # Gerileme kilidi: ad kalıbı alt çizgiyi tanımayınca `eu_taric` kopyaları
        # alınıyor ama listelenmiyor, dönüşüme girmiyor (sonsuza kadar birikip
        # diski dolduruyor) ve indirilemiyordu.
        service = self._service(keep=1)
        base = datetime(2026, 9, 16, 10, 0, tzinfo=UTC)
        service.create_backup(now=base)
        service.create_backup(now=base + timedelta(hours=1))
        self.assertIn("eu_taric", {row["dataset"] for row in service.backups()})
        copies = list((self.dir / storage.BACKUP_DIR_NAME).glob("eu_taric.*.sqlite3"))
        self.assertEqual(len(copies), 1, "eski kopya silinmeliydi")
        self.assertIsNotNone(storage.resolve_backup_file(self.dir, copies[0].name))

    def test_backup_now_runs_off_the_event_loop(self) -> None:
        service = self._service()
        result = asyncio.run(service.backup_now())
        self.assertTrue(result["created"])
        self.assertEqual(service.report()["backup"]["last_run"]["at"], result["at"])


class BackupFileResolutionTests(unittest.TestCase):
    """İndirme ucunun tek savunması: ad doğrulaması."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        _make_db(self.dir / "users.sqlite3")
        (self.dir / "gizli.txt").write_text("sır", encoding="utf-8")
        storage.StorageService(self.dir, disk_usage=_usage(), datasets=("users",)).create_backup(
            now=datetime(2026, 9, 16, 12, 0, tzinfo=UTC)
        )

    def test_a_real_backup_resolves(self) -> None:
        path = storage.resolve_backup_file(self.dir, "users.20260916T120000Z.sqlite3")
        self.assertIsNotNone(path)
        self.assertTrue(path.is_file())

    def test_path_traversal_is_refused(self) -> None:
        for name in (
            "../users.sqlite3",
            "../../etc/passwd",
            "..%2Fusers.sqlite3",
            "/etc/passwd",
            "alt/users.20260916T120000Z.sqlite3",
        ):
            with self.subTest(name=name):
                self.assertIsNone(storage.resolve_backup_file(self.dir, name))

    def test_a_file_that_is_not_a_backup_is_refused(self) -> None:
        for name in ("gizli.txt", "users.sqlite3", "", "users.sqlite3.bak"):
            with self.subTest(name=name):
                self.assertIsNone(storage.resolve_backup_file(self.dir, name))

    def test_a_well_formed_name_that_does_not_exist_is_refused(self) -> None:
        self.assertIsNone(storage.resolve_backup_file(self.dir, "users.20200101T000000Z.sqlite3"))

    def test_name_parsing_rejects_a_bad_timestamp(self) -> None:
        self.assertIsNone(storage.parse_backup_name("users.20261301T000000Z.sqlite3"))
        self.assertEqual(storage.parse_backup_name("users.20260916T120000Z.sqlite3")[0], "users")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
