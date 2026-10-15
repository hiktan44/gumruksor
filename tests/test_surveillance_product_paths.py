"""Execute real HTML controls/JS payload builders, then offline API/arithmetic paths."""

from __future__ import annotations

import asyncio
from contextlib import ExitStack
import io
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from bs4 import BeautifulSoup
from openpyxl import Workbook
from starlette.testclient import TestClient

import app as web_app
from bulk_costing import calculate_rows, rows_from_upload
from customs_advisor import CustomsEvidencePack, CustomsInquiry, _deterministic_cost
from decision_questions import apply_decision_answers, build_decision_questions
from tariff_engine import TariffEngine
from tests.test_tariff_data_safety import GTIP, insert_measure, insert_snapshot

ROOT = Path(__file__).resolve().parents[1]


def js_function(source, name):
    start = source.index(f"function {name}(")
    return source[start:source.index("\n}", start) + 2]


def browser_payload(kind, overrides, *, answers=None, direction="import", events=False, questions=None):
    """DOM values/options come from shipped HTML; execute shipped JS, never a copied payload."""
    node = shutil.which("node")
    if not node:
        raise unittest.SkipTest("node bulunamadı")
    html = BeautifulSoup((ROOT / "web/index.html").read_text(), "html.parser")
    dom = {}
    for tag in html.select("[id]"):
        options = [option.get("value", option.text) for option in tag.select("option")]
        selected = tag.select_one("option[selected]") or tag.select_one("option")
        value = tag.get("value", tag.text if tag.name == "textarea" else "")
        if tag.name == "select" and selected is not None:
            value = selected.get("value", selected.text)
        dom["#" + tag["id"]] = dict(value=value, tagName=tag.name.upper(), options=options,
                                   dataset={key[5:]: val for key, val in tag.attrs.items() if key.startswith("data-")})
    for control, value in overrides.items():
        if "#" + control not in dom:
            raise AssertionError(f"HTML control missing: {control}")
        element = html.select_one("#" + control)
        expected_form = "tariffForm" if kind == "tariff" else "customsForm"
        if element.find_parent("form").get("id") != expected_form:
            raise AssertionError(f"Control outside {expected_form}: {control}")
        if element.has_attr("disabled"):
            raise AssertionError(f"Disabled control: {control}")
        if dom["#" + control]["tagName"] == "SELECT" and value not in dom["#" + control]["options"]:
            raise AssertionError(f"Unavailable select option: {control}={value}")
        dom["#" + control]["value"] = value
    source = (ROOT / "web/app.js").read_text()
    names = ["parseLocalizedNumber", "nullableNumber", "liraFields", "collectTariffCostInputs",
             "parseClassificationQuestions", "collectClassificationAnswers", "declarationFields", "customsRequestBody"]
    # Additional helpers are part of the production contract once present.
    for name in ("surveillanceFields", "setDecisionField", "applyDecisionAnswerToFields", "bindLocalizedNumberInputs", "decisionQuestionsHtml"):
        if f"function {name}(" in source:
            names.append(name)
    scripts = [js_function(source, name) for name in names]
    for name in ("EXPORT_NULLED_LEVIES", "DECISION_FIELDS"):
        start = source.index("const " + name + " =")
        scripts.append(source[start:source.index("\n};", start) + 3])
    start = source.index('$("#tariffForm").addEventListener("submit",')
    scripts.append(source[start:source.index("\n});", start) + 4])
    start = source.index('document.addEventListener("change", (event) => {\n  const input = event.target.closest("[data-decision-id]");')
    scripts.append(source[start:source.index("\n});", start) + 4])
    data = dict(dom=dom, kind=kind, answers=answers or {}, direction=direction, events=events, questions=questions or [])
    harness = r"""
const vm = require('node:vm');
const input = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
const handlers = {};
for (const [id, field] of Object.entries(input.dom)) {
  field.options = field.options.map(value => ({value, text: value}));
  field.classList = {toggle() {}, remove() {}};
  field.addEventListener = (name, fn) => {handlers[id + ':' + name] = fn;};
  field.dispatchEvent = () => {};
  field.querySelector = () => ({disabled: false});
}
let captured;
const context = {
  $: id => input.dom[id] || null,
  document: {querySelectorAll: () => Object.values(input.dom).filter(x => x.dataset.number !== undefined),
    addEventListener: (name, fn) => {handlers['document:' + name] = fn;}},
  state: {tradeDirection: input.direction, decisionAnswers: {tool: {}, precheck: {}},
    customsClassificationAnswers: {}, customsGtipSelectionConfirmed: true, customsExactGtipConfirmed: true},
  fetchJson: async (url, options) => {captured = {url, body: JSON.parse(options.body)}; return {};},
  renderTariffTool: () => '', hasCapability: () => true,
  escapeHtml: value => String(value).replaceAll('&', '&amp;').replaceAll('"', '&quot;').replaceAll('<', '&lt;'),
  updateDecisionProgress() {}, showToast() {},
  localizedNumberFormat: new Intl.NumberFormat('tr-TR', {maximumFractionDigits: 4}),
  Event: class Event {},
};
vm.createContext(context);
vm.runInContext(input.source, context);
(async () => {
  for (const [id, value] of Object.entries(input.answers)) {
    const answer = {value, type: id === 'surveillance_cross_rate' ? 'text' : 'radio', checked: true,
      dataset: {decisionId: id, decisionContext: input.kind === 'tariff' ? 'tool' : 'precheck'},
      classList: {toggle() {}}, closest: () => null};
    handlers['document:change']({target: {closest: () => answer}});
  }
  if (input.events) {
    context.bindLocalizedNumberInputs();
    for (const id of ['#tariffSurveillanceCrossRate', '#assistSurveillanceCrossRate']) {
      if (input.dom[id]?.value) {handlers[id + ':blur'](); handlers[id + ':focus']();}
    }
  }
  if (input.kind === 'tariff') {
    await handlers['#tariffForm:submit']({preventDefault() {}, currentTarget: input.dom['#tariffForm']});
    if (!captured) throw new Error('Tariff submit did not produce an API request');
  } else {captured = {url: '/api/customs/precheck', body: context.customsRequestBody()};}
  captured.questions_html = context.decisionQuestionsHtml(input.questions, input.kind === 'tariff' ? 'tool' : 'precheck');
  process.stdout.write(JSON.stringify(captured));
})().catch(error => {console.error(error); process.exitCode = 1;});
"""
    data["source"] = "\n".join(scripts)
    completed = subprocess.run([node, "-e", harness], input=json.dumps(data), text=True, capture_output=True, timeout=15)
    if completed.returncode:
        raise AssertionError(completed.stderr)
    return json.loads(completed.stdout)


def form_values(kind, **conversion):
    if kind == "tariff":
        values = dict(tariffGtip=GTIP, tariffOrigin="Çin", tariffInvoice="1000", tariffQuantity="100",
                      tariffCurrency="EUR", tariffSurveillance="20.000", tariffSurveillanceCertificate="false",
                      tariffEmy="0", tariffAntiDumping="0", tariffSct="0", tariffKkdf="0", tariffVat="20")
        prefix = "tariff"
    else:
        values = dict(customsQuestion="Maliyet nedir?", candidateGtip=GTIP, originCountry="Çin",
                      invoiceValue="1000", quantity="100", currency="EUR", surveillanceUnitValue="20.000",
                      hasSurveillanceCertificate="false", customsDutyRate="10", additionalDutyRate="0",
                      additionalFinancialLiabilityRate="0", antiDumpingAmount="0", sctAmount="0", kkdfRate="0", vatRate="20")
        prefix = "assist"
    names = dict(quantity_unit="tariffQuantityUnit" if kind == "tariff" else "quantityUnit",
                 surveillance_unit=prefix + "SurveillanceUnit", surveillance_currency=prefix + "SurveillanceCurrency",
                 surveillance_cross_rate=prefix + "SurveillanceCrossRate")
    values.update({names[field]: value for field, value in conversion.items()})
    return values


class SurveillanceProductPathTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.engine = TariffEngine(data_dir=self.temp.name)
        with self.engine._connect() as db:
            insert_snapshot(db, "current")
            insert_snapshot(db, "igv", "additional_duty")
            insert_measure(db, "duty", "current", rate=10)
        self.client = TestClient(web_app.app, base_url="https://gumruksor.com")
        self.addCleanup(self.client.close)
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(lambda: asyncio.run(self.engine.close()))
        for name, value in (("tariff_engine", self.engine), ("rate_limiter", web_app.FixedWindowRateLimiter())):
            patcher = patch.object(web_app, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def cost(self, request):
        response = self.client.post(request["url"], json=request["body"], headers={"Origin": "https://gumruksor.com"})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_tariff_html_submit_reaches_api_and_calculator_with_conversion(self):
        for currency, fx, expected in (("USD", "", 2000), ("EUR", "0.900", 1800)):
            with self.subTest(currency=currency):
                values = form_values("tariff", quantity_unit="kg", surveillance_unit="ton",
                                     surveillance_currency="USD", surveillance_cross_rate=fx)
                values["tariffCurrency"] = currency
                request = browser_payload("tariff", values, events=True)
                self.assertEqual(request["body"]["quantity_unit"], "kg")
                result = self.cost(request)["cost"]
                self.assertEqual(result["status"], "complete")
                self.assertEqual(result["customs_value"], expected)
                self.assertEqual(result["landed_total"], expected * 1.1 * 1.2)

    def test_blank_units_are_not_assumed_by_web_submit(self):
        result = self.cost(browser_payload("tariff", form_values("tariff")))["cost"]
        self.assertEqual(result["status"], "partial")
        self.assertIsNone(result["landed_total"])

    def test_advisor_html_payload_model_and_deterministic_cost_keep_conversion(self):
        request = browser_payload("advisor", form_values("advisor", quantity_unit="kg", surveillance_unit="ton",
                                  surveillance_currency="USD", surveillance_cross_rate="0.900"), events=True)
        inquiry = CustomsInquiry.model_validate(request["body"])
        result = _deterministic_cost(inquiry)
        self.assertEqual(result["status"], "user_rates_complete")
        self.assertEqual(result["customs_value_estimate"], 1800)
        self.assertEqual(result["known_landed_total"], 2376)

    def test_advisor_unknown_units_still_block_total(self):
        request = browser_payload("advisor", form_values("advisor"))
        result = _deterministic_cost(CustomsInquiry.model_validate(request["body"]))
        self.assertEqual(result["status"], "rates_missing")
        self.assertIsNone(result["known_landed_total"])

    def test_decision_answers_reach_real_controls_payload_and_api(self):
        answers = dict(quantity_unit="kg", surveillance_unit="ton", surveillance_cross_rate="0.900")
        missing = self.cost(browser_payload("tariff", form_values("tariff")))["decision_questions"]
        request = browser_payload("tariff", form_values("tariff"), answers=answers, questions=missing)
        markup = BeautifulSoup(request["questions_html"], "html.parser")
        self.assertIsNotNone(markup.select_one('input[data-decision-id="surveillance_cross_rate"][type="text"]'))
        self.assertIsNotNone(markup.select_one('input[data-decision-id="quantity_unit"][value="kg"]'))
        result = self.cost(request)["cost"]
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["customs_value"], 1800)

    def test_qualified_turkish_and_english_headers_reach_bulk_calculation(self):
        for headers in (
            ["GTİP", "Menşe", "Fatura (EUR)", "Para birimi", "Miktar", "Miktar birimi (kg)",
             "Gözetim (USD/ton)", "Gözetim dövizi (USD)", "Gözetim birimi (USD/kg)", "Gözetim çapraz kuru (USD/EUR)",
             "Gözetim belgesi", "GV", "İGV", "EMY", "Damping", "ÖTV", "KKDF", "KDV"],
            ["GTIP", "Origin", "Invoice (EUR)", "Currency", "Quantity", "Quantity unit (kg)",
             "Surveillance (USD/ton)", "Surveillance currency (USD)", "Surveillance unit (USD/kg)", "Surveillance cross rate (USD/EUR)",
             "Surveillance certificate", "Customs duty", "Additional duty", "EMY", "Dumping", "SCT", "KKDF", "VAT"],
        ):
            values = [GTIP, "Çin", 1000, "EUR", 100, "kg", 20000, "USD", "ton", "0.900", "false", 10, 0, 0, 0, 0, 0, 20]
            workbook = Workbook()
            workbook.active.append(headers)
            workbook.active.append(values)
            buffer = io.BytesIO()
            workbook.save(buffer)
            workbook.close()
            csv = (";".join(headers) + "\n" + ";".join(str(x) for x in values)).encode()
            for name, raw in (("x.csv", csv), ("x.xlsx", buffer.getvalue())):
                with self.subTest(headers=headers[7], file=name):
                    rows = rows_from_upload(raw, name)
                    self.assertEqual(rows[0].get("surveillance_currency"), "USD")
                    self.assertEqual(rows[0].get("surveillance_unit"), "ton")
                    result = asyncio.run(calculate_rows(self.engine, rows))["rows"][0]
                    self.assertEqual(result["status"], "complete", result)
                    self.assertEqual(result["customs_value"], 1800)

    def test_advisor_api_validates_payload_and_calls_arithmetic_without_llm(self):
        request = browser_payload("advisor", form_values("advisor", quantity_unit="kg", surveillance_unit="ton",
                                  surveillance_currency="USD", surveillance_cross_rate="0,9"))
        async def arithmetic_only(inquiry):
            return SimpleNamespace(status="evidence_only", model_dump=lambda **kwargs: {"deterministic_cost": _deterministic_cost(inquiry)})
        with patch.object(web_app, "_enforce_quota", return_value=None), patch.object(
            web_app.customs_advisor_service, "analyse", side_effect=arithmetic_only
        ) as analyse:
            result = self.cost(request)["deterministic_cost"]
        self.assertEqual(analyse.await_args.args[0].surveillance_cross_rate, 0.9)
        self.assertEqual(result["status"], "user_rates_complete")
        self.assertEqual(result["customs_value_estimate"], 1800)

    def test_cross_rate_validation_and_missing_or_incompatible_conversion(self):
        for overrides in ({"surveillance_unit": ""}, {"quantity_unit": "adet"}, {"surveillance_cross_rate": ""}):
            with self.subTest(overrides=overrides):
                conversion = dict(quantity_unit="kg", surveillance_unit="ton", surveillance_currency="USD", surveillance_cross_rate="0,9")
                conversion.update(overrides)
                request = browser_payload("tariff", form_values("tariff", **conversion))
                result = self.cost(request)["cost"]
                self.assertEqual(result["status"], "partial")
                self.assertIsNone(result["landed_total"])
        for invalid in ("", "0", "-1", "NaN"):
            with self.subTest(cleared_answer=invalid):
                request = browser_payload("tariff", form_values("tariff", quantity_unit="kg", surveillance_unit="ton",
                                          surveillance_currency="USD", surveillance_cross_rate="0,9"),
                                          answers={"surveillance_cross_rate": invalid})
                self.assertIsNone(request["body"]["surveillance_cross_rate"])
                self.assertNotIn("surveillance_cross_rate", request["body"]["decision_answers"])
                result = self.cost(request)["cost"]
                self.assertEqual(result["status"], "partial")
                self.assertIsNone(result["landed_total"])
        for bad in (0, -1, 1000001):
            with self.subTest(bad=bad):
                response = self.client.post("/api/tariff/cost", json={
                    "gtip": GTIP, "origin_country": "Çin", "invoice_value": 1000, "surveillance_cross_rate": bad,
                })
                self.assertEqual(response.status_code, 422)


class SurveillanceMcpPathTests(unittest.IsolatedAsyncioTestCase):
    async def test_prepare_precheck_tool_forwards_conversion_to_inquiry(self):
        import mevzuat_mcp_server
        from fastmcp import Client

        captured = []
        async def evidence_only(inquiry):
            captured.append(inquiry)
            return CustomsEvidencePack(inquiry=inquiry, as_of="2026-10-15", missing_information=[], sources=[],
                                       deterministic_cost=_deterministic_cost(inquiry), legal_notice="synthetic")
        values = dict(question="Maliyet nedir?", invoice_value=1000, quantity=100, currency="EUR", customs_duty_rate=10,
                      additional_duty_rate=0, additional_financial_liability_rate=0, anti_dumping_amount=0,
                      sct_amount=0, kkdf_rate=0, vat_rate=20, surveillance_unit_value=20000,
                      has_surveillance_certificate=False, quantity_unit="kg", surveillance_unit="ton",
                      surveillance_currency="USD", surveillance_cross_rate=0.9)
        with ExitStack() as stack:
            stack.enter_context(patch.object(mevzuat_mcp_server, "BACKGROUND_LOOPS", []))
            stack.enter_context(patch.object(mevzuat_mcp_server.customs_advisor_service, "evidence_pack", side_effect=evidence_only))
            for engine, loop in (
                (mevzuat_mcp_server.ticaret_client, "periodic_refresh_loop"),
                (mevzuat_mcp_server.tariff_engine, "periodic_sync_loop"),
                (mevzuat_mcp_server.control_engine, "periodic_sync_loop"),
                (mevzuat_mcp_server.classification_engine, "periodic_sync_loop"),
            ):
                stack.enter_context(patch.object(engine, loop, AsyncMock()))
                stack.enter_context(patch.object(engine, "close", AsyncMock()))
            stack.enter_context(patch.object(mevzuat_mcp_server.customs_advisor_service, "close", AsyncMock()))
            async with Client(mevzuat_mcp_server.app) as client:
                await client.call_tool("prepare_customs_precheck", values)
        self.assertEqual(captured[0].quantity_unit, "kg")
        self.assertEqual(_deterministic_cost(captured[0])["customs_value_estimate"], 1800)


class SurveillanceDecisionTests(unittest.TestCase):
    def test_missing_conversion_questions_are_explicit_and_have_no_defaults(self):
        inquiry = dict(surveillance_unit_value=20000, has_surveillance_certificate=False,
                       surveillance_currency="USD", currency="EUR")
        questions = {q.id: q for q in build_decision_questions(inquiry=inquiry)}
        for field in ("quantity_unit", "surveillance_unit", "surveillance_cross_rate"):
            self.assertIn(field, questions)
            self.assertIsNone(questions[field].default)
            self.assertEqual(questions[field].affects, [field])

    def test_answered_or_exempt_conversion_produces_no_extra_questions(self):
        values = dict(surveillance_unit_value=20000, has_surveillance_certificate=False,
                      surveillance_currency="USD", currency="EUR", quantity_unit="kg",
                      surveillance_unit="ton", surveillance_cross_rate=0.9)
        for overrides in ({}, {"has_surveillance_certificate": True, "quantity_unit": None},
                          {"surveillance_unit_value": 0, "quantity_unit": None}):
            with self.subTest(overrides=overrides):
                questions = {q.id for q in build_decision_questions(inquiry={**values, **overrides})}
                self.assertFalse(questions & {"quantity_unit", "surveillance_unit", "surveillance_cross_rate"})

    def test_only_valid_explicit_answers_fill_empty_fields(self):
        answers = dict(quantity_unit="kg", surveillance_unit="ton", surveillance_cross_rate="0.900")
        result = apply_decision_answers(answers, {})
        self.assertEqual(result, dict(quantity_unit="kg", surveillance_unit="ton", surveillance_cross_rate=0.9))
        self.assertEqual(apply_decision_answers(answers, dict(quantity_unit="ton", surveillance_cross_rate=1.2))["surveillance_cross_rate"], 1.2)
        for invalid in ("NaN", "Infinity", "0", "-1", "1000001", "unknown"):
            with self.subTest(invalid=invalid):
                self.assertNotIn("surveillance_cross_rate", apply_decision_answers({"surveillance_cross_rate": invalid}, {}))
        self.assertNotIn("quantity_unit", apply_decision_answers({"quantity_unit": "unknown"}, {}))


if __name__ == "__main__":
    unittest.main()
