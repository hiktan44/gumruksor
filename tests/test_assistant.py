"""Tool-calling assistant: loop, limits, server-side verification and the route (PRD Faz 3.3).

No test performs a real LLM call: every model turn comes from a scripted fake
client, and the provider clients are exercised against patched HTTP helpers.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

from starlette.testclient import TestClient

import app as web_app
from account_service import AccountService
from assistant import (
    AssistantModelResult,
    AssistantTool,
    CustomsAssistant,
    GeminiToolLLM,
    LLMTurn,
    OpenAIToolLLM,
    TariffMeasuresArgs,
    ToolCallRecord,
    build_default_tools,
    verify_against_tools,
)
from auth_service import GoogleAuthService

PUBLIC_ORIGIN = "https://gumruksor.com"

TARIFF_OUTPUT = {
    "status": "matched",
    "gtip": "610463000000",
    "origin_country": "Çin",
    "unambiguous_rates": {"customs_duty": 12.0, "additional_duty": 30.0},
    "warnings": [],
}
CONTROL_OUTPUT = {"status": "matched", "gtip": "610463000000", "matches": [{"rule_code": "2026/18"}]}


def final_answer(**overrides: Any) -> str:
    payload = {
        "answer": "Gümrük vergisi %12.",
        "claims": [{"text": "Gümrük vergisi %12'dir.", "source_ids": ["tool_1"]}],
        "gtip_candidates": [],
        "rates": [{"name": "Gümrük vergisi", "value": "12", "source_ids": ["tool_1"]}],
        "next_steps": ["Gümrük müşaviri teyidi alın."],
    }
    payload.update(overrides)
    return json.dumps(payload, ensure_ascii=False)


class FakeLLM:
    """Replays a scripted list of turns and records the tool results it was handed."""

    provider = "fake"

    def __init__(self, turns: list[LLMTurn], *, delay: float = 0.0) -> None:
        self.turns = list(turns)
        self.delay = delay
        self.received: list[list[tuple[str, str, dict[str, Any]]]] = []
        self.started: dict[str, Any] | None = None

    async def _next(self) -> LLMTurn:
        if self.delay:
            await asyncio.sleep(self.delay)
        if not self.turns:
            return LLMTurn(tool_calls=[], text=final_answer(), model="fake-model")
        return self.turns.pop(0)

    async def start(self, *, system: str, user: str, history: list[dict[str, str]], tools: list[AssistantTool]) -> LLMTurn:
        self.started = {"system": system, "user": user, "history": history, "tools": [tool.name for tool in tools]}
        return await self._next()

    async def continue_with_tool_results(self, results: list[tuple[str, str, dict[str, Any]]]) -> LLMTurn:
        self.received.append(results)
        return await self._next()


def call(name: str, args: dict[str, Any] | None = None, *, id: str = "call_1") -> dict[str, Any]:
    return {"id": id, "name": name, "args": args or {}}


def fake_tools() -> list[AssistantTool]:
    tariff = AsyncMock(return_value=dict(TARIFF_OUTPUT))
    controls = AsyncMock(return_value=dict(CONTROL_OUTPUT))
    return [
        AssistantTool("lookup_tariff_measures", "tarife", TariffMeasuresArgs, tariff),
        AssistantTool("lookup_import_controls", "kontrol", TariffMeasuresArgs, controls),
    ]


class ToolLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_function_calls_run_the_handlers_and_become_sources(self) -> None:
        llm = FakeLLM([
            LLMTurn([call("lookup_tariff_measures", {"gtip": "610463000000", "origin_country": "Çin"})], None, "fake"),
            LLMTurn([call("lookup_import_controls", {"gtip": "610463000000"}, id="call_2")], None, "fake"),
            LLMTurn([], final_answer(), "fake"),
        ])
        assistant = CustomsAssistant(tools=fake_tools(), llm=llm)
        response = await assistant.ask("Çin menşeli şort için oran ve TAREKS?")
        self.assertEqual([record.id for record in response.tool_calls], ["tool_1", "tool_2"])
        self.assertEqual([source.tool for source in response.sources], ["lookup_tariff_measures", "lookup_import_controls"])
        self.assertIn("status=matched", response.sources[0].title)
        self.assertEqual(llm.received[0][0][2]["source_id"], "tool_1")
        self.assertTrue(response.legal_notice)
        self.assertEqual(response.rates[0].value, "12")

    async def test_seventh_tool_call_is_rejected(self) -> None:
        turns = [LLMTurn([call("lookup_tariff_measures", {"gtip": "610463000000"}, id=f"call_{i}")], None, "fake") for i in range(1, 8)]
        turns.append(LLMTurn([], final_answer(), "fake"))
        llm = FakeLLM(turns)
        assistant = CustomsAssistant(tools=fake_tools(), llm=llm, max_tool_calls=6)
        response = await assistant.ask("Soru")
        self.assertEqual(len(response.tool_calls), 6)
        self.assertIn("sınır", llm.received[-1][0][2]["error"])

    async def test_unknown_tool_is_refused_without_running_anything(self) -> None:
        llm = FakeLLM([
            LLMTurn([call("delete_everything", {"x": 1})], None, "fake"),
            LLMTurn([], final_answer(claims=[], rates=[]), "fake"),
        ])
        tools = fake_tools()
        assistant = CustomsAssistant(tools=tools, llm=llm)
        response = await assistant.ask("Soru")
        self.assertEqual(response.tool_calls, [])
        self.assertIn("bilinmeyen araç", llm.received[0][0][2]["error"])
        tools[0].handler.assert_not_awaited()
        self.assertTrue(any("bilinmeyen" in item for item in response.warnings))

    async def test_invalid_arguments_are_reported_to_the_model(self) -> None:
        llm = FakeLLM([
            LLMTurn([call("lookup_tariff_measures", {"gtip": "abc"})], None, "fake"),
            LLMTurn([], final_answer(claims=[], rates=[]), "fake"),
        ])
        tools = fake_tools()
        assistant = CustomsAssistant(tools=tools, llm=llm)
        response = await assistant.ask("Soru")
        self.assertEqual(response.tool_calls, [])
        self.assertIn("şemaya uymuyor", llm.received[0][0][2]["error"])
        tools[0].handler.assert_not_awaited()

    async def test_tool_failure_becomes_data_not_a_crash(self) -> None:
        broken = AssistantTool("lookup_tariff_measures", "tarife", TariffMeasuresArgs, AsyncMock(side_effect=RuntimeError("motor kapalı")))
        llm = FakeLLM([
            LLMTurn([call("lookup_tariff_measures", {"gtip": "610463000000"})], None, "fake"),
            LLMTurn([], final_answer(claims=[], rates=[]), "fake"),
        ])
        response = await CustomsAssistant(tools=[broken], llm=llm).ask("Soru")
        self.assertIn("araç hatası", response.tool_calls[0].summary)

    async def test_history_and_question_are_sanitised(self) -> None:
        llm = FakeLLM([LLMTurn([], final_answer(claims=[], rates=[]), "fake")])
        assistant = CustomsAssistant(tools=fake_tools(), llm=llm)
        await assistant.ask(
            "Oran nedir? e-posta: kisi@example.com",
            gtip="6104.63.00.00.00",
            origin_country="Çin",
            history=[{"role": "user", "content": "önceki soru"}, {"role": "assistant", "content": "önceki yanıt"}],
        )
        self.assertNotIn("kisi@example.com", llm.started["user"])
        self.assertIn("610463000000", llm.started["user"])
        self.assertEqual(len(llm.started["history"]), 2)

    async def test_deadline_stops_a_slow_model(self) -> None:
        llm = FakeLLM([LLMTurn([], final_answer(), "fake")], delay=0.5)
        assistant = CustomsAssistant(tools=fake_tools(), llm=llm, deadline_seconds=0.05)
        with self.assertRaises(RuntimeError) as ctx:
            await assistant.ask("Soru")
        self.assertIn("süre", str(ctx.exception))

    async def test_missing_final_json_still_returns_the_tool_evidence(self) -> None:
        llm = FakeLLM([
            LLMTurn([call("lookup_tariff_measures", {"gtip": "610463000000"})], None, "fake"),
            LLMTurn([], "şemaya uymayan düz metin", "fake"),
        ])
        response = await CustomsAssistant(tools=fake_tools(), llm=llm).ask("Soru")
        self.assertEqual(len(response.tool_calls), 1)
        self.assertTrue(any("şemaya uygun" in item for item in response.warnings))


class VerificationTests(unittest.TestCase):
    @staticmethod
    def records() -> list[ToolCallRecord]:
        return [ToolCallRecord(id="tool_1", name="lookup_tariff_measures", args={}, summary="", output=dict(TARIFF_OUTPUT))]

    def test_uncited_claims_are_dropped(self) -> None:
        result = AssistantModelResult(
            answer="Metin.",
            claims=[{"text": "Kaynaksız iddia.", "source_ids": []}, {"text": "Kaynaklı.", "source_ids": ["tool_1"]}],
        )
        verified, unverified = verify_against_tools(result, self.records())
        self.assertEqual([claim.text for claim in verified.claims], ["Kaynaklı."])
        self.assertEqual([item.kind for item in unverified], ["claim"])

    def test_citation_to_an_unknown_source_id_does_not_count(self) -> None:
        result = AssistantModelResult(answer="Metin.", claims=[{"text": "İddia.", "source_ids": ["tool_9"]}])
        verified, unverified = verify_against_tools(result, self.records())
        self.assertEqual(verified.claims, [])
        self.assertEqual(unverified[0].kind, "claim")

    def test_rate_outside_the_tool_output_is_dropped_and_masked(self) -> None:
        result = AssistantModelResult(
            answer="Gümrük vergisi %12, KDV %18'dir.",
            rates=[
                {"name": "Gümrük vergisi", "value": "12", "source_ids": ["tool_1"]},
                {"name": "KDV", "value": "18", "source_ids": ["tool_1"]},
            ],
        )
        verified, unverified = verify_against_tools(result, self.records())
        self.assertEqual([rate.name for rate in verified.rates], ["Gümrük vergisi"])
        self.assertEqual([item.kind for item in unverified], ["rate"])
        self.assertIn("[doğrulanmadı]", verified.answer)
        self.assertIn("%12", verified.answer)

    def test_gtip_outside_the_tool_output_is_dropped_and_masked(self) -> None:
        result = AssistantModelResult(
            answer="Kod 620462310000 olmalıdır; 610463000000 da resmî satırdır.",
            gtip_candidates=[
                {"code": "620462310000", "explanation": "uydurma", "source_ids": ["tool_1"]},
                {"code": "610463000000", "explanation": "resmî", "source_ids": ["tool_1"]},
            ],
        )
        verified, unverified = verify_against_tools(result, self.records())
        self.assertEqual([item.code for item in verified.gtip_candidates], ["610463000000"])
        self.assertEqual([item.kind for item in unverified], ["gtip"])
        self.assertNotIn("620462310000", verified.answer)
        self.assertIn("610463000000", verified.answer)

    def test_supported_gtip_and_rate_survive(self) -> None:
        result = AssistantModelResult(
            answer="610463000000 için gümrük vergisi %12.",
            claims=[{"text": "Oran %12.", "source_ids": ["tool_1"]}],
            gtip_candidates=[{"code": "610463000000", "explanation": "resmî", "source_ids": ["tool_1"]}],
            rates=[{"name": "Gümrük vergisi", "value": "%12", "source_ids": ["tool_1"]}],
        )
        verified, unverified = verify_against_tools(result, self.records())
        self.assertEqual(unverified, [])
        self.assertNotIn("[doğrulanmadı]", verified.answer)


class ProviderClientTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def response(body: dict[str, Any]) -> SimpleNamespace:
        return SimpleNamespace(is_success=True, status_code=200, json=lambda: body, text=json.dumps(body))

    async def test_gemini_client_parses_function_calls_and_sends_responses(self) -> None:
        bodies = [
            {"candidates": [{"content": {"parts": [{"functionCall": {"name": "lookup_tariff_measures", "args": {"gtip": "610463000000"}}}]}}]},
            {"candidates": [{"content": {"parts": [{"text": final_answer()}]}}], "modelVersion": "gemini-test"},
        ]
        posts = AsyncMock(side_effect=[self.response(body) for body in bodies])
        with patch("assistant._post_gemini_generate", new=posts):
            llm = GeminiToolLLM(api_key="k", models=["gemini-3.8-flash"])
            turn = await llm.start(system="S", user="U", history=[], tools=fake_tools())
            self.assertEqual(turn.tool_calls[0]["name"], "lookup_tariff_measures")
            payload = posts.await_args_list[0].kwargs["payload"]
            self.assertIn("functionDeclarations", payload["tools"][0])
            final = await llm.continue_with_tool_results([("call_1", "lookup_tariff_measures", {"status": "matched"})])
        self.assertIsNone(final.tool_calls or None)
        self.assertEqual(final.model, "gemini-test")
        sent = posts.await_args_list[1].kwargs["payload"]["contents"]
        self.assertEqual(sent[-1]["parts"][0]["functionResponse"]["name"], "lookup_tariff_measures")
        self.assertIn("functionCall", sent[-2]["parts"][0])

    async def test_openai_client_parses_tool_calls_and_appends_tool_messages(self) -> None:
        bodies = [
            {"choices": [{"message": {"content": None, "tool_calls": [
                {"id": "call_abc", "type": "function",
                 "function": {"name": "lookup_tariff_measures", "arguments": '{"gtip": "610463000000"}'}}]}}],
             "model": "glm-5.3"},
            {"choices": [{"message": {"content": final_answer()}}], "model": "glm-5.3"},
        ]
        posts = AsyncMock(side_effect=[self.response(body) for body in bodies])
        with patch("assistant._post_chat_completion", new=posts):
            llm = OpenAIToolLLM(provider="openrouter", api_key="k", models=["z-ai/glm-5.3"], base_url="https://openrouter.ai/api/v1")
            turn = await llm.start(system="S", user="U", history=[], tools=fake_tools())
            self.assertEqual(turn.tool_calls[0]["args"], {"gtip": "610463000000"})
            payload = posts.await_args_list[0].kwargs["payload"]
            self.assertEqual(payload["tools"][0]["function"]["name"], "lookup_tariff_measures")
            final = await llm.continue_with_tool_results([("call_abc", "lookup_tariff_measures", {"status": "matched"})])
        self.assertEqual(final.tool_calls, [])
        messages = posts.await_args_list[1].kwargs["payload"]["messages"]
        tool_messages = [item for item in messages if item.get("role") == "tool"]
        self.assertEqual(tool_messages[0]["tool_call_id"], "call_abc")
        self.assertEqual(json.loads(tool_messages[0]["content"])["status"], "matched")
        assistant_message = next(item for item in messages if item.get("role") == "assistant" and item.get("tool_calls"))
        self.assertEqual(assistant_message["tool_calls"][0]["function"]["name"], "lookup_tariff_measures")

    async def test_openai_path_runs_the_same_loop(self) -> None:
        bodies = [
            {"choices": [{"message": {"content": None, "tool_calls": [
                {"id": "call_abc", "type": "function",
                 "function": {"name": "lookup_tariff_measures", "arguments": '{"gtip": "610463000000"}'}}]}}], "model": "glm"},
            {"choices": [{"message": {"content": final_answer()}}], "model": "glm"},
        ]
        posts = AsyncMock(side_effect=[self.response(body) for body in bodies])
        with patch("assistant._post_chat_completion", new=posts):
            llm = OpenAIToolLLM(provider="openrouter", api_key="k", models=["glm"], base_url="https://openrouter.ai/api/v1")
            response = await CustomsAssistant(tools=fake_tools(), llm=llm).ask("Soru")
        self.assertEqual([record.name for record in response.tool_calls], ["lookup_tariff_measures"])
        self.assertEqual(response.rates[0].value, "12")


class ToolSetTests(unittest.IsolatedAsyncioTestCase):
    async def test_default_tool_set_wires_every_engine(self) -> None:
        tools = build_default_tools(
            tariff_engine=SimpleNamespace(lookup=AsyncMock(), decision_tree=AsyncMock(), calculate=AsyncMock()),
            control_engine=SimpleNamespace(lookup=AsyncMock()),
            classification_engine=SimpleNamespace(search=AsyncMock()),
            trade_measure_engine=SimpleNamespace(lookup=lambda *a, **k: None),
            excise_tax_index=SimpleNamespace(lookup=lambda code: {}),
            exchange_rate_service=SimpleNamespace(customs_quote=AsyncMock()),
            vat_rate_index=SimpleNamespace(lookup=lambda code: {}),
        )
        names = {tool.name for tool in tools}
        self.assertEqual(
            names,
            {
                "lookup_tariff_measures", "resolve_turkish_tariff_tree", "calculate_import_landed_cost",
                "lookup_import_controls", "lookup_trade_measures", "lookup_excise_tax", "lookup_vat_rate",
                "get_customs_exchange_rate", "search_classification_evidence", "origin_scenarios", "savings",
            },
        )
        for tool in tools:
            schema = tool.parameters_schema()
            self.assertEqual(schema["type"], "object")
            self.assertIn("properties", schema)

    async def test_vat_tool_is_optional(self) -> None:
        tools = build_default_tools(
            tariff_engine=SimpleNamespace(), control_engine=SimpleNamespace(), classification_engine=SimpleNamespace(),
            trade_measure_engine=SimpleNamespace(), excise_tax_index=SimpleNamespace(),
            exchange_rate_service=SimpleNamespace(), vat_rate_index=None,
        )
        self.assertNotIn("lookup_vat_rate", {tool.name for tool in tools})


class AssistantRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        data_dir = Path(self.temp.name)
        self.auth = GoogleAuthService(
            client_id="test-client", client_secret="test-secret",
            session_secret="test-session-secret-that-is-long-enough", data_dir=data_dir,
        )
        self.accounts = AccountService(data_dir, admin_emails="admin@example.com")
        self.user = {"sub": "user-sub", "email": "user@example.com", "name": "user", "picture": ""}
        with sqlite3.connect(self.accounts.db_path) as connection:
            connection.execute(
                "INSERT INTO users(google_sub,email,name,picture,created_at,last_login_at) VALUES(?,?,?,?,1,1)",
                (self.user["sub"], self.user["email"], self.user["name"], self.user["picture"]),
            )
        self.original = (web_app.google_auth, web_app.account_service, web_app.rate_limiter)
        web_app.google_auth = self.auth
        web_app.account_service = self.accounts
        web_app.rate_limiter = web_app.FixedWindowRateLimiter()
        self.client = TestClient(web_app.app, base_url=PUBLIC_ORIGIN)

    def tearDown(self) -> None:
        self.client.close()
        web_app.google_auth, web_app.account_service, web_app.rate_limiter = self.original
        self.temp.cleanup()

    def post(self, body: dict[str, Any], *, signed: bool = True):
        headers = {"Origin": PUBLIC_ORIGIN}
        if signed:
            headers["Cookie"] = f"{self.auth.session_cookie}={self.auth.create_session(self.user)}"
        return self.client.post("/api/customs/assistant", json=body, headers=headers)

    @staticmethod
    def response_payload() -> dict[str, Any]:
        return {
            "answer": "Gümrük vergisi %12.",
            "claims": [{"text": "Oran %12.", "source_ids": ["tool_1"]}],
            "gtip_candidates": [],
            "rates": [{"name": "Gümrük vergisi", "value": "12", "source_ids": ["tool_1"]}],
            "next_steps": [],
            "tool_calls": [{"id": "tool_1", "name": "lookup_tariff_measures", "args": {"gtip": "610463000000"}, "summary": "matched"}],
            "sources": [{"id": "tool_1", "tool": "lookup_tariff_measures", "title": "matched"}],
            "unverified": [],
            "warnings": [],
            "model": "fake",
            "as_of": "2026-09-14T00:00:00+03:00",
            "legal_notice": "Bu bir ön değerlendirmedir.",
        }

    def _ask_mock(self) -> AsyncMock:
        from assistant import AssistantResponse

        return AsyncMock(return_value=AssistantResponse.model_validate(self.response_payload()))

    def test_anonymous_request_is_rejected(self) -> None:
        response = self.post({"question": "Oran nedir?"}, signed=False)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["code"], "authentication_required")

    def test_signed_user_gets_the_verified_answer(self) -> None:
        ask = self._ask_mock()
        with patch.object(web_app.customs_assistant, "ask", new=ask):
            response = self.post({"question": "Çin menşeli şort için oran nedir?", "gtip": "6104630000"})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["tool_calls"][0]["id"], "tool_1")
        self.assertTrue(body["legal_notice"])
        self.assertEqual(ask.await_args.kwargs["gtip"], "6104630000")

    def test_short_question_is_rejected(self) -> None:
        with patch.object(web_app.customs_assistant, "ask", new=self._ask_mock()):
            response = self.post({"question": "a"})
        self.assertEqual(response.status_code, 422)

    def test_history_longer_than_six_turns_is_rejected(self) -> None:
        history = [{"role": "user", "content": f"soru {index}"} for index in range(7)]
        with patch.object(web_app.customs_assistant, "ask", new=self._ask_mock()):
            response = self.post({"question": "Oran nedir?", "history": history})
        self.assertEqual(response.status_code, 422)

    def test_quota_exhaustion_returns_429(self) -> None:
        for _ in range(15):
            self.accounts.consume(self.user, "classification")
        with patch.object(web_app.customs_assistant, "ask", new=self._ask_mock()):
            response = self.post({"question": "Oran nedir?"})
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.json()["code"], "quota_exceeded")

    def test_rate_limit_is_ten_per_minute(self) -> None:
        with patch.object(web_app.customs_assistant, "ask", new=self._ask_mock()):
            codes = [self.post({"question": "Oran nedir?"}).status_code for _ in range(11)]
        self.assertEqual(codes[:10], [200] * 10)
        self.assertEqual(codes[10], 429)

    def test_llm_failure_becomes_503(self) -> None:
        with patch.object(web_app.customs_assistant, "ask", new=AsyncMock(side_effect=RuntimeError("Asistan süre sınırını aştı."))):
            response = self.post({"question": "Oran nedir?"})
        self.assertEqual(response.status_code, 503)
        self.assertIn("süre", response.json()["error"])


if __name__ == "__main__":
    unittest.main()
