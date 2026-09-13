from __future__ import annotations

import asyncio
import time
import base64
import io
import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from PIL import Image

from tariff_engine import TariffLookupResult
import customs_advisor
from customs_advisor import (
    CandidateGtip,
    ClassificationAnswer,
    CustomsAdvisor,
    CustomsInquiry,
    CustomsModelResult,
    EvidenceSource,
    Finding,
    OfficialSourceRegistry,
    ProductAttributeAnalysis,
    ProductClassificationRequest,
    TaxFinding,
    _deterministic_cost,
    decode_image_data_url,
    _evidence_prompt,
    _expert_review_packet,
    _missing_information,
    _openrouter_message_text,
    _openrouter_headers,
    _openrouter_error_detail,
    _openrouter_models,
    _openrouter_payload,
    _llm_api_key_value,
    _llm_base_url,
    _llm_provider,
    _model_payload,
    _openrouter_chat,
    _strip_json_fences,
    _parse_json_object,
    _sanitize_model_result,
    _strict_json_schema,
    validate_image,
)


class CustomsAdvisorSafetyTests(unittest.TestCase):
    def test_user_answers_and_textile_context_are_preserved_for_classification(self) -> None:
        answer = ClassificationAnswer(
            question="Kumaşın net elyaf kompozisyonu nedir?",
            answer="%60 pamuk, %40 polyester",
        )
        request = ProductClassificationRequest(
            product_description="Örme kumaştan iki parçalı çocuk giyim takımı",
            target_user="Kız çocuk, 8-12 yaş",
            declared_product_type="Pijama takımı",
            classification_answers=[answer],
        )
        payload = json.loads(request.model_dump_json())
        self.assertEqual(payload["target_user"], "Kız çocuk, 8-12 yaş")
        self.assertEqual(payload["declared_product_type"], "Pijama takımı")
        self.assertEqual(payload["classification_answers"][0]["answer"], "%60 pamuk, %40 polyester")

    def test_gtip_is_normalised_but_not_invented(self) -> None:
        inquiry = CustomsInquiry(
            question="Bu ürünün ithalat koşulları nedir?",
            candidate_gtip="6104.63.00.00.00",
            tariff_selection_confirmed=True,
        )
        self.assertEqual(inquiry.candidate_gtip, "610463000000")
        self.assertIn("Ürünün teknik ve ticari tanımı", _missing_information(inquiry))

    def test_invalid_gtip_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            CustomsInquiry(question="Bu ürün nedir?", candidate_gtip="12345")

    def test_unconfirmed_tariff_code_is_rejected_server_side(self) -> None:
        with self.assertRaises(ValueError):
            CustomsInquiry(question="Bu ürün nedir?", candidate_gtip="610463")

    def test_exact_confirmation_requires_twelve_digits(self) -> None:
        with self.assertRaises(ValueError):
            CustomsInquiry(
                question="Bu ürün nedir?",
                candidate_gtip="610463",
                tariff_selection_confirmed=True,
                exact_gtip_confirmed=True,
            )

    def test_classification_model_ids_are_bounded_and_sanitised(self) -> None:
        inquiry = CustomsInquiry(
            question="Bu ürün nedir?",
            classification_models=[" google/gemini-test ", "google/gemini-test", "z-ai/glm-test"],
        )
        self.assertEqual(inquiry.classification_models, ["google/gemini-test", "z-ai/glm-test"])
        with self.assertRaises(ValueError):
            CustomsInquiry(question="Bu ürün nedir?", classification_models=["https://example.test/model?secret=x"])

    def test_evidence_prompt_and_expert_packet_keep_hash_chain(self) -> None:
        digest = "a" * 64
        inquiry = CustomsInquiry(
            question="Bu ürünün yükümlülükleri nedir?",
            product_description="Porselen kahve fincanı takımı",
            candidate_gtip="691110000000",
            tariff_selection_confirmed=True,
            exact_gtip_confirmed=True,
            classification_verification_status="dual_agreement",
            classification_confidence_score=90,
            classification_models=["google/gemini-test", "z-ai/glm-test"],
        )
        pack = SimpleNamespace(
            inquiry=inquiry,
            missing_information=[],
            deterministic_cost={"status": "rates_missing"},
            tariff_lookup=SimpleNamespace(unresolved_measure_types=["anti_dumping"]),
            control_lookup=SimpleNamespace(matches=[]),
            sources=[EvidenceSource(
                id="tariff_customs_duty_test_1",
                title="İthalat Rejimi",
                authority="T.C. Ticaret Bakanlığı",
                url="https://ticaret.gov.tr/test",
                excerpt="GTİP ve oran kanıtı",
                retrieved_at="2026-08-31T00:00:00+03:00",
                sha256=digest,
            )],
            as_of="2026-08-31T00:00:00+03:00",
            legal_notice="Ön değerlendirmedir.",
        )
        prompt = _evidence_prompt(pack)
        packet = _expert_review_packet(pack)
        self.assertIn("RESMÎ KANIT PAKETİ", prompt)
        self.assertIn("gümrük_müşaviri", packet.review_types)
        self.assertEqual(packet.tariff_snapshot_sha256, [digest])
        self.assertTrue(packet.escalation_required)

    def test_cost_uses_only_user_supplied_rates(self) -> None:
        inquiry = CustomsInquiry(
            question="Maliyet nedir?",
            invoice_value=1000,
            freight=100,
            insurance=10,
            other_pre_import_costs=20,
            customs_duty_rate=10,
            additional_duty_rate=5,
            additional_financial_liability_rate=0,
            anti_dumping_amount=0,
            kkdf_rate=0,
            vat_rate=20,
            sct_amount=0,
            surveillance_unit_value=0,
        )
        cost = _deterministic_cost(inquiry)
        self.assertIsNotNone(cost)
        self.assertEqual(cost["customs_value_estimate"], 1110)
        self.assertEqual(cost["customs_duty"], 111)
        self.assertEqual(cost["additional_duty"], 55.5)
        self.assertEqual(cost["vat"], 259.3)
        self.assertEqual(cost["status"], "user_rates_complete")

    def test_uncited_model_claims_are_neutralised(self) -> None:
        result = CustomsModelResult(
            summary="Ön değerlendirme",
            answer_status="preliminary",
            candidate_gtips=[
                CandidateGtip(code="6104630000", explanation="Kanıtsız aday", citations=["fake"]),
                CandidateGtip(code="6104620000", explanation="Kanıtlı aday", citations=["tariff_btb"]),
            ],
            controls=[Finding(name="TAREKS", status="required", explanation="Kesin gerekir", citations=[])],
            taxes=[TaxFinding(name="İGV", status="applicable", rate="%20", explanation="Kesin", citations=["fake"])],
        )
        clean = _sanitize_model_result(result, {"tariff_btb"})
        self.assertEqual([item.code for item in clean.candidate_gtips], ["6104620000"])
        self.assertEqual(clean.controls[0].status, "unknown")
        self.assertEqual(clean.taxes[0].status, "unknown")
        self.assertIsNone(clean.taxes[0].rate)

    def test_uploaded_image_is_decoded_and_reencoded(self) -> None:
        original = io.BytesIO()
        Image.new("RGB", (400, 300), "navy").save(original, format="PNG")
        clean, media_type = validate_image(original.getvalue(), "image/png")
        self.assertEqual(media_type, "image/jpeg")
        self.assertTrue(clean.startswith(b"\xff\xd8\xff"))

    def test_non_image_upload_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            validate_image(b"not-an-image", "image/png")

    def test_decompression_bomb_and_oversized_dimensions_are_rejected(self) -> None:
        import warnings
        original = io.BytesIO()
        # 6000 x 5000 = 30,000,000 pixels (> 25 MP limit)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", Image.DecompressionBombWarning)
            Image.new("RGB", (6000, 5000), "white").save(original, format="PNG")
            with self.assertRaises(ValueError) as exc:
                validate_image(original.getvalue(), "image/png")
        self.assertIn("25 megapiksel", str(exc.exception))

    def test_vision_json_parser_accepts_fenced_object(self) -> None:
        parsed = _parse_json_object('```json\n{"product_name":"Çocuk şortu"}\n```')
        self.assertEqual(parsed["product_name"], "Çocuk şortu")

    def test_openrouter_default_chain_starts_with_gemini_then_glm(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            models = _openrouter_models("OPENROUTER_VISION_MODELS")
        self.assertEqual(
            models,
            [
                "~google/gemini-flash-latest",
                "z-ai/glm-5.3-flash",
                "~x-ai/grok-latest",
                "openai/gpt-chat-latest",
                "~anthropic/claude-opus-latest",
            ],
        )

    def test_openrouter_chain_is_configurable_and_deduplicated(self) -> None:
        with patch.dict(
            os.environ,
            {"OPENROUTER_VISION_MODELS": "google/gemini-3.7-flash, z-ai/glm-5.3-flash, google/gemini-3.7-flash"},
            clear=True,
        ):
            models = _openrouter_models("OPENROUTER_VISION_MODELS")
        self.assertEqual(models, ["google/gemini-3.7-flash", "z-ai/glm-5.3-flash"])

    def test_invalid_openrouter_model_id_is_rejected(self) -> None:
        with patch.dict(
            os.environ,
            {"OPENROUTER_VISION_MODELS": "https://untrusted.example/model"},
            clear=True,
        ):
            with self.assertRaises(ValueError):
                _openrouter_models("OPENROUTER_VISION_MODELS")

    def test_openrouter_multiblock_content_keeps_only_text(self) -> None:
        content = _openrouter_message_text(
            [
                {"type": "text", "text": "ilk"},
                {"type": "tool_call", "text": "çalıştırma"},
                {"type": "output_text", "text": "ikinci"},
            ]
        )
        self.assertEqual(content, "ilk\nikinci")

    def test_openrouter_payload_enforces_order_schema_and_privacy(self) -> None:
        models = ["~google/gemini-flash-latest", "z-ai/glm-5.3-flash"]
        payload = _openrouter_payload(
            models=models,
            messages=[{"role": "user", "content": "test"}],
            response_schema={"type": "object"},
            schema_name="test_schema",
            max_tokens=100,
        )
        self.assertEqual(payload["models"], models)
        self.assertTrue(payload["provider"]["allow_fallbacks"])
        self.assertTrue(payload["provider"]["require_parameters"])
        self.assertEqual(payload["provider"]["data_collection"], "deny")
        self.assertTrue(payload["response_format"]["json_schema"]["strict"])

    def test_openrouter_headers_are_ascii_safe(self) -> None:
        headers = _openrouter_headers("sk-or-v1-test")
        self.assertEqual(headers["X-OpenRouter-Title"], "Gumrukce")
        for value in headers.values():
            value.encode("ascii")

    def test_openrouter_error_detail_is_short_and_does_not_echo_request(self) -> None:
        response = httpx.Response(
            400,
            request=httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions"),
            json={"error": {"message": "Model bu istek biçimini desteklemiyor. " + ("x" * 400)}},
        )
        detail = _openrouter_error_detail(response)
        self.assertLessEqual(len(detail), 240)
        self.assertIn("Model bu istek biçimini desteklemiyor", detail)
        self.assertNotIn("Authorization", detail)

    def test_strict_schema_requires_all_nested_properties(self) -> None:
        schema = _strict_json_schema(CustomsModelResult.model_json_schema())
        self.assertEqual(set(schema["required"]), set(schema["properties"]))
        self.assertFalse(schema["additionalProperties"])
        for definition in schema["$defs"].values():
            self.assertEqual(set(definition["required"]), set(definition["properties"]))
            self.assertFalse(definition["additionalProperties"])

    def test_vision_result_never_exposes_model_supplied_gtip(self) -> None:
        result = ProductAttributeAnalysis.model_validate(
            {
                "provider": "openrouter",
                "model": "z-ai/glm-5.3-flash",
                "product_name": "Şort",
                "visible_origin_country": "",
                "required_user_inputs": ["Menşe ülke", "Etiket bileşimi"],
                "candidate_gtip": "610463000000",
            }
        )
        self.assertNotIn("candidate_gtip", result.model_dump())
        self.assertEqual(result.required_user_inputs, ["Menşe ülke", "Etiket bileşimi"])
        self.assertTrue(result.user_confirmation_required)

    def test_image_data_url_round_trip(self) -> None:
        buffer = io.BytesIO()
        Image.new("RGB", (120, 90), "white").save(buffer, format="JPEG")
        payload = base64.b64encode(buffer.getvalue()).decode("ascii")
        image_bytes, media_type = decode_image_data_url(f"data:image/jpeg;base64,{payload}")
        self.assertEqual(media_type, "image/jpeg")
        self.assertEqual(image_bytes, buffer.getvalue())

    def test_image_data_url_rejects_non_images_and_garbage(self) -> None:
        with self.assertRaises(ValueError):
            decode_image_data_url("data:text/plain;base64,aGVsbG8=")
        with self.assertRaises(ValueError):
            decode_image_data_url("data:image/jpeg;base64,!!!")
        with self.assertRaises(ValueError):
            decode_image_data_url("x" * 11_500_001)
        with self.assertRaises(ValueError):
            decode_image_data_url(42)


class DescribeImageTests(unittest.IsolatedAsyncioTestCase):
    async def test_describe_image_keeps_server_controlled_fields_only(self) -> None:
        buffer = io.BytesIO()
        Image.new("RGB", (120, 90), "white").save(buffer, format="JPEG")
        advisor = CustomsAdvisor()
        raw = {
            "product_name": "Porselen fincan takımı",
            "label_text": "İletişim: 0538 000 00 00",
            "candidate_gtip": "69111000",
            "confidence": "medium",
        }
        with patch(
            "customs_advisor._request_openrouter_vision_analysis",
            new=AsyncMock(return_value=(raw, "google/gemini-flash-latest")),
        ), patch("customs_advisor._openrouter_api_key", return_value="test-key"):
            result = await advisor.describe_image(buffer.getvalue(), "image/jpeg")
        dumped = result.model_dump()
        self.assertEqual(result.provider, "openrouter")
        self.assertEqual(result.model, "google/gemini-flash-latest")
        self.assertNotIn("candidate_gtip", dumped)
        self.assertTrue(result.user_confirmation_required)
        self.assertIn("GTİP değildir", result.warning)


class OfficialSourceRegistryTests(unittest.IsolatedAsyncioTestCase):
    async def test_captcha_source_is_manual_only_and_not_fetched(self) -> None:
        registry = OfficialSourceRegistry()
        try:
            result = await registry._fetch(
                {
                    "id": "tariff_search",
                    "title": "Tarife Arama Motoru",
                    "authority": "T.C. Ticaret Bakanlığı",
                    "url": "https://uygulama.gtb.gov.tr/Tara/TarifeBasitArama",
                    "access_mode": "manual_only",
                    "note": "Güvenlik sorusu nedeniyle manuel doğrulanır.",
                },
                ["şort"],
            )
        finally:
            await registry.close()
        self.assertEqual(result.access_mode, "manual_only")
        self.assertEqual(result.excerpt, "")
        self.assertIn("manuel", result.fetch_warning)


class TariffClassificationTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _candidate_result(code: str, explanation: str = "Resmî cetvelde doğrulanacak aday.") -> dict:
        return {
            "candidates": [{
                "code": code,
                "explanation": explanation,
                "confidence": "high",
                "decisive_missing_information": [],
            }],
            "missing_information": [],
            "summary": f"{code} değerlendirildi.",
        }

    async def test_candidates_are_verified_and_receive_origin_rates(self) -> None:
        class FakeTariffEngine:
            async def lookup(self, code, **kwargs):
                if code == "999999":
                    return SimpleNamespace(matched_gtip_count=0)
                rates = {"customs_duty": 12.0, "additional_duty": 39.0}
                return SimpleNamespace(
                    matched_gtip_count=3,
                    unambiguous_rates=rates,
                    ambiguous_measure_types=[],
                    rate_variants={key: [value] for key, value in rates.items()},
                )

        model_result = {
            "candidates": [
                {
                    "code": "691110",
                    "explanation": "Porselenden sofra eşyası adayı.",
                    "confidence": "medium",
                    "decisive_missing_information": ["Malzemenin porselen olup olmadığı"],
                },
                {
                    "code": "691200",
                    "explanation": "Porselen dışındaki seramik sofra eşyası adayı.",
                    "confidence": "medium",
                    "decisive_missing_information": ["Seramik türü"],
                },
                {
                    "code": "999999",
                    "explanation": "Resmî cetvelde bulunmayan uydurma kod.",
                    "confidence": "low",
                    "decisive_missing_information": [],
                },
            ],
            "missing_information": ["Kesin seramik türü"],
            "summary": "İki malzeme alternatifi var.",
        }
        advisor = CustomsAdvisor(tariff_engine=FakeTariffEngine())
        try:
            with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}), patch(
                "customs_advisor._openrouter_chat",
                new=AsyncMock(return_value=(json.dumps(model_result), "google/gemini-test")),
            ):
                result = await advisor.classify_product(
                    ProductClassificationRequest(
                        product_description="Dört parçalı seramik veya porselen kahve fincanı takımı",
                        composition="Seramik veya porselen",
                        origin_country="Çin",
                    )
                )
        finally:
            await advisor.close()
        self.assertEqual([item.code for item in result.candidates], ["691110", "691200"])
        self.assertTrue(all(item.verified_in_official_tariff for item in result.candidates))
        self.assertEqual(result.candidates[0].customs_duty_rate, 12.0)
        self.assertEqual(result.candidates[0].additional_duty_rate, 39.0)
        self.assertEqual(result.candidates[0].rate_status, "unambiguous")

    async def test_rates_wait_for_origin_country(self) -> None:
        class FakeTariffEngine:
            async def lookup(self, code, **kwargs):
                return SimpleNamespace(
                    matched_gtip_count=1,
                    unambiguous_rates={},
                    ambiguous_measure_types=[],
                    rate_variants={},
                )

        model_result = {
            "candidates": [{
                "code": "691110",
                "explanation": "Porselen fincan adayı.",
                "confidence": "low",
                "decisive_missing_information": ["Menşe ülke", "Malzeme"],
            }],
            "missing_information": ["Menşe ülke"],
            "summary": "Menşe oran için gereklidir.",
        }
        advisor = CustomsAdvisor(tariff_engine=FakeTariffEngine())
        try:
            with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}), patch(
                "customs_advisor._openrouter_chat",
                new=AsyncMock(return_value=(json.dumps(model_result), "google/gemini-test")),
            ):
                result = await advisor.classify_product(
                    ProductClassificationRequest(
                        product_description="Porselen olabilecek kahve fincanı ve tabak takımı",
                    )
                )
        finally:
            await advisor.close()
        self.assertEqual(result.candidates[0].rate_status, "origin_required")
        self.assertIsNone(result.candidates[0].customs_duty_rate)

    async def test_gemini_and_glm_are_called_independently_and_self_reported_confidence_is_ignored(self) -> None:
        class FakeTariffEngine:
            async def lookup(self, code, **kwargs):
                return SimpleNamespace(
                    matched_gtip_count=2,
                    unambiguous_rates={"customs_duty": 8.0},
                    ambiguous_measure_types=[],
                    rate_variants={"customs_duty": [8.0]},
                )

        called_chains = []

        async def fake_chat(**kwargs):
            called_chains.append(kwargs["models"])
            first = kwargs["models"][0]
            resolved = "google/gemini-test" if "gemini" in first else "z-ai/glm-test"
            return json.dumps(self._candidate_result("691110")), resolved

        advisor = CustomsAdvisor(tariff_engine=FakeTariffEngine())
        try:
            with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}), patch(
                "customs_advisor._openrouter_chat",
                new=fake_chat,
            ):
                result = await advisor.classify_product(
                    ProductClassificationRequest(product_description="Porselen kahve fincanı takımı")
                )
        finally:
            await advisor.close()
        self.assertEqual(result.verification_status, "dual_agreement")
        self.assertEqual(result.candidates[0].model_votes, 2)
        self.assertEqual(result.candidates[0].agreement_status, "exact")
        self.assertNotEqual(result.candidates[0].confidence, "high")
        self.assertIn("gemini", called_chains[0][0])
        self.assertIn("glm", called_chains[1][0])

    async def test_third_model_arbitrates_only_when_primary_codes_disagree(self) -> None:
        class FakeTariffEngine:
            async def lookup(self, code, **kwargs):
                return SimpleNamespace(
                    matched_gtip_count=1,
                    unambiguous_rates={"customs_duty": 8.0},
                    ambiguous_measure_types=[],
                    rate_variants={"customs_duty": [8.0]},
                )

        calls = []

        async def fake_chat(**kwargs):
            first = kwargs["models"][0]
            calls.append(first)
            if "gemini" in first:
                return json.dumps(self._candidate_result("691110")), "google/gemini-test"
            if "glm" in first:
                return json.dumps(self._candidate_result("691200")), "z-ai/glm-test"
            return json.dumps(self._candidate_result("691110")), "x-ai/grok-test"

        advisor = CustomsAdvisor(tariff_engine=FakeTariffEngine())
        try:
            with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}), patch(
                "customs_advisor._openrouter_chat",
                new=fake_chat,
            ):
                result = await advisor.classify_product(
                    ProductClassificationRequest(product_description="Seramik veya porselen kahve fincanı")
                )
        finally:
            await advisor.close()
        self.assertEqual(result.verification_status, "arbitrated_disagreement")
        self.assertEqual(len(calls), 3)
        self.assertEqual(result.candidates[0].code, "691110")
        self.assertEqual(result.candidates[0].model_votes, 2)

    async def test_evidence_pack_verifies_and_demotes_unverified_client_gate_flags(self) -> None:
        class FakeRegistry:
            async def gather(self, inquiry):
                return []

            async def close(self):
                pass

        class PartialTariffEngine:
            async def lookup(self, code, **kwargs):
                return TariffLookupResult(
                    status="partial",
                    gtip=code,
                    as_of="2026-09-08T00:00:00+03:00",
                    matched_gtip_count=2,
                    unambiguous_rates={"customs_duty": 8.0},
                    ambiguous_measure_types=[],
                    rate_variants={"customs_duty": [8.0]},
                    measures=[],
                )

        class NotFoundTariffEngine:
            async def lookup(self, code, **kwargs):
                return TariffLookupResult(
                    status="not_found",
                    gtip=code,
                    as_of="2026-09-08T00:00:00+03:00",
                    matched_gtip_count=0,
                    unambiguous_rates={},
                    ambiguous_measure_types=[],
                    rate_variants={},
                    measures=[],
                )

        inquiry = CustomsInquiry(
            question="Bu ürünün gümrük durumu nedir?",
            product_description="Porselen fincan",
            candidate_gtip="691110000000",
            exact_gtip_confirmed=True,
            tariff_selection_confirmed=True,
            classification_confidence_score=95,
        )

        advisor_partial = CustomsAdvisor(
            registry=FakeRegistry(),
            tariff_engine=PartialTariffEngine(),
        )
        try:
            pack_partial = await advisor_partial.evidence_pack(inquiry)
            self.assertFalse(pack_partial.inquiry.exact_gtip_confirmed)
            self.assertTrue(pack_partial.inquiry.tariff_selection_confirmed)
            self.assertEqual(pack_partial.inquiry.classification_confidence_score, 60)
        finally:
            await advisor_partial.close()

        advisor_not_found = CustomsAdvisor(
            registry=FakeRegistry(),
            tariff_engine=NotFoundTariffEngine(),
        )
        try:
            pack_not_found = await advisor_not_found.evidence_pack(inquiry)
            self.assertFalse(pack_not_found.inquiry.exact_gtip_confirmed)
            self.assertFalse(pack_not_found.inquiry.tariff_selection_confirmed)
            self.assertEqual(pack_not_found.inquiry.classification_confidence_score, 30)
        finally:
            await advisor_not_found.close()


_REAL_ASYNC_CLIENT = httpx.AsyncClient
_LLM_ENV_KEYS = (
    "ZAI_API_KEY", "OPENROUTER_API_KEY", "LLM_BASE_URL", "ZAI_REASONING_EFFORT", "ZAI_MAX_CONCURRENCY",
    "ZAI_VISION_THINKING", "LLM_FALLBACK_TO_OPENROUTER", "OPENROUTER_FALLBACK_MODELS",
    "LLM_REQUEST_TIMEOUT_SECONDS", "LLM_TOTAL_DEADLINE_SECONDS", "LLM_PRIMARY_BUDGET_SECONDS",
    "GEMINI_API_KEY", "GEMINI_MODELS", "GEMINI_REASONING_EFFORT", "LLM_PRIMARY_PROVIDER", "LLM_FALLBACK_TO_GEMINI",
    "LLM_FALLBACK_TO_ZAI",
)


def _llm_env(**values: str) -> dict[str, str]:
    """Environment with every LLM variable cleared except the given ones."""
    env = {key: value for key, value in os.environ.items() if key not in _LLM_ENV_KEYS}
    env.update(values)
    return env


def _chat_response(content: str, model: str = "glm-5.3") -> dict:
    return {
        "model": model,
        "choices": [{"message": {"content": content}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


def _gemini_response(text: str, model: str = "gemini-3.8-flash") -> dict:
    """Native generateContent reply shape (candidates + usageMetadata + modelVersion)."""
    return {
        "candidates": [{"content": {"role": "model", "parts": [{"text": text}]}, "finishReason": "STOP"}],
        "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5, "totalTokenCount": 15},
        "modelVersion": model,
    }


def _gemini_model_from_url(request: httpx.Request) -> str:
    return request.url.path.rsplit("/models/", 1)[-1].split(":", 1)[0]


def _mock_client_factory(handler):
    def factory(*args, **kwargs):
        return _REAL_ASYNC_CLIENT(transport=httpx.MockTransport(handler), timeout=kwargs.get("timeout"))

    return factory


class ZaiProviderConfigTests(unittest.TestCase):
    def test_zai_key_selects_zai_base_url_and_takes_precedence(self) -> None:
        with patch.dict(os.environ, _llm_env(ZAI_API_KEY="zai-key", OPENROUTER_API_KEY="or-key"), clear=True):
            self.assertEqual(_llm_base_url(), "https://api.z.ai/api/coding/paas/v4")
            self.assertEqual(_llm_provider(), "zai")
            self.assertEqual(_llm_api_key_value(), "zai-key")

    def test_without_zai_key_openrouter_stays_default(self) -> None:
        with patch.dict(os.environ, _llm_env(OPENROUTER_API_KEY="or-key"), clear=True):
            self.assertEqual(_llm_base_url(), "https://openrouter.ai/api/v1")
            self.assertEqual(_llm_provider(), "openrouter")
            self.assertEqual(_llm_api_key_value(), "or-key")

    def test_explicit_base_url_wins(self) -> None:
        with patch.dict(
            os.environ,
            _llm_env(ZAI_API_KEY="zai-key", LLM_BASE_URL="https://open.bigmodel.cn/api/paas/v4/"),
            clear=True,
        ):
            self.assertEqual(_llm_base_url(), "https://open.bigmodel.cn/api/paas/v4")
            self.assertEqual(_llm_provider(), "zai")

    def test_slashless_zai_model_ids_are_accepted(self) -> None:
        with patch.dict(os.environ, {"OPENROUTER_VISION_MODELS": "glm-5v-turbo, glm-4.6v, glm-5v-turbo"}):
            self.assertEqual(_openrouter_models("OPENROUTER_VISION_MODELS"), ["glm-5v-turbo", "glm-4.6v"])
        with patch.dict(os.environ, {"OPENROUTER_CUSTOMS_MODELS": "glm-5.3,glm-5.3-flash"}):
            self.assertEqual(_openrouter_models("OPENROUTER_CUSTOMS_MODELS"), ["glm-5.3", "glm-5.3-flash"])

    def test_zai_falls_back_to_glm_defaults_when_chain_is_openrouter_only(self) -> None:
        # ZAI_API_KEY var ama OPENROUTER_*_MODELS hic ayarlanmamis (Coolify'daki
        # en yaygin durum): OpenRouter varsayilanlari Z.ai'de calismaz.
        with patch.dict(os.environ, _llm_env(ZAI_API_KEY="zai-key"), clear=True):
            self.assertEqual(_openrouter_models("OPENROUTER_VISION_MODELS"), ["glm-5v-turbo", "glm-4.6v"])
            self.assertEqual(_openrouter_models("OPENROUTER_CUSTOMS_MODELS"), ["glm-5.3", "glm-5.3-flash"])
        # Eski OpenRouter listesi ayarli: yalniz Z.ai'de gecerli olanlar kalir.
        env = _llm_env(
            ZAI_API_KEY="zai-key",
            OPENROUTER_VISION_MODELS="~google/gemini-flash-latest,z-ai/glm-5.3-flash,openai/gpt-chat-latest",
            OPENROUTER_CUSTOMS_MODELS="~google/gemini-flash-latest,~anthropic/claude-opus-latest",
        )
        with patch.dict(os.environ, env, clear=True):
            # glm-5.3-flash gorsel modeli degildir; gorsel zinciri varsayilana doner.
            self.assertEqual(_openrouter_models("OPENROUTER_VISION_MODELS"), ["glm-5v-turbo", "glm-4.6v"])
            self.assertEqual(_openrouter_models("OPENROUTER_CUSTOMS_MODELS"), ["glm-5.3", "glm-5.3-flash"])
        env = _llm_env(
            ZAI_API_KEY="zai-key",
            OPENROUTER_VISION_MODELS="z-ai/glm-4.6v,~google/gemini-flash-latest",
            OPENROUTER_CUSTOMS_MODELS="z-ai/glm-5.3-flash,openai/gpt-chat-latest",
        )
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(_openrouter_models("OPENROUTER_VISION_MODELS"), ["glm-4.6v"])
            self.assertEqual(_openrouter_models("OPENROUTER_CUSTOMS_MODELS"), ["glm-5.3-flash"])
        # OpenRouter saglayicisinda liste oldugu gibi kalir.
        with patch.dict(os.environ, _llm_env(OPENROUTER_API_KEY="or-key"), clear=True):
            self.assertEqual(_openrouter_models("OPENROUTER_VISION_MODELS")[0], "~google/gemini-flash-latest")

    def test_zai_payload_uses_json_object_without_openrouter_provider(self) -> None:
        payload = _openrouter_payload(
            models=["glm-5.3"],
            messages=[{"role": "system", "content": "Sistem"}, {"role": "user", "content": "test"}],
            response_schema={"type": "object", "properties": {"summary": {"type": "string"}}},
            schema_name="test_schema",
            max_tokens=100,
            provider="zai",
        )
        self.assertNotIn("provider", payload)
        self.assertEqual(payload["response_format"], {"type": "json_object"})
        self.assertEqual(payload["max_tokens"], 4100)
        system = payload["messages"][0]["content"]
        self.assertTrue(system.startswith("Sistem"))
        self.assertIn("test_schema", system)
        self.assertIn('"summary"', system)
        self.assertEqual(payload["messages"][1], {"role": "user", "content": "test"})

    def test_reasoning_effort_only_for_glm5_text_models(self) -> None:
        base = {"models": ["x"], "messages": [], "max_tokens": 10}
        with patch.dict(os.environ, _llm_env(), clear=True):
            self.assertEqual(_model_payload(base, "glm-5.3", "zai")["reasoning_effort"], "low")
            self.assertEqual(_model_payload(base, "glm-5.3-flash", "zai")["reasoning_effort"], "low")
            self.assertNotIn("reasoning_effort", _model_payload(base, "glm-5v-turbo", "zai"))
            self.assertNotIn("reasoning_effort", _model_payload(base, "glm-4.6v", "zai"))
            self.assertNotIn("reasoning_effort", _model_payload(base, "z-ai/glm-5.3-flash", "openrouter"))
            self.assertNotIn("models", _model_payload(base, "glm-5.3", "zai"))
        with patch.dict(os.environ, _llm_env(ZAI_REASONING_EFFORT="medium"), clear=True):
            self.assertEqual(_model_payload(base, "glm-5.3", "zai")["reasoning_effort"], "medium")

    def test_zai_headers_omit_openrouter_attribution(self) -> None:
        headers = _openrouter_headers("zai-test", "zai")
        self.assertEqual(headers["Authorization"], "Bearer zai-test")
        self.assertNotIn("X-OpenRouter-Title", headers)
        self.assertNotIn("HTTP-Referer", headers)

    def test_json_fences_are_stripped(self) -> None:
        self.assertEqual(_strip_json_fences('```json\n{"a": 1}\n```'), '{"a": 1}')
        self.assertEqual(_strip_json_fences('```\n{"a": 1}\n```'), '{"a": 1}')
        self.assertEqual(_strip_json_fences('Yanıt:\n```json\n{"a": 1}\n```'), '{"a": 1}')
        self.assertEqual(_strip_json_fences(' {"a": 1} '), '{"a": 1}')


class ZaiChatTransportTests(unittest.IsolatedAsyncioTestCase):
    async def _chat(self, handler, models, env, sleep_mock=None):
        sleep_mock = sleep_mock or AsyncMock()
        with patch.dict(os.environ, env, clear=True), patch(
            "customs_advisor.httpx.AsyncClient", new=_mock_client_factory(handler)
        ), patch("customs_advisor._retry_sleep", new=sleep_mock):
            return await _openrouter_chat(
                api_key="test-key",
                models=models,
                messages=[{"role": "system", "content": "Sistem"}, {"role": "user", "content": "Ürün"}],
                response_schema={"type": "object", "properties": {"a": {"type": "integer"}}},
                schema_name="test_schema",
                max_tokens=100,
            )

    async def test_zai_request_body_and_fence_stripping(self) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json=_chat_response('```json\n{"a": 1}\n```'))

        text, model = await self._chat(handler, ["glm-5.3"], _llm_env(ZAI_API_KEY="zai-key"))
        self.assertEqual(text, '{"a": 1}')
        self.assertEqual(model, "glm-5.3")
        request = seen[0]
        self.assertEqual(str(request.url), "https://api.z.ai/api/coding/paas/v4/chat/completions")
        self.assertNotIn("x-openrouter-title", request.headers)
        body = json.loads(request.content)
        self.assertNotIn("provider", body)
        self.assertEqual(body["response_format"], {"type": "json_object"})
        self.assertEqual(body["reasoning_effort"], "low")
        self.assertEqual(body["model"], "glm-5.3")
        self.assertEqual(body["max_tokens"], 4100)

    async def test_zai_vision_request_keeps_image_url_without_reasoning_effort(self) -> None:
        seen: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(json.loads(request.content))
            return httpx.Response(200, json=_chat_response('{"a": 1}', "glm-5v-turbo"))

        env = _llm_env(ZAI_API_KEY="zai-key")
        with patch.dict(os.environ, env, clear=True), patch(
            "customs_advisor.httpx.AsyncClient", new=_mock_client_factory(handler)
        ):
            await _openrouter_chat(
                api_key="test-key",
                models=["glm-5v-turbo"],
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "Evsaf"},
                            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AAAA"}},
                        ],
                    }
                ],
                response_schema={"type": "object", "properties": {"a": {"type": "integer"}}},
                schema_name="product_attributes",
                max_tokens=100,
            )
        body = seen[0]
        self.assertNotIn("reasoning_effort", body)
        self.assertEqual(body["messages"][0]["role"], "system")
        image_part = body["messages"][1]["content"][1]
        self.assertEqual(image_part["type"], "image_url")
        self.assertEqual(image_part["image_url"]["url"], "data:image/jpeg;base64,AAAA")

    async def test_zai_1302_rate_limit_is_retried_on_same_model(self) -> None:
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(json.loads(request.content)["model"])
            if len(calls) == 1:
                return httpx.Response(429, json={"error": {"code": "1302", "message": "concurrency"}})
            return httpx.Response(200, json=_chat_response('{"a": 1}'))

        sleep_mock = AsyncMock()
        text, model = await self._chat(
            handler, ["glm-5.3", "glm-5.3-flash"], _llm_env(ZAI_API_KEY="zai-key"), sleep_mock
        )
        self.assertEqual(text, '{"a": 1}')
        self.assertEqual(calls, ["glm-5.3", "glm-5.3"])
        sleep_mock.assert_awaited_once_with(3.0)

    async def test_zai_rate_limit_retries_at_most_twice_then_next_model(self) -> None:
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            model = json.loads(request.content)["model"]
            calls.append(model)
            if model == "glm-5.3":
                return httpx.Response(429, json={"error": {"code": "1302"}})
            return httpx.Response(200, json=_chat_response('{"a": 2}', model))

        sleep_mock = AsyncMock()
        text, model = await self._chat(
            handler, ["glm-5.3", "glm-5.3-flash"], _llm_env(ZAI_API_KEY="zai-key"), sleep_mock
        )
        self.assertEqual((text, model), ('{"a": 2}', "glm-5.3-flash"))
        self.assertEqual(calls, ["glm-5.3", "glm-5.3", "glm-5.3", "glm-5.3-flash"])
        self.assertEqual([call.args[0] for call in sleep_mock.await_args_list], [3.0, 6.0])

    async def test_zai_1113_moves_to_next_model_without_retry(self) -> None:
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            model = json.loads(request.content)["model"]
            calls.append(model)
            if model == "glm-5.3":
                return httpx.Response(429, json={"error": {"code": "1113", "message": "balance"}})
            return httpx.Response(200, json=_chat_response('{"a": 3}', model))

        sleep_mock = AsyncMock()
        text, model = await self._chat(
            handler, ["glm-5.3", "glm-5.3-flash"], _llm_env(ZAI_API_KEY="zai-key"), sleep_mock
        )
        self.assertEqual(model, "glm-5.3-flash")
        self.assertEqual(calls, ["glm-5.3", "glm-5.3-flash"])
        sleep_mock.assert_not_awaited()

    async def test_zai_concurrency_is_capped(self) -> None:
        state = {"active": 0, "peak": 0}

        async def handler(request: httpx.Request) -> httpx.Response:
            state["active"] += 1
            state["peak"] = max(state["peak"], state["active"])
            await asyncio.sleep(0.01)
            state["active"] -= 1
            return httpx.Response(200, json=_chat_response('{"a": 1}'))

        env = _llm_env(ZAI_API_KEY="zai-key", ZAI_MAX_CONCURRENCY="2")  # gitleaks:allow
        with patch.dict(os.environ, env, clear=True), patch(
            "customs_advisor.httpx.AsyncClient", new=_mock_client_factory(handler)
        ):
            await asyncio.gather(
                *[
                    _openrouter_chat(
                        api_key="k",
                        models=["glm-5.3"],
                        messages=[{"role": "user", "content": "x"}],
                        response_schema={"type": "object"},
                        schema_name="s",
                        max_tokens=10,
                    )
                    for _ in range(5)
                ]
            )
        self.assertEqual(state["peak"], 2)

    async def test_without_zai_key_openrouter_body_is_unchanged(self) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            if len(seen) == 1:
                return httpx.Response(429, json={"error": {"code": 429, "message": "rate"}})
            return httpx.Response(200, json=_chat_response('```json\n{"a": 1}\n```', "google/gemini"))

        sleep_mock = AsyncMock()
        text, _ = await self._chat(
            handler,
            ["~google/gemini-flash-latest", "z-ai/glm-5.3-flash"],
            _llm_env(OPENROUTER_API_KEY="or-key"),
            sleep_mock,
        )
        # OpenRouter path: no retry, next model, content returned verbatim.
        sleep_mock.assert_not_awaited()
        self.assertEqual(text, '```json\n{"a": 1}\n```')
        self.assertEqual(str(seen[0].url), "https://openrouter.ai/api/v1/chat/completions")
        self.assertEqual(seen[0].headers["x-openrouter-title"], "Gumrukce")
        body = json.loads(seen[1].content)
        self.assertEqual(body["model"], "z-ai/glm-5.3-flash")
        self.assertEqual(body["provider"]["data_collection"], "deny")
        self.assertTrue(body["response_format"]["json_schema"]["strict"])
        self.assertEqual(body["max_tokens"], 100)
        self.assertNotIn("reasoning_effort", body)
        self.assertNotIn("models", body)


class LlmResilienceTests(unittest.IsolatedAsyncioTestCase):
    """Timeouts, queue waits and the automatic OpenRouter fallback."""

    async def _chat(self, handler, models, env, **overrides):
        overrides = overrides or {"_LLM_MIN_FALLBACK_SECONDS": customs_advisor._LLM_MIN_FALLBACK_SECONDS}
        with patch.dict(os.environ, env, clear=True), patch(
            "customs_advisor.httpx.AsyncClient", new=_mock_client_factory(handler)
        ), patch("customs_advisor._retry_sleep", new=AsyncMock()), patch.multiple(
            customs_advisor, **overrides
        ):
            return await _openrouter_chat(
                api_key="zai-key",
                models=models,
                messages=[{"role": "system", "content": "Sistem"}, {"role": "user", "content": "Ürün"}],
                response_schema={"type": "object", "properties": {"a": {"type": "integer"}}},
                schema_name="test_schema",
                max_tokens=100,
            )

    async def test_recent_events_record_success_and_exhausted_chain(self) -> None:
        customs_advisor._LLM_RECENT_EVENTS.clear()

        def ok_handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_chat_response('{"a": 1}', "glm-5.3"))

        def failing_handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, json={"error": {"message": "upstream down"}})

        env = _llm_env(ZAI_API_KEY="zai-key")  # gitleaks:allow
        await self._chat(ok_handler, ["glm-5.3"], env)
        with self.assertRaises(RuntimeError):
            await self._chat(failing_handler, ["glm-5.3"], env)
        events = customs_advisor.recent_llm_events()
        self.assertEqual([event["ok"] for event in events], [False, True])
        failed, succeeded = events
        self.assertEqual(failed["operation"], "test_schema")
        self.assertEqual(failed["provider"], "zai")
        self.assertIn("HTTP 503", failed["detail"])
        self.assertIn("upstream down", failed["detail"])
        self.assertNotIn("zai-key", json.dumps(events))
        self.assertEqual(succeeded["model"], "glm-5.3")
        self.assertTrue(succeeded["at"].endswith("+00:00"))
        customs_advisor._LLM_RECENT_EVENTS.clear()

    def test_vision_models_disable_thinking_by_default(self) -> None:
        base = {"models": ["x"], "messages": [], "max_tokens": 10}
        with patch.dict(os.environ, _llm_env(ZAI_API_KEY="zai-key"), clear=True):
            self.assertEqual(_model_payload(base, "glm-5v-turbo", "zai")["thinking"], {"type": "disabled"})
            self.assertEqual(_model_payload(base, "glm-4.6v", "zai")["thinking"], {"type": "disabled"})
            self.assertNotIn("thinking", _model_payload(base, "glm-5.3", "zai"))
            self.assertNotIn("thinking", _model_payload(base, "google/gemini-flash-latest", "openrouter"))
        with patch.dict(os.environ, _llm_env(ZAI_API_KEY="zai-key", ZAI_VISION_THINKING="enabled"), clear=True):
            self.assertEqual(_model_payload(base, "glm-5v-turbo", "zai")["thinking"], {"type": "enabled"})

    def test_fallback_models_are_openrouter_ids_without_zai(self) -> None:
        with patch.dict(os.environ, _llm_env(), clear=True):
            models = customs_advisor._openrouter_fallback_models()
        self.assertEqual(models[0], "~google/gemini-flash-latest")
        self.assertTrue(all("/" in m for m in models))
        self.assertFalse(any(m.lstrip("~").startswith("z-ai/") for m in models))
        with patch.dict(os.environ, _llm_env(OPENROUTER_FALLBACK_MODELS="openai/gpt-chat-latest, glm-5.3"), clear=True):
            self.assertEqual(customs_advisor._openrouter_fallback_models(), ["openai/gpt-chat-latest"])

    async def test_zai_failure_falls_back_to_openrouter_gemini(self) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            if request.url.host == "api.z.ai":
                return httpx.Response(500, json={"error": {"message": "upstream"}})
            return httpx.Response(200, json=_chat_response('{"a": 2}', "google/gemini-flash-latest"))

        text, model = await self._chat(
            handler, ["glm-5v-turbo", "glm-4.6v"],
            _llm_env(ZAI_API_KEY="zai-key", OPENROUTER_API_KEY="or-key", LLM_FALLBACK_TO_OPENROUTER="1"),  # gitleaks:allow
        )
        self.assertEqual(text, '{"a": 2}')
        self.assertEqual(model, "google/gemini-flash-latest")
        hosts = [request.url.host for request in seen]
        self.assertEqual(hosts[:2], ["api.z.ai", "api.z.ai"])
        self.assertEqual(hosts[2], "openrouter.ai")
        fallback = seen[2]
        self.assertEqual(fallback.headers["authorization"], "Bearer or-key")
        body = json.loads(fallback.content)
        self.assertEqual(body["model"], "~google/gemini-flash-latest")
        self.assertEqual(body["response_format"]["type"], "json_schema")
        self.assertNotIn("thinking", body)

    async def test_no_fallback_without_openrouter_key_and_error_hides_provider_names(self) -> None:
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.url.host)
            return httpx.Response(500, json={"error": {"message": "upstream"}})

        with self.assertRaises(RuntimeError) as ctx:
            await self._chat(handler, ["glm-5v-turbo"], _llm_env(ZAI_API_KEY="zai-key"))
        self.assertEqual(seen, ["api.z.ai"])
        message = str(ctx.exception)
        self.assertIn("Yapay zekâ analizi şu anda yanıt vermedi", message)
        for banned in ("Z.ai", "OpenRouter", "glm", "upstream"):
            self.assertNotIn(banned, message)

    async def test_fallback_can_be_switched_off(self) -> None:
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.url.host)
            return httpx.Response(500, json={"error": {"message": "upstream"}})

        with self.assertRaises(RuntimeError):
            await self._chat(
                handler, ["glm-5v-turbo"],
                _llm_env(ZAI_API_KEY="zai-key", OPENROUTER_API_KEY="or-key", LLM_FALLBACK_TO_OPENROUTER="0"),
            )
        self.assertEqual(seen, ["api.z.ai"])

    async def test_slow_primary_is_cut_by_deadline_and_fallback_answers(self) -> None:
        seen: list[str] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.url.host)
            if request.url.host == "api.z.ai":
                await asyncio.sleep(30)
            return httpx.Response(200, json=_chat_response('{"a": 3}', "google/gemini-flash-latest"))

        started = time.monotonic()
        text, model = await self._chat(
            handler, ["glm-5v-turbo", "glm-4.6v"],
            _llm_env(ZAI_API_KEY="zai-key", OPENROUTER_API_KEY="or-key", LLM_FALLBACK_TO_OPENROUTER="1"),  # gitleaks:allow
            _llm_total_deadline=lambda: 2.0,
            _llm_primary_budget=lambda total: 0.2,
            _LLM_MIN_FALLBACK_SECONDS=0.1,
        )
        self.assertLess(time.monotonic() - started, 5.0)
        self.assertEqual((text, model), ('{"a": 3}', "google/gemini-flash-latest"))
        # The deadline cuts the chain after the first slow model; the second Z.ai model is not tried.
        self.assertEqual(seen, ["api.z.ai", "openrouter.ai"])

    async def test_slow_provider_without_fallback_raises_within_deadline(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            await asyncio.sleep(30)
            return httpx.Response(200, json=_chat_response('{"a": 1}'))

        started = time.monotonic()
        with self.assertRaises(RuntimeError):
            await self._chat(handler, ["glm-5v-turbo"], _llm_env(ZAI_API_KEY="zai-key"), _llm_total_deadline=lambda: 0.3)
        self.assertLess(time.monotonic() - started, 5.0)

    async def test_full_queue_does_not_block_forever(self) -> None:
        called: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            called.append(request.url.host)
            return httpx.Response(200, json=_chat_response('{"a": 1}'))

        with patch.dict(os.environ, _llm_env(ZAI_API_KEY="zai-key", ZAI_MAX_CONCURRENCY="1"), clear=True):  # gitleaks:allow
            customs_advisor._LLM_SEMAPHORE = None
            semaphore = customs_advisor._llm_semaphore()
            await semaphore.acquire()
            try:
                started = time.monotonic()
                with self.assertRaises(RuntimeError):
                    await self._chat(handler, ["glm-5.3"], _llm_env(ZAI_API_KEY="zai-key", ZAI_MAX_CONCURRENCY="1"), _LLM_QUEUE_WAIT_SECONDS=0.1)  # gitleaks:allow
                self.assertLess(time.monotonic() - started, 5.0)
                self.assertEqual(called, [])
            finally:
                semaphore.release()
                customs_advisor._LLM_SEMAPHORE = None

    def test_request_timeout_and_deadline_are_bounded(self) -> None:
        with patch.dict(os.environ, _llm_env(LLM_REQUEST_TIMEOUT_SECONDS="5", LLM_TOTAL_DEADLINE_SECONDS="9999"), clear=True):
            timeout = customs_advisor._llm_request_timeout()
            self.assertEqual(timeout.read, 10.0)
            self.assertEqual(timeout.connect, 10.0)
            self.assertEqual(customs_advisor._llm_total_deadline(), 600.0)
        with patch.dict(os.environ, _llm_env(LLM_REQUEST_TIMEOUT_SECONDS="abc"), clear=True):
            self.assertEqual(customs_advisor._llm_request_timeout().read, 75.0)
            self.assertEqual(customs_advisor._llm_request_timeout().connect, 15.0)


class GeminiProviderTests(unittest.IsolatedAsyncioTestCase):
    """Direct Google Gemini (native generateContent API) as primary or fallback."""

    async def _chat(self, handler, models, env):
        with patch.dict(os.environ, env, clear=True), patch(
            "customs_advisor.httpx.AsyncClient", new=_mock_client_factory(handler)
        ), patch("customs_advisor._retry_sleep", new=AsyncMock()):
            return await _openrouter_chat(
                api_key=customs_advisor._llm_api_key_value(),
                models=models,
                messages=[{"role": "system", "content": "Sistem"}, {"role": "user", "content": "Ürün"}],
                response_schema={"type": "object", "properties": {"a": {"type": "integer"}}},
                schema_name="test_schema",
                max_tokens=100,
            )

    def test_gemini_is_primary_when_its_key_exists_and_override_can_pick_zai(self) -> None:
        with patch.dict(os.environ, _llm_env(GEMINI_API_KEY="gem-key"), clear=True):
            self.assertEqual(_llm_provider(), "gemini")
            self.assertEqual(_llm_base_url(), "https://generativelanguage.googleapis.com/v1beta/openai")
            self.assertEqual(_llm_api_key_value(), "gem-key")
            self.assertEqual(_openrouter_models("OPENROUTER_VISION_MODELS"), ["gemini-3.8-flash", "gemini-flash-latest"])
            self.assertEqual(_openrouter_models("OPENROUTER_CUSTOMS_MODELS"), ["gemini-3.8-flash", "gemini-flash-latest"])
        with patch.dict(os.environ, _llm_env(ZAI_API_KEY="zai-key", GEMINI_API_KEY="gem-key"), clear=True):
            self.assertEqual(_llm_provider(), "gemini", "Gemini key wins over Z.ai without an override")
            self.assertEqual(_llm_api_key_value(), "gem-key")
            self.assertEqual(_openrouter_models("OPENROUTER_VISION_MODELS")[0], "gemini-3.8-flash")
        with patch.dict(os.environ, _llm_env(ZAI_API_KEY="zai-key", GEMINI_API_KEY="gem-key", LLM_PRIMARY_PROVIDER="zai"), clear=True):
            self.assertEqual(_llm_provider(), "zai")
            self.assertEqual(_llm_api_key_value(), "zai-key")
        with patch.dict(os.environ, _llm_env(ZAI_API_KEY="zai-key", LLM_PRIMARY_PROVIDER="gemini"), clear=True):
            self.assertEqual(_llm_provider(), "zai", "override without a Gemini key is ignored")
        with patch.dict(os.environ, _llm_env(ZAI_API_KEY="zai-key", GEMINI_API_KEY="gem-key", OPENROUTER_API_KEY="or-key"), clear=True):
            fallbacks = [name for name, _, _, _ in customs_advisor._fallback_providers("gemini", vision=True)]
            self.assertEqual(fallbacks, ["zai"], "OpenRouter stays out of the chain unless explicitly enabled")

    def test_gemini_model_list_accepts_openrouter_style_google_ids(self) -> None:
        with patch.dict(os.environ, _llm_env(GEMINI_MODELS="~google/gemini-3.8-flash, z-ai/glm-5.3, gemini-flash-latest"), clear=True):
            self.assertEqual(customs_advisor._gemini_models(), ["gemini-3.8-flash", "gemini-flash-latest"])
        with patch.dict(os.environ, _llm_env(GEMINI_MODELS="z-ai/glm-5.3"), clear=True):
            self.assertEqual(customs_advisor._gemini_models(), ["gemini-3.8-flash", "gemini-flash-latest"])

    async def test_gemini_primary_request_shape(self) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json=_gemini_response('```json\n{"a": 5}\n```', "gemini-3.8-flash-001"))

        text, model = await self._chat(handler, ["gemini-3.8-flash"], _llm_env(GEMINI_API_KEY="gem-key"))
        self.assertEqual((text, model), ('{"a": 5}', "gemini-3.8-flash-001"))
        request = seen[0]
        # Same request shape as productanaliz (proven in production): native
        # generateContent, key header, systemInstruction, no OpenAI-only knobs.
        self.assertEqual(
            str(request.url),
            "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.8-flash:generateContent",
        )
        self.assertEqual(request.headers["x-goog-api-key"], "gem-key")
        self.assertEqual(request.headers["user-agent"], "aistudio-build")
        self.assertNotIn("authorization", request.headers)
        body = json.loads(request.content)
        self.assertEqual(set(body), {"contents", "systemInstruction"})
        system_text = body["systemInstruction"]["parts"][0]["text"]
        self.assertTrue(system_text.startswith("Sistem"))
        self.assertIn("test_schema", system_text)
        self.assertEqual(body["contents"], [{"role": "user", "parts": [{"text": "Ürün"}]}])
        for key in ("model", "response_format", "reasoning_effort", "generationConfig", "thinking"):
            self.assertNotIn(key, body)

    async def test_zai_failure_prefers_gemini_over_openrouter(self) -> None:
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.url.host)
            if request.url.host == "api.z.ai":
                return httpx.Response(500, json={"error": {"message": "upstream"}})
            return httpx.Response(200, json=_gemini_response('{"a": 7}', "gemini-3.8-flash"))

        text, model = await self._chat(
            handler, ["glm-5v-turbo"],
            _llm_env(ZAI_API_KEY="zai-key", GEMINI_API_KEY="gem-key", OPENROUTER_API_KEY="or-key", LLM_PRIMARY_PROVIDER="zai", LLM_FALLBACK_TO_OPENROUTER="1"),  # gitleaks:allow
        )
        self.assertEqual((text, model), ('{"a": 7}', "gemini-3.8-flash"))
        self.assertEqual(seen, ["api.z.ai", "generativelanguage.googleapis.com"])

    async def test_gemini_failure_continues_to_openrouter(self) -> None:
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.url.host)
            if request.url.host == "openrouter.ai":
                return httpx.Response(200, json=_chat_response('{"a": 8}', "google/gemini-flash-latest"))
            return httpx.Response(503, json={"error": {"message": "down"}})

        text, model = await self._chat(
            handler, ["glm-5v-turbo"],
            _llm_env(ZAI_API_KEY="zai-key", GEMINI_API_KEY="gem-key", OPENROUTER_API_KEY="or-key", LLM_PRIMARY_PROVIDER="zai", LLM_FALLBACK_TO_OPENROUTER="1"),  # gitleaks:allow
        )
        self.assertEqual((text, model), ('{"a": 8}', "google/gemini-flash-latest"))
        self.assertEqual(seen[0], "api.z.ai")
        # Each Gemini model: one 503 plus a single retry, then the next model, then OpenRouter.
        self.assertEqual(seen[1:5], ["generativelanguage.googleapis.com"] * 4)
        self.assertEqual(seen[5], "openrouter.ai")
        self.assertEqual(len(seen), 6)

    async def test_gemini_retries_transient_errors_and_skips_to_next_model_on_404(self) -> None:
        calls: list[str] = []
        sleeps = AsyncMock()

        def handler(request: httpx.Request) -> httpx.Response:
            model = _gemini_model_from_url(request)
            calls.append(model)
            if model == "gemini-3.8-flash":
                if calls.count(model) == 1:
                    return httpx.Response(429, json={"error": {"message": "quota", "status": "RESOURCE_EXHAUSTED"}})
                return httpx.Response(404, json={"error": {"message": "model not found", "status": "NOT_FOUND"}})
            return httpx.Response(200, json=_gemini_response('{"a": 9}', "gemini-flash-latest"))

        env = _llm_env(GEMINI_API_KEY="gem-key")
        with patch.dict(os.environ, env, clear=True), patch(
            "customs_advisor.httpx.AsyncClient", new=_mock_client_factory(handler)
        ), patch("customs_advisor._retry_sleep", new=sleeps):
            text, model = await _openrouter_chat(
                api_key="gem-key",
                models=["gemini-3.8-flash", "gemini-flash-latest"],
                messages=[{"role": "system", "content": "Sistem"}, {"role": "user", "content": "Ürün"}],
                response_schema={"type": "object", "properties": {"a": {"type": "integer"}}},
                schema_name="test_schema",
                max_tokens=100,
            )
        self.assertEqual((text, model), ('{"a": 9}', "gemini-flash-latest"))
        # 429 -> one retry (1.5 s) -> 404 -> straight to the next model, no further retry.
        self.assertEqual(calls, ["gemini-3.8-flash", "gemini-3.8-flash", "gemini-flash-latest"])
        sleeps.assert_awaited_once_with(1.5)

    async def test_gemini_blocked_or_empty_reply_moves_to_next_model(self) -> None:
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            model = _gemini_model_from_url(request)
            calls.append(model)
            if model == "gemini-3.8-flash":
                return httpx.Response(200, json={"promptFeedback": {"blockReason": "SAFETY"}, "candidates": []})
            return httpx.Response(200, json=_gemini_response('{"a": 3}', "gemini-flash-latest"))

        text, model = await self._chat(handler, ["gemini-3.8-flash", "gemini-flash-latest"], _llm_env(GEMINI_API_KEY="gem-key"))
        self.assertEqual((text, model), ('{"a": 3}', "gemini-flash-latest"))
        self.assertEqual(calls, ["gemini-3.8-flash", "gemini-flash-latest"])

    async def test_gemini_primary_falls_back_to_zai_vision_chain_for_images(self) -> None:
        seen: list[tuple[str, str]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            if request.url.host == "api.z.ai":
                seen.append((request.url.host, body["model"]))
                self.assertEqual(body["thinking"], {"type": "disabled"})
                return httpx.Response(200, json=_chat_response('{"a": 4}', "glm-5v-turbo"))
            seen.append((request.url.host, _gemini_model_from_url(request)))
            parts = body["contents"][0]["parts"]
            self.assertEqual(parts[0], {"text": "Evsaf"})
            self.assertEqual(parts[1], {"inlineData": {"mimeType": "image/png", "data": "AAAA"}})
            return httpx.Response(503, json={"error": {"message": "down"}})

        env = _llm_env(ZAI_API_KEY="zai-key", GEMINI_API_KEY="gem-key", LLM_PRIMARY_PROVIDER="gemini")
        with patch.dict(os.environ, env, clear=True), patch(
            "customs_advisor.httpx.AsyncClient", new=_mock_client_factory(handler)
        ), patch("customs_advisor._retry_sleep", new=AsyncMock()):
            text, model = await _openrouter_chat(
                api_key=customs_advisor._llm_api_key_value(),
                models=_openrouter_models("OPENROUTER_VISION_MODELS"),
                messages=[
                    {"role": "system", "content": "Sistem"},
                    {"role": "user", "content": [{"type": "text", "text": "Evsaf"}, {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]},
                ],
                response_schema={"type": "object", "properties": {"a": {"type": "integer"}}},
                schema_name="test_schema",
                max_tokens=100,
            )
        self.assertEqual((text, model), ('{"a": 4}', "glm-5v-turbo"))
        gemini_calls = [model for host, model in seen if host == "generativelanguage.googleapis.com"]
        self.assertEqual(gemini_calls, ["gemini-3.8-flash", "gemini-3.8-flash", "gemini-flash-latest", "gemini-flash-latest"])
        self.assertEqual(seen[-1], ("api.z.ai", "glm-5v-turbo"))

    async def test_gemini_primary_text_fallback_uses_zai_text_models(self) -> None:
        seen: list[tuple[str, str]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "api.z.ai":
                seen.append((request.url.host, json.loads(request.content)["model"]))
                return httpx.Response(200, json=_chat_response('{"a": 6}', "glm-5.3"))
            seen.append((request.url.host, _gemini_model_from_url(request)))
            return httpx.Response(503, json={"error": {"message": "down"}})

        text, model = await self._chat(
            handler, ["gemini-3.8-flash"],
            _llm_env(ZAI_API_KEY="zai-key", GEMINI_API_KEY="gem-key", LLM_PRIMARY_PROVIDER="gemini", LLM_FALLBACK_TO_ZAI="1"),  # gitleaks:allow
        )
        self.assertEqual((text, model), ('{"a": 6}', "glm-5.3"))
        self.assertEqual(seen[-1], ("api.z.ai", "glm-5.3"))
        with self.assertRaises(RuntimeError):
            await self._chat(
                handler, ["gemini-3.8-flash"],
                _llm_env(ZAI_API_KEY="zai-key", GEMINI_API_KEY="gem-key", LLM_PRIMARY_PROVIDER="gemini", LLM_FALLBACK_TO_ZAI="0"),  # gitleaks:allow
            )

    async def test_gemini_fallback_can_be_switched_off(self) -> None:
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.url.host)
            return httpx.Response(500, json={"error": {"message": "upstream"}})

        with self.assertRaises(RuntimeError):
            await self._chat(
                handler, ["glm-5v-turbo"],
                _llm_env(ZAI_API_KEY="zai-key", GEMINI_API_KEY="gem-key", LLM_PRIMARY_PROVIDER="zai", LLM_FALLBACK_TO_GEMINI="0"),  # gitleaks:allow
            )
        self.assertEqual(seen, ["api.z.ai"])


class LlmDiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    """Admin connectivity report: one tiny request per configured provider."""

    async def test_report_lists_keys_chains_and_per_provider_results(self) -> None:
        seen: list[tuple[str, str, bool]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            if request.url.host == "generativelanguage.googleapis.com":
                has_image = any("inlineData" in part for part in body["contents"][0]["parts"])
                seen.append((request.url.host, _gemini_model_from_url(request), has_image))
                self.assertIn("systemInstruction", body)
                return httpx.Response(200, json=_gemini_response('```json\n{"ok": true, "seen": "Kırmızı"}\n```', "gemini-3.8-flash"))
            has_image = any(
                isinstance(part, dict) and part.get("type") == "image_url"
                for part in (body["messages"][1]["content"] if isinstance(body["messages"][1]["content"], list) else [])
            )
            seen.append((request.url.host, body["model"], has_image))
            if request.url.host == "api.z.ai":
                return httpx.Response(500, json={"error": {"message": "upstream down"}})
            # OpenRouter echoes the prompt without looking at the image: must be reported unhealthy.
            return httpx.Response(200, json=_chat_response('{"ok": true, "seen": "görsel"}', body["model"]))

        env = _llm_env(ZAI_API_KEY="zai-key", GEMINI_API_KEY="gem-key", OPENROUTER_API_KEY="or-key", LLM_PRIMARY_PROVIDER="zai", LLM_FALLBACK_TO_OPENROUTER="1")  # gitleaks:allow
        with patch.dict(os.environ, env, clear=True), patch(
            "customs_advisor.httpx.AsyncClient", new=_mock_client_factory(handler)
        ), patch("customs_advisor._retry_sleep", new=AsyncMock()):
            report = await customs_advisor.diagnose_llm_providers(vision=True, timeout_seconds=5)
        self.assertEqual(report["mode"], "vision")
        self.assertEqual(report["primary"], "zai")
        self.assertEqual(report["keys"], {"zai": True, "gemini": True, "openrouter": True})
        self.assertEqual(report["chains"]["primary"], ["glm-5v-turbo", "glm-4.6v"])
        self.assertEqual([fb["provider"] for fb in report["chains"]["fallbacks"]], ["gemini", "openrouter"])
        self.assertTrue(report["healthy"])
        by_provider = {check["provider"]: check for check in report["checks"]}
        self.assertFalse(by_provider["zai"]["ok"])
        self.assertIn("HTTP 500", by_provider["zai"]["error"])
        self.assertTrue(by_provider["gemini"]["ok"])
        self.assertEqual(by_provider["gemini"]["resolved_model"], "gemini-3.8-flash")
        self.assertEqual(by_provider["gemini"]["host"], "generativelanguage.googleapis.com")
        self.assertFalse(by_provider["openrouter"]["ok"])
        self.assertIn("görsel işlenmedi", by_provider["openrouter"]["error"])
        self.assertTrue(all(has_image for _, _, has_image in seen))
        self.assertEqual(seen[0][:2], ("api.z.ai", "glm-5v-turbo"))
        serialised = json.dumps(report)
        for secret in ("zai-key", "gem-key", "or-key"):
            self.assertNotIn(secret, serialised)

    async def test_diagnostic_calls_are_recorded_and_gateway_key_resolves_like_live_calls(self) -> None:
        usage: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_chat_response('{"ok": true, "seen": "metin"}', "gateway-model"))

        env = _llm_env(LLM_BASE_URL="https://gateway.example.com/v1", ZAI_API_KEY="zai-key")
        with patch.dict(os.environ, env, clear=True), patch(
            "customs_advisor.httpx.AsyncClient", new=_mock_client_factory(handler)
        ), patch("customs_advisor._LLM_USAGE_HOOK", new=lambda **kwargs: usage.append(kwargs)):
            report = await customs_advisor.diagnose_llm_providers(vision=False, timeout_seconds=5)
        self.assertEqual(report["primary"], "openrouter")
        self.assertEqual(report["primary_host"], "gateway.example.com")
        self.assertTrue(report["healthy"])
        self.assertEqual(report["checks"][0]["host"], "gateway.example.com")
        # The Z.ai key also makes Z.ai a fallback target, so two calls are recorded.
        self.assertEqual([check["provider"] for check in report["checks"]], ["openrouter", "zai"])
        self.assertEqual(len(usage), 2)
        self.assertEqual(usage[0]["operation"], "diagnostic_text")
        self.assertEqual(usage[0]["model"], "gateway-model")

    async def test_missing_primary_key_is_reported_without_requests(self) -> None:
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request.url.host)
            return httpx.Response(200, json=_chat_response('{"ok": true, "seen": "metin"}'))

        with patch.dict(os.environ, _llm_env(), clear=True), patch(
            "customs_advisor.httpx.AsyncClient", new=_mock_client_factory(handler)
        ):
            report = await customs_advisor.diagnose_llm_providers(vision=False, timeout_seconds=5)
        self.assertEqual(report["primary"], "openrouter")
        self.assertFalse(report["healthy"])
        self.assertEqual(calls, [])
        self.assertIn("anahtar", report["checks"][0]["error"])


class ZaiDescribeImageProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_describe_image_reports_zai_provider(self) -> None:
        buffer = io.BytesIO()
        Image.new("RGB", (120, 90), "white").save(buffer, format="JPEG")
        with patch.dict(os.environ, _llm_env(ZAI_API_KEY="zai-key"), clear=True), patch(
            "customs_advisor._request_openrouter_vision_analysis",
            new=AsyncMock(return_value=({"product_name": "Fincan"}, "glm-5v-turbo")),
        ):
            result = await CustomsAdvisor().describe_image(buffer.getvalue(), "image/jpeg")
        self.assertEqual(result.provider, "zai")
        self.assertEqual(result.model, "glm-5v-turbo")


if __name__ == "__main__":
    unittest.main()
