"""AB EBTI günlük yayın akışı: ayrıştırma, ZIP güvenliği, inceleme kapısı ve arama.

Gerçek ağ erişimi yoktur; yayın listesi ve ZIP gövdeleri ``httpx.MockTransport`` ile üretilir.
"""

from __future__ import annotations

import asyncio
import csv
import io
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ebti_decisions as ebti
from review_policy import ReviewPolicy

COLUMNS = [
    "BTI_REFERENCE", "ISSUING_COUNTRY", "START_DATE_OF_VALIDITY", "END_DATE_OF_VALIDITY",
    "NOMENCLATURE_CODE", "CLASSIFICATION_JUSTIFICATION", "STATUS", "INVALIDATION_REASON",
    "INVALIDATION_JUSTIFICATION", "LANGUAGE", "PLACE_OF_ISSUE", "DATE_OF _ISSUE",
    "NAME_AND_ADDRESS", "DESCRIPTION_OF_GOODS", "KEYWORDS",
]


def _row(reference: str, code: str, description: str, *, country: str = "NL", language: str = "nl", status: str = "VALID") -> dict[str, str]:
    return {
        "BTI_REFERENCE": reference,
        "ISSUING_COUNTRY": country,
        "START_DATE_OF_VALIDITY": "2026-09-11 00:00:00",
        "END_DATE_OF_VALIDITY": "2029-09-10 00:00:00",
        "NOMENCLATURE_CODE": f"{code}************",
        "CLASSIFICATION_JUSTIFICATION": "De algemene regels 1 en 6 voor de interpretatie van de gecombineerde nomenclatuur.",
        "STATUS": status,
        "INVALIDATION_REASON": "",
        "INVALIDATION_JUSTIFICATION": "",
        "LANGUAGE": language,
        "PLACE_OF_ISSUE": "Rotterdam",
        "DATE_OF _ISSUE": "2026-09-11 00:00:00",
        "NAME_AND_ADDRESS": "Gizli Başvuru Sahibi A.Ş., Örnek Mahallesi 1",
        "DESCRIPTION_OF_GOODS": description,
        "KEYWORDS": "USB,OPLADER,SMARTPHONE",
    }


def _csv_text(rows: list[dict[str, str]]) -> str:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=COLUMNS)
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return buffer.getvalue()


def _zip_bytes(name: str, text: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(name, text)
    return buffer.getvalue()


def _listing_html(dates: list[str]) -> str:
    links = "".join(
        f'<tr><td>{date}</td><td><a href="https://ec.europa.eu/taxation_customs/dds2/ebti/'
        f'ebti_export_management.jsp?publicationDate={date}&amp;message=extract">Download</a></td></tr>'
        for date in dates
    )
    return f"<html><body><table>{links}</table></body></html>"


DEFAULT_ROWS = [
    _row("NLBTI2026-0341", "8504408390", "Een statische omvormer voor het opladen van smartphones."),
    _row("FRBTI-2026-0007", "8517130000", "Téléphone intelligent avec écran tactile.", country="FR", language="fr"),
    _row("DEBTI-2026-9", "3920202190", "Transparente Folie aus Polypropylen.", country="DE", language="de", status="INVALID"),
]


class ParserTests(unittest.TestCase):
    def test_parse_publication_list(self):
        html = _listing_html(["2026-09-12 04:41:13", "2026-09-11 04:41:14"])
        publications = ebti.parse_publication_list(html)
        self.assertEqual([item["day"] for item in publications], ["2026-09-12", "2026-09-11"])
        self.assertIn("publicationDate=2026-09-12", publications[0]["url"])
        self.assertIn("message=extract", publications[0]["url"])

    def test_parse_publication_list_deduplicates(self):
        html = _listing_html(["2026-09-12 04:41:13", "2026-09-12 04:41:13"])
        self.assertEqual(len(ebti.parse_publication_list(html)), 1)

    def test_normalise_code_strips_padding(self):
        self.assertEqual(ebti.normalise_code("3920202190************"), "3920202190")
        self.assertEqual(ebti.normalise_code("8517 13 00 00"), "8517130000")

    def test_parse_decisions_reads_official_columns(self):
        decisions, warnings = ebti.parse_decisions(_csv_text(DEFAULT_ROWS))
        self.assertEqual(len(decisions), 3)
        self.assertEqual(warnings, [])
        first = decisions[0]
        self.assertEqual(first["reference"], "NLBTI2026-0341")
        self.assertEqual(first["code"], "8504408390")
        self.assertEqual(first["issuing_country"], "NL")
        self.assertEqual(first["valid_from"], "2026-09-11")
        self.assertEqual(first["valid_to"], "2029-09-10")
        self.assertEqual(first["date_of_issue"], "2026-09-11")  # "DATE_OF _ISSUE" boşluklu başlık
        self.assertEqual(first["language"], "nl")

    def test_applicant_name_and_address_is_never_stored(self):
        decisions, _ = ebti.parse_decisions(_csv_text(DEFAULT_ROWS))
        blob = repr(decisions)
        self.assertNotIn("NAME_AND_ADDRESS", blob)
        self.assertNotIn("Gizli Başvuru Sahibi", blob)

    def test_parse_decisions_warns_on_unreadable_code(self):
        rows = [dict(DEFAULT_ROWS[0], NOMENCLATURE_CODE="—")]
        decisions, warnings = ebti.parse_decisions(_csv_text(rows))
        self.assertEqual(len(decisions), 1)
        self.assertTrue(any("nomenklatür kodu okunamadı" in item for item in warnings))

    def test_parse_decisions_rejects_wrong_header(self):
        with self.assertRaises(ValueError):
            ebti.parse_decisions("a,b,c\n1,2,3\n")

    def test_parse_decisions_rejects_empty_file(self):
        with self.assertRaises(ValueError):
            ebti.parse_decisions(_csv_text([]))

    def test_decision_url(self):
        self.assertIn("reference=NLBTI2026-0341", ebti.decision_url("NLBTI2026-0341"))


class ZipSafetyTests(unittest.TestCase):
    def test_extracts_single_csv(self):
        text = ebti.extract_csv_from_zip(_zip_bytes("EBTI_20260912.csv", _csv_text(DEFAULT_ROWS)))
        self.assertIn("BTI_REFERENCE", text)

    def test_rejects_path_traversal(self):
        payload = _zip_bytes("../../etc/passwd.csv", "x")
        with self.assertRaises(ValueError):
            ebti.extract_csv_from_zip(payload)

    def test_rejects_absolute_path(self):
        payload = _zip_bytes("/etc/shadow.csv", "x")
        with self.assertRaises(ValueError):
            ebti.extract_csv_from_zip(payload)

    def test_rejects_archive_without_csv(self):
        with self.assertRaises(ValueError):
            ebti.extract_csv_from_zip(_zip_bytes("readme.txt", "x"))

    def test_rejects_too_many_members(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            for index in range(ebti._MAX_MEMBERS + 1):
                archive.writestr(f"f{index}.csv", "x")
        with self.assertRaises(ValueError):
            ebti.extract_csv_from_zip(buffer.getvalue())


def _transport(calls: list[str], *, dates: list[str] | None = None, payloads: dict[str, bytes] | None = None) -> httpx.MockTransport:
    dates = dates if dates is not None else ["2026-09-12 04:41:13"]

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if "daily_publications" in request.url.path:
            return httpx.Response(200, html=_listing_html(dates))
        if "ebti_export_management" in request.url.path:
            day = (request.url.params.get("publicationDate") or "")[:10]
            body = (payloads or {}).get(day) or _zip_bytes(f"EBTI_{day}.csv", _csv_text(DEFAULT_ROWS))
            return httpx.Response(200, content=body, headers={"content-type": "application/zip"})
        return httpx.Response(404)

    return httpx.MockTransport(handler)


class EngineTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.calls: list[str] = []

    def _engine(self, *, policy: ReviewPolicy | None = None, **kwargs) -> ebti.EbtiDecisionEngine:
        client = httpx.AsyncClient(transport=_transport(self.calls, **kwargs))
        engine = ebti.EbtiDecisionEngine(self._tmp.name, http=client, review_policy=policy or ReviewPolicy())
        self.addCleanup(lambda: asyncio.run(engine.close()))
        return engine

    def test_sync_ingests_and_search_finds_by_code(self):
        engine = self._engine()
        status = asyncio.run(engine.sync(force=True))
        self.assertEqual(status["ingested"], 1)
        self.assertTrue(status["ready"])
        self.assertEqual(status["decision_count"], 3)
        self.assertEqual(status["latest_publication"][:10], "2026-09-12")

        result = engine.search(code_prefix="851713").as_dict()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(len(result["hits"]), 1)
        hit = result["hits"][0]
        self.assertEqual(hit["reference"], "FRBTI-2026-0007")
        self.assertEqual(hit["code"], "8517130000")
        self.assertEqual(hit["issuing_country"], "FR")
        self.assertIn("reference=FRBTI-2026-0007", hit["url"])
        self.assertIn("bağlayıcı değildir", result["binding_note"])

    def test_search_by_keyword(self):
        engine = self._engine()
        asyncio.run(engine.sync(force=True))
        hits = engine.search("smartphones opladen").hits
        self.assertTrue(hits)
        self.assertEqual(hits[0].code, "8504408390")

    def test_search_before_sync_is_unavailable(self):
        engine = self._engine()
        result = engine.search(code_prefix="8517")
        self.assertEqual(result.status, "unavailable")
        self.assertEqual(result.hits, [])

    def test_short_code_rejected(self):
        engine = self._engine()
        asyncio.run(engine.sync(force=True))
        with self.assertRaises(ValueError):
            engine.search(code_prefix="85")

    def test_second_sync_skips_known_publication(self):
        engine = self._engine()
        asyncio.run(engine.sync(force=True))
        before = len(self.calls)
        again = asyncio.run(engine.sync(force=True))
        self.assertEqual(again["ingested"], 0)
        # Yalnız liste sayfası yeniden okunur, ZIP indirilmez.
        self.assertEqual(len(self.calls), before + 1)

    def test_strict_review_hides_decisions_until_approved(self):
        engine = self._engine(policy=ReviewPolicy(mode="strict"))
        status = asyncio.run(engine.sync(force=True))
        self.assertFalse(status["ready"])
        self.assertEqual(status["pending_review_count"], 1)
        self.assertEqual(engine.search(code_prefix="851713").status, "unavailable")

        pending = engine.pending_reviews()
        self.assertEqual(pending[0]["kind"], "ebti")
        engine.review_snapshot(pending[0]["snapshot_id"], "approve", reviewed_by="editor@example.com")
        self.assertTrue(engine.status()["ready"])
        self.assertEqual(len(engine.search(code_prefix="851713").hits), 1)

    def test_rejected_publication_stays_hidden(self):
        engine = self._engine(policy=ReviewPolicy(mode="strict"))
        asyncio.run(engine.sync(force=True))
        snapshot_id = engine.pending_reviews()[0]["snapshot_id"]
        engine.review_snapshot(snapshot_id, "reject", reviewed_by="editor@example.com", note="hatalı")
        self.assertFalse(engine.status()["ready"])
        self.assertEqual(engine.pending_reviews(), [])

    def test_multiple_publications_are_ingested_oldest_first(self):
        engine = self._engine(dates=["2026-09-12 04:41:13", "2026-09-11 04:41:14", "2026-09-10 04:41:15"])
        status = asyncio.run(engine.sync(force=True))
        self.assertEqual(status["ingested"], 3)
        self.assertEqual(status["publication_count"], 3)

    def test_broken_file_does_not_stop_the_others(self):
        engine = self._engine(
            dates=["2026-09-12 04:41:13", "2026-09-11 04:41:14"],
            payloads={"2026-09-11": b"not-a-zip"},
        )
        status = asyncio.run(engine.sync(force=True))
        self.assertEqual(status["ingested"], 1)
        self.assertTrue(status["errors"])

    def test_corpus_rows_for_hybrid_index(self):
        engine = self._engine()
        asyncio.run(engine.sync(force=True))
        rows = engine.corpus_rows()
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]["corpus"], "ebti")
        self.assertTrue(rows[0]["source_url"].startswith("https://ec.europa.eu/"))
        self.assertTrue(all("Gizli Başvuru Sahibi" not in row["text"] for row in rows))

    def test_ledger_batch_recorded(self):
        recorded: list[dict] = []

        class FakeLedger:
            def record_batch(self, **kwargs):
                recorded.append(kwargs)
                return "batch-ebti"

            def has_batch(self, batch_id):
                return False

        engine = self._engine()
        engine.ledger = FakeLedger()
        asyncio.run(engine.sync(force=True))
        self.assertEqual(len(recorded), 1)
        self.assertEqual(recorded[0]["kind"], "ebti")
        self.assertEqual(recorded[0]["total_rows"], 3)

    def test_redirect_outside_allow_list_is_blocked(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(302, headers={"location": "https://evil.test/data.zip"})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        engine = ebti.EbtiDecisionEngine(self._tmp.name, http=client)
        self.addCleanup(lambda: asyncio.run(engine.close()))
        status = asyncio.run(engine.sync(force=True))
        self.assertFalse(status["ready"])
        self.assertTrue(status["errors"])


if __name__ == "__main__":
    unittest.main()
