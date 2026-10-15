import unittest

from tariff_engine import LandedCostInput, calculate_landed_cost


class CostExplanationTests(unittest.TestCase):
    def _cost(self, **kwargs):
        base = dict(
            invoice_value=1000, freight=100, insurance=10, customs_duty_rate=10, additional_duty_rate=0,
            additional_financial_liability_rate=0, anti_dumping_amount=0, sct_amount=0, kkdf_rate=0,
            vat_rate=20, surveillance_unit_value=0,
        )
        base.update(kwargs)
        return calculate_landed_cost(LandedCostInput(**base))

    def test_every_line_has_explanation_with_legal_basis(self):
        result = self._cost()
        for line in result.lines:
            explanation = line["explanation"]
            self.assertIn(explanation["source"], {"user_input", "declared_values", "unverified", "payment_method_suggestion"})
            self.assertTrue(explanation["legal_basis"], line["code"])

    def test_vat_base_components_sum_to_vat_base(self):
        result = self._cost()
        vat = next(line for line in result.lines if line["code"] == "vat")
        total = sum(part["amount"] or 0 for part in vat["explanation"]["base_components"])
        self.assertAlmostEqual(total, result.vat_base, places=2)
        self.assertIn("md.21", vat["explanation"]["legal_basis"])

    def test_kkdf_suggestion_and_unverified_are_labelled(self):
        result = self._cost(kkdf_rate=None, payment_method="Peşin", vat_rate=None)
        kkdf = next(line for line in result.lines if line["code"] == "kkdf")
        vat = next(line for line in result.lines if line["code"] == "vat")
        self.assertEqual(kkdf["explanation"]["source"], "payment_method_suggestion")
        self.assertEqual(vat["explanation"]["source"], "unverified")

    def test_arithmetic_unchanged(self):
        result = self._cost()
        self.assertEqual(result.customs_value, 1110.0)
        duty = next(line for line in result.lines if line["code"] == "customs_duty")
        self.assertEqual(duty["amount"], 111.0)


if __name__ == "__main__":
    unittest.main()
