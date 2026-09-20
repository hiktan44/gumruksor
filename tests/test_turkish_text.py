"""Türkçe katlama: noktalı İ tuzağının tek yerde çözüldüğünü kilitler."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from turkish_text import COMBINING_DOT_ABOVE, fold


class FoldTests(unittest.TestCase):
    def test_the_dotted_capital_i_trap(self):
        """Tuzağın birinci yarısı: İ küçültülünce araya birleşen nokta giriyor."""
        self.assertNotIn("ithalat", "İTHALAT".lower())
        self.assertIn(COMBINING_DOT_ABOVE, "İTHALAT".lower())
        # casefold() de çözmez; bu yüzden ayrı bir katlama gerekiyor.
        self.assertNotIn("ithalat", "İTHALAT".casefold())

    def test_the_dotless_capital_i_trap(self):
        """İkinci yarısı: noktasız I, ı yerine ASCII i'ye düşüyor.

        Bu gerçek bir terimi vuruyordu: içtihat ilgi terimlerinden "kıymet", kaynağın
        büyük harfli "GÜMRÜK KIYMETİ" etiketiyle eşleşmiyordu.
        """
        self.assertNotIn("kıymet", "KIYMETİ".lower())
        self.assertIn("kiymet", "KIYMETİ".lower())

    def test_folding_makes_both_comparisons_work(self):
        self.assertEqual(fold("İTHALAT"), "ithalat")
        self.assertEqual(fold("KIYMETİ"), "kıymeti")
        self.assertIn("ithalat", fold("İTHALAT REJİMİ KARARI"))
        self.assertIn("kıymet", fold("GÜMRÜK KIYMETİ"))
        self.assertIn("tebliğ", fold("TEBLİĞ"))
        self.assertIn("istatistik", fold("GÜMRÜK TARİFE İSTATİSTİK POZİSYONU"))

    def test_other_turkish_letters_are_preserved(self):
        self.assertEqual(fold("GÜMRÜK ÇAĞRI ŞÖLEN"), "gümrük çağrı şölen")
        self.assertEqual(fold("ÖZEL TÜKETİM VERGİSİ"), "özel tüketim vergisi")

    def test_the_deliberate_side_effect_is_documented(self):
        """İngilizce büyük I da ı'ya döner; bu bilinçli ve belgelenmiş."""
        self.assertEqual(fold("IMPORT"), "ımport")

    def test_lowercase_input_is_unchanged(self):
        self.assertEqual(fold("gümrük kıymeti"), "gümrük kıymeti")

    def test_empty_and_none_are_safe(self):
        self.assertEqual(fold(""), "")
        self.assertEqual(fold(None), "")


if __name__ == "__main__":
    unittest.main()
