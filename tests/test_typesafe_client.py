"""Jev (TypeSafe) istemcisi ve alt satır daraltması.

Kilitlenen davranışlar:

* İstek yalnız resmî uç noktaya gider, yönlendirme izlenmez, anahtar hiçbir hata metnine
  sızmaz, durum metni kişisel veri ve gizli değerden arındırılır.
* Jev'in döndürdüğü seçim, bizim verdiğimiz resmî seçenekler arasında değilse atılır —
  model kod uyduramaz.
* Daraltma varsayılan olarak kapalıdır; anahtar tek başına davranışı değiştirmez.
* Emin olmayan, "hiçbiri" diyen ya da ayırt edici resmî tanımı olmayan alt satırlarda kod
  6 hanede kalır; Jev'in her hatası sınıflandırmayı bozmadan yutulur.
"""

from __future__ import annotations

import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

from customs_advisor import CustomsAdvisor, ProductClassificationRequest
from typesafe_client import API_URL, ChoiceAnswer, JevError, TypeSafeClient

# Parçalardan kurulur: tek bir tırnaklı "anahtar" dizisi olarak yazılsaydı gitleaks'in genel
# API anahtarı kuralı bu sahte değeri gerçek sızıntı sanardı.
_KEY = "test-" + "x" * 24


def _choice(criteria: dict[str, str]) -> dict:
    return {"type": "choice", "instructions": "Hangisi?", "criteria": criteria}


def _ok_response(answers: dict) -> httpx.Response:
    return httpx.Response(200, json={"model": "jev-1.13.0", "answers": answers})


class TypeSafeClientTests(unittest.IsolatedAsyncioTestCase):
    async def _client(self, handler) -> tuple[TypeSafeClient, list[httpx.Request]]:
        seen: list[httpx.Request] = []

        def record(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return handler(request)

        http = httpx.AsyncClient(transport=httpx.MockTransport(record))
        self.addAsyncCleanup(http.aclose)
        return TypeSafeClient(api_key=_KEY, http=http, model="jev-latest"), seen

    async def test_the_request_goes_only_to_the_official_endpoint_with_a_bearer_key(self):
        client, seen = await self._client(
            lambda request: _ok_response(
                {"q": {"type": "choice", "choice": "a", "confidence": 0.9, "probabilities": {"a": 0.9, "b": 0.1}}}
            )
        )
        answers = await client.choose("Pamuklu tişört", {"q": _choice({"a": "A", "b": "B"})})
        self.assertEqual(len(seen), 1)
        self.assertEqual(str(seen[0].url), API_URL)
        self.assertEqual(seen[0].url.host, "api.typesafe.ai", "topluluk vekili değil, resmî uç nokta")
        self.assertEqual(seen[0].headers["authorization"], f"Bearer {_KEY}")
        body = json.loads(seen[0].content)
        self.assertEqual(body["model"], "jev-latest")
        self.assertEqual(body["questions"]["q"]["criteria"], {"a": "A", "b": "B"})
        self.assertEqual(answers["q"], ChoiceAnswer("a", 0.9, {"a": 0.9, "b": 0.1}))

    async def test_personal_data_is_redacted_from_the_state_before_it_leaves(self):
        client, seen = await self._client(lambda request: _ok_response({}))
        await client.choose(
            "Satıcı: ali@example.com, TCKN 10000000146, ürün pamuklu tişört",
            {"q": _choice({"a": "A", "b": "B"})},
        )
        state = json.loads(seen[0].content)["state"]
        self.assertNotIn("ali@example.com", state)
        self.assertNotIn("10000000146", state)
        self.assertIn("pamuklu tişört", state)

    async def test_a_choice_outside_the_official_options_is_discarded(self):
        """Model listede olmayan bir kod 'seçerse' o cevap hatta hiç girmez."""
        client, _ = await self._client(
            lambda request: _ok_response(
                {"q": {"type": "choice", "choice": "99999999", "confidence": 0.99, "probabilities": {}}}
            )
        )
        answers = await client.choose("x", {"q": _choice({"72107010": "A", "72107080": "B"})})
        self.assertEqual(answers, {})

    async def test_an_out_of_range_confidence_is_discarded(self):
        client, _ = await self._client(
            lambda request: _ok_response({"q": {"type": "choice", "choice": "a", "confidence": 1.7}})
        )
        self.assertEqual(await client.choose("x", {"q": _choice({"a": "A", "b": "B"})}), {})

    async def test_an_error_never_leaks_the_key_even_if_the_server_echoes_it(self):
        client, _ = await self._client(
            lambda request: httpx.Response(401, json={"error": {"message": f"invalid key {_KEY}"}})
        )
        with self.assertRaises(JevError) as caught:
            await client.choose("x", {"q": _choice({"a": "A", "b": "B"})})
        self.assertIn("401", str(caught.exception))
        self.assertNotIn(_KEY, str(caught.exception))

    async def test_a_redirect_is_not_followed(self):
        client, seen = await self._client(
            lambda request: httpx.Response(302, headers={"location": "https://evil.example/steal"})
        )
        with self.assertRaises(JevError):
            await client.choose("x", {"q": _choice({"a": "A", "b": "B"})})
        self.assertEqual([request.url.host for request in seen], ["api.typesafe.ai"])

    async def test_a_transport_failure_is_reported_without_the_key(self):
        def fail(request):
            raise httpx.ConnectError(f"boom {_KEY}")

        client, _ = await self._client(fail)
        with self.assertRaises(JevError) as caught:
            await client.choose("x", {"q": _choice({"a": "A", "b": "B"})})
        self.assertNotIn(_KEY, str(caught.exception))

    async def test_an_oversized_response_is_refused(self):
        client, _ = await self._client(lambda request: httpx.Response(200, content=b"x" * 300_000))
        with self.assertRaises(JevError):
            await client.choose("x", {"q": _choice({"a": "A", "b": "B"})})

    async def test_without_a_key_no_request_is_made(self):
        seen: list[httpx.Request] = []
        http = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: seen.append(r) or _ok_response({})))
        self.addAsyncCleanup(http.aclose)
        client = TypeSafeClient(api_key="", http=http)
        self.assertFalse(client.configured)
        with self.assertRaises(JevError):
            await client.choose("x", {"q": _choice({"a": "A", "b": "B"})})
        self.assertEqual(seen, [])

    async def test_malformed_questions_are_refused_before_any_request(self):
        client, seen = await self._client(lambda request: _ok_response({}))
        with self.assertRaises(JevError):
            await client.choose("x", {"q": {"type": "score", "criteria": {"a": "A"}}})
        with self.assertRaises(JevError):
            await client.choose("x", {"q": _choice({str(i): "o" for i in range(256)})})
        self.assertEqual(seen, [])


class TypeSafeDiagnoseTests(unittest.IsolatedAsyncioTestCase):
    async def test_diagnose_without_a_key_says_so_and_sends_nothing(self):
        seen: list[httpx.Request] = []
        http = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: seen.append(r) or _ok_response({})))
        self.addAsyncCleanup(http.aclose)
        report = await TypeSafeClient(api_key="", http=http).diagnose()
        self.assertFalse(report["configured"])
        self.assertFalse(report["ok"])
        self.assertIn("TYPESAFE_API_KEY", report["error"])
        self.assertEqual(seen, [])

    async def test_diagnose_uses_a_synthetic_probe_and_reports_success(self):
        seen: list[httpx.Request] = []

        def handler(request):
            seen.append(request)
            return _ok_response({"probe": {"type": "choice", "choice": "tekstil", "confidence": 0.97}})

        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(http.aclose)
        report = await TypeSafeClient(api_key=_KEY, http=http).diagnose()
        self.assertTrue(report["ok"])
        self.assertEqual(report["probe_choice"], "tekstil")
        self.assertNotIn(_KEY, json.dumps(report))
        # Sentetik cümle; müşteri verisi değil.
        self.assertIn("tişört", json.loads(seen[0].content)["state"])

    async def test_a_rejected_key_shows_up_as_a_failed_probe(self):
        """jevai.org gibi resmî olmayan bir anahtar burada 401/403 olarak görünür."""
        http = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(401, json={"error": "unauthorized"}))
        )
        self.addAsyncCleanup(http.aclose)
        report = await TypeSafeClient(api_key=_KEY, http=http).diagnose()
        self.assertTrue(report["configured"])
        self.assertFalse(report["ok"])
        self.assertIn("401", report["error"])
        self.assertNotIn(_KEY, json.dumps(report))


# ---------------------------------------------------------------- daraltma
class _FakeTariff:
    """721070 → iki CN8 alt satırı; 72107080 → tek satır (daraltma sonrası doğrulama)."""

    CHILDREN = {
        "721070": ["721070100000", "721070800000"],
        "72107010": ["721070100000"],
        "72107080": ["721070800000"],
    }

    async def lookup(self, code, **kwargs):
        gtips = self.CHILDREN.get(code, [])
        return SimpleNamespace(
            matched_gtip_count=len(gtips),
            matched_gtips=list(gtips),
            unambiguous_rates={},
            ambiguous_measure_types=[],
            rate_variants={},
        )


class _FakeNomenclature:
    def __init__(self, rows: dict[str, dict]):
        self.rows = rows

    def describe_many(self, codes):
        return {code: self.rows[code] for code in codes if code in self.rows}

    def search(self, text, limit=10):
        return {"hits": []}


_ROWS = {
    "721070": {"description": "Boyanmış, verniklenmiş veya plastikle kaplanmış", "source": "exact"},
    "72107010": {"description": "Teneke; verniklenmiş", "source": "descendants"},
    "72107080": {"description": "Diğerleri", "source": "descendants"},
}


class _FakeJev:
    def __init__(self, answers=None, *, configured=True, error: Exception | None = None):
        self.answers = answers or {}
        self.configured = configured
        self.error = error
        self.calls: list[tuple[str, dict]] = []

    async def choose(self, state, questions):
        self.calls.append((state, questions))
        if self.error:
            raise self.error
        return self.answers


def _request(**extra) -> ProductClassificationRequest:
    return ProductClassificationRequest(
        product_description="Plastikle kaplanmış, soğuk haddelenmiş yassı çelik rulo; genişlik 1250 mm",
        composition="Demir dışı olmayan alaşımsız çelik",
        **extra,
    )


class JevNarrowingTests(unittest.IsolatedAsyncioTestCase):
    def _advisor(self, jev, rows=_ROWS) -> CustomsAdvisor:
        advisor = CustomsAdvisor(tariff_engine=_FakeTariff())
        advisor.nomenclature_engine = _FakeNomenclature(rows)
        advisor.typesafe_client = jev
        self.addAsyncCleanup(advisor.close)
        return advisor

    async def test_narrowing_is_off_by_default_even_with_a_key(self):
        jev = _FakeJev({"hs721070": ChoiceAnswer("72107080", 0.95)})
        with patch.dict(os.environ, {"JEV_NARROWING_ENABLED": ""}):
            chosen = await self._advisor(jev)._jev_narrow(["721070"], _request())
        self.assertEqual(chosen, {})
        self.assertEqual(jev.calls, [], "kapalıyken hiçbir istek gönderilmemeli")

    async def test_a_confident_choice_among_official_children_narrows_the_candidate(self):
        jev = _FakeJev({"hs721070": ChoiceAnswer("72107080", 0.9)})
        with patch.dict(os.environ, {"JEV_NARROWING_ENABLED": "1"}):
            chosen = await self._advisor(jev)._jev_narrow(["721070"], _request())
        self.assertEqual(chosen, {"721070": ("72107080", 0.9)})
        criteria = jev.calls[0][1]["hs721070"]["criteria"]
        # Seçenekler yalnız resmî alt satırlar + "hiçbiri".
        self.assertEqual(set(criteria), {"72107010", "72107080", "hicbiri"})
        self.assertEqual(criteria["72107080"], "Diğerleri")

    async def test_a_low_confidence_choice_leaves_the_code_at_six_digits(self):
        jev = _FakeJev({"hs721070": ChoiceAnswer("72107080", 0.4)})
        with patch.dict(os.environ, {"JEV_NARROWING_ENABLED": "1"}):
            self.assertEqual(await self._advisor(jev)._jev_narrow(["721070"], _request()), {})

    async def test_the_threshold_is_configurable(self):
        jev = _FakeJev({"hs721070": ChoiceAnswer("72107080", 0.4)})
        with patch.dict(os.environ, {"JEV_NARROWING_ENABLED": "1", "JEV_MIN_CONFIDENCE": "0.3"}):
            chosen = await self._advisor(jev)._jev_narrow(["721070"], _request())
        self.assertEqual(chosen, {"721070": ("72107080", 0.4)})

    async def test_none_of_these_leaves_the_code_at_six_digits(self):
        jev = _FakeJev({"hs721070": ChoiceAnswer("hicbiri", 0.99)})
        with patch.dict(os.environ, {"JEV_NARROWING_ENABLED": "1"}):
            self.assertEqual(await self._advisor(jev)._jev_narrow(["721070"], _request()), {})

    async def test_a_child_without_a_distinguishing_official_description_is_not_asked(self):
        rows = dict(_ROWS)
        rows["72107010"] = {"description": "Boyanmış…", "source": "ancestor"}
        jev = _FakeJev({"hs721070": ChoiceAnswer("72107080", 0.95)})
        with patch.dict(os.environ, {"JEV_NARROWING_ENABLED": "1"}):
            chosen = await self._advisor(jev, rows)._jev_narrow(["721070"], _request())
        self.assertEqual(chosen, {})
        self.assertEqual(jev.calls, [], "ayırt edilemeyen seçenekle soru sorulmaz")

    async def test_siblings_with_identical_descriptions_are_not_asked(self):
        rows = dict(_ROWS)
        rows["72107010"] = {"description": "Diğerleri", "source": "descendants"}
        jev = _FakeJev({"hs721070": ChoiceAnswer("72107080", 0.95)})
        with patch.dict(os.environ, {"JEV_NARROWING_ENABLED": "1"}):
            self.assertEqual(await self._advisor(jev, rows)._jev_narrow(["721070"], _request()), {})
        self.assertEqual(jev.calls, [])

    async def test_a_jev_failure_never_breaks_classification(self):
        jev = _FakeJev(error=JevError("Jev 500 döndürdü."))
        with patch.dict(os.environ, {"JEV_NARROWING_ENABLED": "1"}):
            self.assertEqual(await self._advisor(jev)._jev_narrow(["721070"], _request()), {})

    async def test_an_unconfigured_client_is_never_called(self):
        jev = _FakeJev({"hs721070": ChoiceAnswer("72107080", 0.95)}, configured=False)
        with patch.dict(os.environ, {"JEV_NARROWING_ENABLED": "1"}):
            self.assertEqual(await self._advisor(jev)._jev_narrow(["721070"], _request()), {})
        self.assertEqual(jev.calls, [])

    def test_the_state_carries_confirmed_attributes_not_photo_guesses(self):
        state = CustomsAdvisor._jev_state(
            _request(inferred_features="Fotoğraftan tahmin: paslanmaz olabilir")
        )
        self.assertIn("Plastikle kaplanmış", state)
        self.assertNotIn("paslanmaz", state, "fotoğraf tahmini kesin evsaf sayılmaz")

    async def test_end_to_end_the_classifier_returns_the_narrowed_code_with_a_provenance_note(self):
        model_result = {
            "candidates": [{
                "code": "721070",
                "explanation": "Kaplanmış yassı çelik.",
                "confidence": "medium",
                "decisive_missing_information": [],
            }],
            "missing_information": [],
            "summary": "Tek pozisyon.",
        }
        jev = _FakeJev({"hs721070": ChoiceAnswer("72107080", 0.88)})
        advisor = self._advisor(jev)
        with patch.dict(
            os.environ, {"OPENROUTER_API_KEY": "test-key", "JEV_NARROWING_ENABLED": "1"}
        ), patch(
            "customs_advisor._openrouter_chat",
            new=AsyncMock(return_value=(json.dumps(model_result), "google/gemini-test")),
        ):
            result = await advisor.classify_product(_request())
        self.assertEqual([item.code for item in result.candidates], ["72107080"])
        self.assertIn("Jev", result.candidates[0].explanation)
        self.assertIn("%88", result.candidates[0].explanation)

    async def test_end_to_end_with_narrowing_off_the_code_stays_at_six_digits(self):
        model_result = {
            "candidates": [{
                "code": "721070",
                "explanation": "Kaplanmış yassı çelik.",
                "confidence": "medium",
                "decisive_missing_information": [],
            }],
            "missing_information": [],
            "summary": "Tek pozisyon.",
        }
        jev = _FakeJev({"hs721070": ChoiceAnswer("72107080", 0.88)})
        advisor = self._advisor(jev)
        with patch.dict(
            os.environ, {"OPENROUTER_API_KEY": "test-key", "JEV_NARROWING_ENABLED": ""}
        ), patch(
            "customs_advisor._openrouter_chat",
            new=AsyncMock(return_value=(json.dumps(model_result), "google/gemini-test")),
        ):
            result = await advisor.classify_product(_request())
        self.assertEqual([item.code for item in result.candidates], ["721070"])
        self.assertEqual(jev.calls, [])


if __name__ == "__main__":
    unittest.main()
