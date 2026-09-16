"""Geçmiş arşivin aranabilirliği (FAZ 8.0) — `as_of` ve indekste geçmiş sürümler.

En kritik iki değişmez:

1. **Gerileme kilidi:** ``as_of`` verilmediğinde davranış göç öncesiyle birebir aynıdır —
   yalnız yürürlükteki sürüm döner.
2. **Künyesiz geçmiş cevabı yok:** tarihi bilinmeyen bir satır geçmiş sorgusunda elenir;
   dönen her geçmiş kayıt ``source_sha256`` ve yürürlük aralığını taşır.
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import hybrid_corpora  # noqa: E402
from hybrid_index import HybridIndex  # noqa: E402
from unified_search import UnifiedSearchEngine, _snapshot_filter  # noqa: E402


def _doc(doc_id: str, text: str, *, sha: str, **validity) -> dict:
    return {
        "id": doc_id,
        "corpus": "controls",
        "title": "Oyuncak Denetimi Tebliği",
        "text": text,
        "gtip_codes": ["950300"],
        "source_url": "https://ticaret.gov.tr/ornek",
        "source_sha256": sha,
        "snapshot_id": doc_id.split(":")[1],
        **validity,
    }


class IndexValidityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.index = HybridIndex(data_dir=Path(self.tmp.name), embedder=None)
        # Eski sürüm 2024-01-01 → 2025-06-01 arası; yeni sürüm 2025-06-01'den beri yürürlükte.
        self.index.upsert_documents(
            [
                _doc(
                    "control:s1:950300:scope",
                    "Tekerlekli oyuncaklar eski kapsam",
                    sha="sha-old",
                    as_of_from="2024-01-01",
                    as_of_to="2025-06-01",
                    snapshot_active=False,
                ),
                _doc(
                    "control:s2:950300:scope",
                    "Tekerlekli oyuncaklar yeni kapsam",
                    sha="sha-new",
                    as_of_from="2025-06-01",
                    as_of_to="",
                    snapshot_active=True,
                ),
            ]
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _ids(self, **kwargs) -> list[str]:
        return [item["id"] for item in self.index.search_lexical("oyuncaklar", limit=10, **kwargs)["items"]]

    def test_without_as_of_only_the_active_version_is_returned(self) -> None:
        # Gerileme kilidi: göç öncesi davranış.
        self.assertEqual(self._ids(), ["control:s2:950300:scope"])

    def test_as_of_selects_the_version_in_force_on_that_day(self) -> None:
        self.assertEqual(self._ids(as_of="2024-07-01"), ["control:s1:950300:scope"])
        self.assertEqual(self._ids(as_of="2026-01-01"), ["control:s2:950300:scope"])

    def test_the_boundary_day_belongs_to_the_new_version(self) -> None:
        # valid_to dışlayıcı, valid_from kapsayıcıdır: aynı gün iki sürüm birden dönmez.
        self.assertEqual(self._ids(as_of="2025-06-01"), ["control:s2:950300:scope"])

    def test_a_day_before_every_version_returns_nothing(self) -> None:
        self.assertEqual(self._ids(as_of="2020-01-01"), [])

    def test_a_row_without_a_known_range_is_dropped_from_history(self) -> None:
        # Tarihi doğrulanamayan kaydı "o gün yürürlükteydi" diye göstermek kanıtsız olurdu.
        self.index.upsert_documents([_doc("control:s3:950300:scope", "Tarihsiz oyuncaklar kaydı", sha="sha-x")])
        self.assertIn("control:s3:950300:scope", self._ids())
        self.assertNotIn("control:s3:950300:scope", self._ids(as_of="2024-07-01"))

    def test_history_results_carry_their_provenance(self) -> None:
        item = self.index.search_lexical("oyuncaklar", limit=10, as_of="2024-07-01")["items"][0]
        self.assertEqual(item["source_sha256"], "sha-old")
        self.assertEqual(item["snapshot_id"], "s1")
        self.assertEqual(item["as_of_from"], "2024-01-01")
        self.assertEqual(item["as_of_to"], "2025-06-01")
        self.assertFalse(item["snapshot_active"])

    def test_the_response_reports_which_day_was_asked(self) -> None:
        current = self.index.search_lexical("oyuncaklar", limit=10)
        self.assertIsNone(current["as_of"])
        self.assertFalse(current["history"])
        past = self.index.search_lexical("oyuncaklar", limit=10, as_of="2024-07-01")
        self.assertEqual(past["as_of"], "2024-07-01")
        self.assertTrue(past["history"])

    def test_status_counts_the_archived_versions(self) -> None:
        self.assertEqual(self.index.status()["historical_count"], 1)


class RetimingTests(unittest.TestCase):
    """Metin aynı kalıp yalnız yürürlük aralığı kapandığında ne olur."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.index = HybridIndex(data_dir=Path(self.tmp.name), embedder=None)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_identical_text_with_a_new_range_is_retimed_not_reembedded(self) -> None:
        payload = _doc("control:s1:950300:scope", "Tekerlekli oyuncaklar", sha="sha-1", as_of_from="2024-01-01")
        self.assertEqual(self.index.upsert_documents([payload])["inserted"], 1)
        counts = self.index.upsert_documents(
            [{**payload, "as_of_to": "2025-06-01", "snapshot_active": False}]
        )
        self.assertEqual(counts["retimed"], 1)
        self.assertEqual(counts["updated"], 0)
        self.assertEqual(counts["skipped"], 0)
        self.assertEqual(self.index.status()["historical_count"], 1)

    def test_an_unchanged_document_is_still_skipped(self) -> None:
        payload = _doc("control:s1:950300:scope", "Tekerlekli oyuncaklar", sha="sha-1", as_of_from="2024-01-01")
        self.index.upsert_documents([payload])
        counts = self.index.upsert_documents([payload])
        self.assertEqual(counts["skipped"], 1)
        self.assertEqual(counts["retimed"], 0)

    def test_history_survives_a_refresh_that_still_feeds_it(self) -> None:
        # Asıl mesele buydu: besleme yalnız aktif sürümü verdiği sürece reindex eskiyi siliyordu.
        old = _doc("control:s1:950300:scope", "Eski kapsam", sha="sha-old", as_of_from="2024-01-01",
                   as_of_to="2025-06-01", snapshot_active=False)
        new = _doc("control:s2:950300:scope", "Yeni kapsam", sha="sha-new", as_of_from="2025-06-01",
                   snapshot_active=True)
        self.index.reindex("controls", [old, new])
        self.index.reindex("controls", [old, new])
        self.assertEqual(self.index.status()["document_count"], 2)
        self.assertEqual(self.index.status()["historical_count"], 1)


class ControlFeederHistoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "controls.sqlite3"
        with sqlite3.connect(self.db_path) as connection:
            connection.executescript(
                """
                CREATE TABLE control_snapshots (id TEXT PRIMARY KEY, code TEXT, title TEXT, authority TEXT,
                    system TEXT, source_url TEXT, document_sha256 TEXT, active INTEGER,
                    valid_from TEXT, valid_to TEXT);
                CREATE TABLE control_scope (snapshot_id TEXT, gtip_prefix TEXT, description TEXT,
                    source_line TEXT, excluded INTEGER, list_kind TEXT);
                INSERT INTO control_snapshots VALUES
                    ('s1','2026/9','Oyuncak Denetimi','Ticaret','TAREKS','https://x.gov.tr','sha1',1,'2026-01-01',NULL),
                    ('s2','2025/9','Oyuncak Denetimi','Ticaret','TAREKS','https://y.gov.tr','sha2',0,'2025-01-01','2026-01-01');
                INSERT INTO control_scope VALUES
                    ('s1','950300','Yeni kapsam','satır',0,'scope'),
                    ('s2','950300','Eski kapsam','satır',0,'scope');
                """
            )
        self.engine = type("E", (), {"db_path": self.db_path})()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_default_feed_is_unchanged(self) -> None:
        docs = hybrid_corpora.control_documents(self.engine)
        self.assertEqual([doc["snapshot_id"] for doc in docs], ["s1"])

    def test_history_feed_adds_the_retired_version_with_its_range(self) -> None:
        docs = sorted(hybrid_corpora.control_documents(self.engine, include_history=True), key=lambda d: d["id"])
        self.assertEqual([doc["snapshot_id"] for doc in docs], ["s1", "s2"])
        retired = docs[1]
        self.assertEqual(retired["as_of_from"], "2025-01-01")
        self.assertEqual(retired["as_of_to"], "2026-01-01")
        self.assertFalse(retired["snapshot_active"])

    def test_document_ids_did_not_change(self) -> None:
        # Kimlik şeması değişseydi mevcut ~tüm belgeler yeniden gömülürdü.
        docs = hybrid_corpora.control_documents(self.engine)
        self.assertEqual(docs[0]["id"], "control:s1:950300:scope")

    def test_a_minimal_schema_does_not_kill_the_corpus(self) -> None:
        # Tek eksik sütun yüzünden korpusun tamamının sessizce düşmemesi gerekir.
        path = Path(self.tmp.name) / "old.sqlite3"
        with sqlite3.connect(path) as connection:
            connection.executescript(
                """
                CREATE TABLE control_snapshots (id TEXT PRIMARY KEY, code TEXT, title TEXT, authority TEXT,
                    system TEXT, source_url TEXT, document_sha256 TEXT, active INTEGER);
                CREATE TABLE control_scope (snapshot_id TEXT, gtip_prefix TEXT, description TEXT,
                    source_line TEXT, excluded INTEGER, list_kind TEXT);
                INSERT INTO control_snapshots VALUES ('s1','2026/9','T','A','TAREKS','https://x.gov.tr','sha',1);
                INSERT INTO control_scope VALUES ('s1','950300','Kapsam','satır',0,'scope');
                """
            )
        docs = hybrid_corpora.control_documents(type("E", (), {"db_path": path})(), include_history=True)
        self.assertEqual(len(docs), 1)
        self.assertEqual(docs[0]["as_of_from"], "")


class ClassificationFeederHistoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "classification-evidence.sqlite3"
        with sqlite3.connect(self.db_path) as connection:
            connection.executescript(
                """
                CREATE TABLE snapshots (id TEXT PRIMARY KEY, source_url TEXT, archive_sha256 TEXT,
                    retrieved_at TEXT, active INTEGER);
                CREATE TABLE pages (id TEXT PRIMARY KEY, snapshot_id TEXT, page_number INTEGER,
                    codes_json TEXT, content TEXT);
                INSERT INTO snapshots VALUES
                    ('old','https://eu.example/old','sha-old','2024-03-01T00:00:00+00:00',0),
                    ('new','https://eu.example/new','sha-new','2025-09-01T00:00:00+00:00',1);
                INSERT INTO pages VALUES
                    ('p-old','old',1,'["8517120000"]','Eski tüzük metni'),
                    ('p-new','new',1,'["8517120000"]','Yeni tüzük metni');
                """
            )
        self.engine = type("E", (), {"database_path": self.db_path})()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_default_feed_is_unchanged(self) -> None:
        docs = hybrid_corpora.classification_documents(self.engine)
        self.assertEqual({doc["snapshot_id"] for doc in docs}, {"new"})

    def test_observed_boundaries_are_derived_from_retrieval_dates(self) -> None:
        docs = hybrid_corpora.classification_documents(self.engine, include_history=True)
        by_snapshot = {doc["snapshot_id"]: doc for doc in docs}
        self.assertEqual(by_snapshot["old"]["as_of_from"], "2024-03-01")
        self.assertEqual(by_snapshot["old"]["as_of_to"], "2025-09-01")
        self.assertEqual(by_snapshot["new"]["as_of_from"], "2025-09-01")
        self.assertEqual(by_snapshot["new"]["as_of_to"], "")


class UnifiedSearchFilterTests(unittest.TestCase):
    def test_without_as_of_the_sql_is_exactly_todays(self) -> None:
        # Gerileme kilidi: üretilen SQL parçası göç öncesiyle birebir aynı.
        clause, params = _snapshot_filter(None)
        self.assertEqual(clause, "d.active=1")
        self.assertEqual(params, [])

    def test_with_as_of_the_range_is_bounded_on_both_sides(self) -> None:
        clause, params = _snapshot_filter("2024-07-01")
        self.assertNotIn("active=1", clause)
        self.assertIn("valid_from", clause)
        self.assertIn("valid_to", clause)
        self.assertEqual(params, ["2024-07-01", "2024-07-01"])

    def test_rows_without_a_valid_from_are_excluded_from_history(self) -> None:
        clause, _ = _snapshot_filter("2024-07-01")
        self.assertIn("d.valid_from IS NOT NULL", clause)
        self.assertIn("d.valid_from != ''", clause)


class UnifiedSearchHistoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        data_dir = Path(self.tmp.name)
        with sqlite3.connect(data_dir / "controls.sqlite3") as connection:
            connection.executescript(
                """
                CREATE TABLE control_snapshots (id TEXT PRIMARY KEY, code TEXT, title TEXT, category TEXT,
                    authority TEXT, system TEXT, source_url TEXT, document_sha256 TEXT, retrieved_at TEXT,
                    valid_from TEXT, valid_to TEXT, active INTEGER);
                CREATE TABLE control_scope (snapshot_id TEXT, gtip_prefix TEXT, description TEXT,
                    source_line TEXT, excluded INTEGER, list_kind TEXT);
                INSERT INTO control_snapshots VALUES
                    ('s1','2026/9','Oyuncak Denetimi','ugd','Ticaret','TAREKS','https://x.gov.tr','sha1',
                     '2026-01-02T00:00:00+00:00','2026-01-01',NULL,1),
                    ('s2','2025/9','Oyuncak Denetimi','ugd','Ticaret','TAREKS','https://y.gov.tr','sha2',
                     '2025-01-02T00:00:00+00:00','2025-01-01','2026-01-01',0);
                INSERT INTO control_scope VALUES
                    ('s1','950300','Tekerlekli oyuncaklar yeni','satır',0,'scope'),
                    ('s2','950300','Tekerlekli oyuncaklar eski','satır',0,'scope');
                """
            )
        self.engine = UnifiedSearchEngine(data_dir=data_dir)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _codes(self, **kwargs) -> list[str]:
        result = self.engine.search_all("oyuncaklar", category="denetim", **kwargs)
        return [row["communique_code"] for row in result["categories"]["denetim"]]

    def test_without_as_of_only_the_active_communique_is_returned(self) -> None:
        self.assertEqual(self._codes(), ["2026/9"])

    def test_as_of_returns_the_communique_in_force_that_day(self) -> None:
        self.assertEqual(self._codes(as_of="2025-07-01"), ["2025/9"])

    def test_history_rows_carry_their_provenance(self) -> None:
        row = self.engine.search_all("oyuncaklar", category="denetim", as_of="2025-07-01")["categories"]["denetim"][0]
        self.assertEqual(row["source_sha256"], "sha2")
        self.assertEqual(row["valid_from"], "2025-01-01")
        self.assertEqual(row["valid_to"], "2026-01-01")
        self.assertFalse(row["snapshot_active"])

    def test_the_response_reports_which_day_was_asked(self) -> None:
        self.assertFalse(self.engine.search_all("oyuncaklar")["history"])
        past = self.engine.search_all("oyuncaklar", as_of="2025-07-01")
        self.assertTrue(past["history"])
        self.assertEqual(past["as_of"], "2025-07-01")

    def test_an_empty_query_still_reports_the_day(self) -> None:
        result = self.engine.search_all("", as_of="2025-07-01")
        self.assertEqual(result["as_of"], "2025-07-01")
        self.assertEqual(result["total_count"], 0)


class SearchRouteAsOfTests(unittest.TestCase):
    """Geçmiş tarihli arama `temporal_query` yeteneğine bağlıdır; bugünkü arama herkese açık."""

    def setUp(self) -> None:
        from starlette.testclient import TestClient

        import app as web_app
        from account_service import AccountService
        from auth_service import GoogleAuthService

        self.web_app = web_app
        self.tmp = tempfile.TemporaryDirectory()
        data_dir = Path(self.tmp.name)
        self.auth = GoogleAuthService(
            client_id="test-client",
            client_secret="test-secret",
            session_secret="test-session-secret-that-is-long-enough",
            data_dir=data_dir,
        )
        self.accounts = AccountService(data_dir, admin_emails="admin@example.com")
        self.free = {"sub": "free-sub", "email": "free@example.com", "name": "Free", "picture": ""}
        self.paid = {"sub": "paid-sub", "email": "paid@example.com", "name": "Paid", "picture": ""}
        self.admin = {"sub": "admin-sub", "email": "admin@example.com", "name": "Admin", "picture": ""}
        with sqlite3.connect(self.accounts.db_path) as connection:
            for item in (self.free, self.paid, self.admin):
                connection.execute(
                    "INSERT INTO users(google_sub,email,name,picture,created_at,last_login_at) VALUES(?,?,?,?,1,1)",
                    (item["sub"], item["email"], item["name"], item["picture"]),
                )
        # `temporal_query` Ekip (premium) ve üstünde açıktır.
        self.accounts.admin_set_plan(self.admin, "paid-sub", "team", "active")
        self.original = (web_app.google_auth, web_app.account_service, web_app.rate_limiter)
        web_app.google_auth = self.auth
        web_app.account_service = self.accounts
        web_app.rate_limiter = web_app.FixedWindowRateLimiter()
        self.client = TestClient(web_app.app, base_url="https://gumruksor.com")

    def tearDown(self) -> None:
        self.client.close()
        (
            self.web_app.google_auth,
            self.web_app.account_service,
            self.web_app.rate_limiter,
        ) = self.original
        self.tmp.cleanup()

    def get(self, path: str, user: dict | None = None):
        headers = {"Origin": "https://gumruksor.com"}
        if user:
            headers["Cookie"] = f"{self.auth.session_cookie}={self.auth.create_session(user)}"
        return self.client.get(path, headers=headers)

    def test_todays_search_stays_open_to_everyone(self) -> None:
        response = self.get("/api/search/unified?q=oyuncak")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIsNone(response.json()["as_of"])
        self.assertFalse(response.json()["history"])

    def test_a_past_date_needs_the_temporal_query_feature(self) -> None:
        locked = self.get("/api/search/unified?q=oyuncak&as_of=2025-07-01", self.free)
        self.assertEqual(locked.status_code, 403, locked.text)
        self.assertEqual(locked.json()["feature"], "temporal_query")

        allowed = self.get("/api/search/unified?q=oyuncak&as_of=2025-07-01", self.paid)
        self.assertEqual(allowed.status_code, 200, allowed.text)
        self.assertEqual(allowed.json()["as_of"], "2025-07-01")
        self.assertTrue(allowed.json()["history"])

    def test_autocomplete_and_hybrid_routes_share_the_same_gate(self) -> None:
        for path in ("/api/tariff/autocomplete?q=oyuncak", "/api/search/hybrid?q=oyuncak"):
            self.assertEqual(self.get(path).status_code, 200, path)
            locked = self.get(f"{path}&as_of=2025-07-01", self.free)
            self.assertEqual(locked.status_code, 403, f"{path}: {locked.text}")
            self.assertEqual(locked.json()["feature"], "temporal_query")

    def test_an_unparseable_date_is_rejected(self) -> None:
        response = self.get("/api/search/unified?q=oyuncak&as_of=dun", self.paid)
        self.assertEqual(response.status_code, 422, response.text)


if __name__ == "__main__":
    unittest.main()
