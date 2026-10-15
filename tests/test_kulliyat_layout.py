import re
import unittest
from pathlib import Path

HTML = (Path(__file__).resolve().parent.parent / "web" / "index.html").read_text(encoding="utf-8")
FORM = HTML[HTML.index('id="tariffForm"'): HTML.index('id="tariffOutput"')]


class KulliyatLayoutTests(unittest.TestCase):
    def test_form_is_grouped_and_every_field_keeps_its_id(self):
        self.assertEqual(FORM.count('class="tariff-group"'), 4)
        ids = set(re.findall(r'id="(tariff\w+)"', FORM))
        for needed in ("tariffGtip", "tariffOrigin", "tariffInvoice", "tariffCurrency", "tariffExchangeRate", "tariffAtr",
                       "tariffSurveillanceCertificate", "tariffPayment", "tariffVat", "tariffGekapTry", "tariffSurveillanceCrossRate"):
            self.assertIn(needed, ids)

    def test_required_green_and_optional_blue_markers(self):
        for field in ("tariffGtip", "tariffOrigin", "tariffInvoice", "tariffCurrency", "tariffExchangeRate"):
            self.assertIsNotNone(re.search(rf'class="field req"[^>]*>(?:(?!</label>).)*id="{field}"', FORM, re.S), field)
        self.assertGreaterEqual(FORM.count('class="field opt'), 15)

    def test_document_questions_are_yes_no_unknown(self):
        self.assertIn('id="tariffAtr"><option value="">Bilmiyorum</option>', FORM)
        self.assertIn('<option value="false">Hayır</option>', FORM)

    def test_menu_strip_and_tree_present(self):
        for marker in ("Vergi Hesap", 'id="infoStrip"', 'id="lawTree"', "Risk Kontrol", "BTB / Fihrist", "Eğitim", "Mütalaa"):
            self.assertIn(marker, HTML)


if __name__ == "__main__":
    unittest.main()
