"""`trade_measures` dış bağlantı koruması.

Neden gerekli: bu modülün indirdiği adreslerin bir kısmı sabit değildir — bakanlık
sayfasından **kazınan HTML'den** gelir (`discover_workbook_link`,
`discover_quota_documents`). Yani hedef adres, üçüncü tarafın değiştirebileceği bir
girdidir. Önceki `_get` hiçbir doğrulama yapmadan indiriyordu.

Kilitlenen dört şey:

1. İzin listesi dışındaki bir adres **hiç istek üretmeden** reddedilir.
2. Resmî bir adresten izin listesi dışına **yönlendirme** takip edilmez.
3. Resmî adres normal çalışmaya devam eder (gerileme kilidi).
4. Boyut, `content-length` iddiasına değil, okunan bayta göre sınırlanır.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import trade_measures as tm  # noqa: E402
from security_firewall import SecurityViolation  # noqa: E402

OFFICIAL = "https://ticaret.gov.tr/data/liste.xlsx"


class OutboundGuardTests(unittest.TestCase):
    def _engine(self, handler, *, calls: list[str] | None = None) -> tm.TradeMeasureEngine:
        def wrapped(request: httpx.Request) -> httpx.Response:
            if calls is not None:
                calls.append(str(request.url))
            return handler(request)

        client = httpx.AsyncClient(transport=httpx.MockTransport(wrapped))
        return tm.TradeMeasureEngine(tempfile.mkdtemp(), http=client)

    def _get(self, engine: tm.TradeMeasureEngine, url: str):
        return asyncio.run(engine._get(url))

    def test_an_official_url_still_works(self) -> None:
        # Gerileme kilidi: koruma, çalışan yolu bozmamalı.
        engine = self._engine(lambda request: httpx.Response(200, text="tamam"))
        self.assertEqual(self._get(engine, OFFICIAL).text, "tamam")

    def test_an_off_list_host_is_refused_without_any_request(self) -> None:
        calls: list[str] = []
        engine = self._engine(lambda request: httpx.Response(200, text="gizli"), calls=calls)
        with self.assertRaises(SecurityViolation):
            self._get(engine, "https://evil.example.com/veri.xlsx")
        self.assertEqual(calls, [], "izin listesi dışı adres için istek üretilmemeli")

    def test_plain_http_is_refused(self) -> None:
        engine = self._engine(lambda request: httpx.Response(200, text="x"))
        with self.assertRaises(SecurityViolation):
            self._get(engine, "http://ticaret.gov.tr/data/liste.xlsx")

    def test_a_link_to_the_metadata_service_is_refused(self) -> None:
        # Kazınan HTML bunu içerebilseydi bulut kimlik bilgileri sızardı.
        engine = self._engine(lambda request: httpx.Response(200, text="x"))
        with self.assertRaises(SecurityViolation):
            self._get(engine, "https://169.254.169.254/latest/meta-data/")

    def test_a_redirect_off_the_allow_list_is_not_followed(self) -> None:
        """Asıl korunan şey bu: izin listesindeki adres, dışarı yönlendirebilirdi."""
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "ticaret.gov.tr":
                return httpx.Response(302, headers={"location": "https://evil.example.com/veri.xlsx"})
            return httpx.Response(200, text="gizli")

        engine = self._engine(handler, calls=calls)
        with self.assertRaises(SecurityViolation):
            self._get(engine, OFFICIAL)
        self.assertNotIn("https://evil.example.com/veri.xlsx", calls)

    def test_a_redirect_inside_the_allow_list_is_followed(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/data/liste.xlsx":
                return httpx.Response(302, headers={"location": "https://www.ticaret.gov.tr/data/yeni.xlsx"})
            return httpx.Response(200, text="yeni içerik")

        self.assertEqual(self._get(self._engine(handler), OFFICIAL).text, "yeni içerik")

    def test_a_redirect_loop_stops(self) -> None:
        engine = self._engine(
            lambda request: httpx.Response(302, headers={"location": "https://ticaret.gov.tr/donguye-gir"})
        )
        with self.assertRaises(ValueError):
            self._get(engine, OFFICIAL)

    def test_a_redirect_without_a_target_is_an_error(self) -> None:
        engine = self._engine(lambda request: httpx.Response(302))
        with self.assertRaises(ValueError):
            self._get(engine, OFFICIAL)

    def test_an_oversized_download_is_stopped_while_reading(self) -> None:
        """`content-length` sunucunun iddiasıdır; sınır okunan bayta uygulanır."""
        payload = b"0" * (tm._MAX_DOWNLOAD_BYTES + 1)
        engine = self._engine(
            lambda request: httpx.Response(200, content=payload, headers={"content-length": "10"})
        )
        with self.assertRaises(ValueError) as caught:
            self._get(engine, OFFICIAL)
        self.assertIn("50 MB", str(caught.exception))

    def test_an_http_error_still_raises(self) -> None:
        engine = self._engine(lambda request: httpx.Response(404, text="yok"))
        with self.assertRaises(httpx.HTTPStatusError):
            self._get(engine, OFFICIAL)

    def test_the_allow_list_covers_exactly_the_official_hosts(self) -> None:
        self.assertEqual(set(tm._OFFICIAL_HOSTS), {"ticaret.gov.tr", "mevzuat.gov.tr"})

    def test_every_configured_endpoint_passes_its_own_allow_list(self) -> None:
        """Sabit uçlar listeyle tutarlı olmalı; aksi hâlde eşitleme kendi kapısına takılırdı."""
        from security_firewall import validate_outbound_url

        for url in (
            tm.ANTIDUMPING_PAGE,
            tm.SAFEGUARD_PAGE,
            tm.AGRI_QUOTA_PAGE,
            tm.COMMUNIQUE_PAGE.format(year=2026),
            tm.MEVZUAT_HOME,
            tm.MEVZUAT_DATATABLE,
            tm.MEVZUAT_IFRAME.format(no="42882"),
        ):
            validate_outbound_url(url, allowed_hosts=tm._OFFICIAL_HOSTS)


if __name__ == "__main__":
    unittest.main()
