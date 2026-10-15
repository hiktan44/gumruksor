"""Cross-sector real-world customs broker workflow tests.

Validates that the broker can evaluate products across 5 major sectors:
1. Textiles (6109.10)
2. Electronics / Smartphones (8517.13)
3. Machinery (8458.11)
4. Food / Confectionery (1806.31)
5. Cosmetics (3304.99)
"""

import unittest

from starlette.testclient import TestClient

import app as web_app
from tax_lists import estimate_vat_rate


class CustomsCrossSectorTests(unittest.TestCase):
    """Bu senaryolar resmi kontrol/tarife motorlarinin canli arka plan
    senkronizasyonunu tamamlamis olmasini gerektirir (ImportControlEngine ve
    TariffEngine, yanlis sonuc uretmemek icin senkronize olmadan "unavailable"
    doner). Taze bir ortamda (CI, ilk kurulum) bu veri hic yok; bu yuzden
    setUp ilk istekte canli veri bulunmadigini tespit edip testleri atlar."""

    def setUp(self) -> None:
        self.client = TestClient(web_app.app, base_url="https://gumruksor.com")
        probe = self.client.post("/api/controls/lookup", json={"gtip": "610442000000"})
        if probe.status_code == 200 and probe.json().get("status") == "unavailable":
            self.skipTest("Kontrol/tarife motorları henüz senkronize değil (canlı veri gerektirir)")

    def test_sector_textiles(self) -> None:
        """Textiles: VAT estimate 10% on 6109.10, and controls on 6104.42 (ÜGD 2026/18)."""
        client = self.client
        gtip_tshirt = "610910000000"
        # VAT estimate (textile is reduced 10%)
        assert estimate_vat_rate(gtip_tshirt)["rate"] == 10.0

        # Controls check for 6104.42 (Tekstil ve Deri Denetimi ÜGD 2026/18)
        res = client.post("/api/controls/lookup", json={"gtip": "610442000000"})
        assert res.status_code == 200
        data = res.json()
        assert data["status"] == "matched"
        communique_codes = [m["rule"]["code"] for m in data["matches"]]
        assert any("2026/18" in c or "tekstil" in m["rule"]["title"].lower() for m, c in zip(data["matches"], communique_codes))

        # Tariff lookup with Origin = China
        tariff_res = client.post("/api/tariff/lookup", json={"gtip": gtip_tshirt, "origin_country": "Çin"})
        assert tariff_res.status_code == 200
        tariff_data = tariff_res.json()
        assert tariff_data["status"] == "matched"
        assert tariff_data["unambiguous_rates"]["customs_duty"] is not None

    def test_sector_smartphones_excise_and_trt(self) -> None:
        """Smartphones (8517.13): ÖTV correlation to Liste IV (8517.12) and TAREKS 2026/8."""
        client = self.client
        gtip = "851713000011"
        # VAT is standard 20%
        assert estimate_vat_rate(gtip)["rate"] == 20.0

        # Excise tax lookup via HTTP endpoint: should match via HS correlation
        excise_res = client.post("/api/tariff/excise", json={"gtip": gtip})
        assert excise_res.status_code == 200
        excise = excise_res.json()
        assert len(excise["matches"]) > 0
        match = excise["matches"][0]
        assert "(IV)" in match["list_label"] or "IV" in match["list_label"]
        assert "8517.12" in match["matched_code"]
        assert any("2022" in w or "korelasyon" in w for w in excise.get("warnings", []))

        # Landed cost calculation with smartphone CIF and TRT bandrol
        cost_res = client.post("/api/tariff/cost", json={
            "gtip": gtip,
            "origin_country": "Çin",
            "invoice_value": 1000.0,
            "freight": 50.0,
            "insurance": 10.0,
            "currency": "USD",
            "exchange_rate": 35.0,
            "trt_bandrol_rate": 12.0,
            "sct_amount": 500.0,
            "vat_rate": 20.0,
            "customs_duty_rate": 0.0,
            "additional_financial_liability_rate": 0.0,
            "anti_dumping_amount": 0.0,
            "surveillance_unit_value": 0.0,
            "payment_method": "Peşin",
        })
        assert cost_res.status_code == 200
        cost_data = cost_res.json().get("cost") or cost_res.json()
        assert cost_data["status"] == "complete"
        assert cost_data["customs_value"] == 1060.0
        assert cost_data["try_summary"] is not None
        assert cost_data["try_summary"]["exchange_rate"] == 35.0
        assert cost_data["try_summary"]["total_taxes_try"] > 0

    def test_sector_machinery_controls(self) -> None:
        """Machinery (8458.11 - CNC lathes): Controls check and prefix matching."""
        client = self.client
        gtip = "845811"
        res = client.post("/api/controls/lookup", json={"gtip": gtip})
        assert res.status_code == 200
        data = res.json()
        assert data["status"] == "matched"
        # CE / TSE or Machinery safety communique (2026/32)
        assert len(data["matches"]) > 0
        assert any("2026/32" in m["rule"]["code"] or "makin" in m["rule"]["title"].lower() for m in data["matches"])

    def test_sector_food_vat_reduced(self) -> None:
        """Food (1806.31 - Chocolate blocks): Reduced VAT 10%."""
        gtip = "180631000000"
        assert estimate_vat_rate(gtip)["rate"] == 10.0

    def test_sector_cosmetics(self) -> None:
        """Cosmetics (3304.99 - Skincare creams): ÖTV Liste IV and 20% VAT."""
        client = self.client
        gtip = "330499000000"
        assert estimate_vat_rate(gtip)["rate"] == 20.0
        excise_res = client.post("/api/tariff/excise", json={"gtip": gtip})
        assert excise_res.status_code == 200
        excise = excise_res.json()
        assert len(excise["matches"]) > 0
        assert any("IV" in m["list_label"] for m in excise["matches"])

    def test_autocomplete_and_search_endpoints(self) -> None:
        """Autocomplete and keyword search in controls and tariff."""
        client = self.client
        # Autocomplete for smartphone
        res = client.get("/api/tariff/autocomplete?q=telefon")
        assert res.status_code == 200
        data = res.json()
        assert len(data["items"]) > 0
        assert any("8517" in r["gtip"] for r in data["items"])

        # Search controls for toy
        c_res = client.get("/api/controls/search?q=oyuncak")
        assert c_res.status_code == 200
        c_data = c_res.json()
        assert c_data["total"] > 0
        assert any("2026/10" in r["rule"]["code"] or "oyuncak" in r["rule"]["title"].lower() for r in c_data["results"])

        # Tebliğ fihristi: sayı kaynak listesiyle birlikte büyür; sabit yerine
        # tutarlılık ve bilinen ithalat tebliğlerinin varlığı denetlenir.
        cat_res = client.get("/api/controls/communiques")
        assert cat_res.status_code == 200
        cat_data = cat_res.json()
        communiques = cat_data["communiques"]
        # count alanı liste uzunluğuyla tutarlı olmalı.
        assert cat_data["count"] == len(communiques)
        # En az 24 ÜGD ithalat tebliği (2026/*) kataloğa girmeli.
        import_codes = {c["code"] for c in communiques if c["code"].startswith("2026/")}
        assert len(import_codes) >= 24
        # Senaryoda geçen bilinen tebliğler katalogda bulunmalı.
        for expected in ("2026/10", "2026/18"):
            assert expected in import_codes


if __name__ == "__main__":
    unittest.main()
