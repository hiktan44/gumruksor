"""İhracat tebliğlerinde ek listeleri (FAZ 8.2b).

Canlı ölçüm (16.09.2026) şunu gösterdi: Bedesten'in konsolide metni **eki içermez**.
Her iki ihracat tebliği de "Ekleri için tıklayınız" diyen tek bir *göreceli .docx*
bağlantısıyla biter. Ek listesini metinde arayan eski yol bu yüzden her turda
"Ek-1 GTİP kapsamı ayrıştırılamadı" veriyordu.

Burada kilitlenen dört şey:

1. Tek başına bir ``.docx`` ek ayrıştırılabiliyor (ZIP yolu aynı dosyada boş döner).
2. Madde içi tablo, resmî metnin dediği iddia gücünü taşıyor — Ozon tebliği
   "ihracatı **yasaktır**" dediği için satırlar ``prohibited``, varsayılan değil.
3. İthalat yolu (ZIP arşivi + Ek-N taraması) birebir aynı kalıyor.
4. Bilinmeyen bir ek türü sessizce boş liste değil, açık bir hata veriyor.
"""

from __future__ import annotations

import io
import json
import sys
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from control_engine import (  # noqa: E402
    _LIST_KINDS,
    ImportControlEngine,
    extract_attachment_scope,
    extract_scope_table,
    rule_direction,
)

# Ozon tebliğinin (İhracat: 2023/4) Madde 5 bölümünün canlı metinden kırpılmış hâli.
OZON_TEXT = """
İhracat kısıtlamaları

MADDE 5- (1) Aşağıdaki tabloda belirtilen maddeler ve bu maddelerden herhangi birini
içeren Ek-3/A'da GTİP ve tanımları belirtilen mallar ile söz konusu tabloda belirtilen
mallardan herhangi biri ile çalışan Ek-3/B'de GTİP ve tanımları belirtilen malların
ihracatı yasaktır.

GTİP

MALLARIN TANIMI

2903.14.00.00.00

Karbon tetraklorür

2903.19.10.00.00

1,1,1-Trikloroetan (metilkloroform)

2903.76.10.00.00

Bromoklorodiflorometan

3824.74.00.00.12

142B karışımı

(2) Birinci fıkradaki tabloda yer alan 2903.76.10.00.00 ve 2903.76.20.00.00 GTİP'li
maddeleri içeren Ek-3/A'da yer alan 8424.10 GTİP'li malların ihracatı Çevre,
Şehircilik ve İklim Değişikliği Bakanlığının iznine tabidir.

Tebliğde yer almayan hususlar

MADDE 6- (1) Bu Tebliğde yer almayan hususlarda, ilgili diğer mevzuat hükümleri uygulanır.
"""


def minimal_docx(paragraphs: list[str]) -> bytes:
    """Sahte ama gerçekten geçerli bir .docx üretir (depoda python-docx yok)."""
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-'
        'officedocument.wordprocessingml.document.main+xml"/></Types>'
    )
    rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
        'relationships/officeDocument" Target="word/document.xml"/></Relationships>'
    )
    body = "".join(f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>" for text in paragraphs)
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{body}</w:body></w:document>"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", rels)
        archive.writestr("word/document.xml", document)
    return buffer.getvalue()


def zip_of(name: str, payload: bytes) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(name, payload)
    return buffer.getvalue()


class SingleDocumentAnnexTests(unittest.TestCase):
    """İhracat ekleri tek bir .docx dosyasıdır, arşiv değil."""

    def setUp(self) -> None:
        self.docx = minimal_docx(
            ["GTİP", "0601.10.90.10.00", "Lale soğanı", "0601.20.30.00.00", "Sümbül soğanı"]
        )

    def test_a_bare_docx_annex_is_parsed(self) -> None:
        rows = extract_attachment_scope(self.docx, extension=".docx")
        self.assertEqual([row.gtip_prefix for row in rows], ["060110901000", "060120300000"])
        self.assertEqual(rows[0].description, "Lale soğanı")

    def test_the_archive_path_finds_nothing_in_the_same_file(self) -> None:
        # Asıl hata buydu: .docx teknik olarak ZIP'tir, ama içinde belge üyesi yoktur.
        self.assertEqual(extract_attachment_scope(self.docx), [])

    def test_a_zip_archive_still_works(self) -> None:
        # Gerileme kilidi: ithalat tebliğlerinin eki bir ZIP arşividir.
        rows = extract_attachment_scope(zip_of("ek1.docx", self.docx))
        self.assertEqual([row.gtip_prefix for row in rows], ["060110901000", "060120300000"])

    def test_a_csv_annex_is_parsed(self) -> None:
        payload = "GTİP;Tanım\n0601.10.90.10.00;Lale soğanı\n".encode("utf-8")
        rows = extract_attachment_scope(payload, extension=".csv")
        self.assertEqual([row.gtip_prefix for row in rows], ["060110901000"])

    def test_an_unknown_annex_type_fails_loudly(self) -> None:
        # Sessiz boş liste, "bu üründe yükümlülük yok" gibi okunurdu.
        with self.assertRaises(ValueError) as caught:
            extract_attachment_scope(b"\x00\x01", extension=".rar")
        self.assertIn(".rar", str(caught.exception))

    def test_an_oversized_annex_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            extract_attachment_scope(b"0" * (50 * 1024 * 1024 + 1), extension=".csv")


class InlineTableTests(unittest.TestCase):
    """Ozon tebliğinin yasak listesi madde gövdesindedir, ekinde değil."""

    def _rows(self):
        return extract_scope_table(
            OZON_TEXT, r"İhracat\s+kısıtlamaları", r"Tebliğde\s+yer\s+almayan\s+hususlar"
        )

    def test_the_prohibited_table_is_extracted_from_the_article_body(self) -> None:
        codes = [row.gtip_prefix for row in self._rows()]
        self.assertIn("290314000000", codes)
        self.assertIn("290376100000", codes)
        self.assertIn("382474000012", codes)

    def test_the_table_stops_before_the_next_article(self) -> None:
        # Madde 6'nın içine taşarsa sonraki tebliğ atıflarını yasak sayardık.
        self.assertNotIn("MADDE 6", "".join(row.source_line for row in self._rows()))

    def test_rows_default_to_scope_until_the_engine_labels_them(self) -> None:
        # Saf ayrıştırıcı iddia gücünü bilmez; onu yapılandırma söyler.
        self.assertEqual({row.list_kind for row in self._rows()}, {"scope"})


class AttachmentUrlTests(unittest.TestCase):
    """Bedesten ek adresini mutlak URL olarak verir (canlı ölçüldü)."""

    # Doğal Çiçek Soğanları 2026 tebliğinin `ekler` alanından, birebir.
    REAL_DOCX = "https://www.mevzuat.gov.tr/MevzuatMetin/yonetmelik/9.5.42768-Ek.docx"

    def test_a_docx_annex_is_now_accepted(self) -> None:
        # Eski kod yalnız .zip kabul ediyordu; ihracat ekleri bu yüzden hiç indirilmiyordu.
        self.assertEqual(
            ImportControlEngine._official_attachment_url("", [self.REAL_DOCX]), self.REAL_DOCX
        )

    def test_a_zip_annex_still_wins_for_import_records(self) -> None:
        zip_url = "https://www.mevzuat.gov.tr/MevzuatMetin/yonetmelik/9.5.42906-Ek.zip"
        self.assertEqual(ImportControlEngine._official_attachment_url("", [zip_url]), zip_url)

    def test_an_unparseable_type_is_ignored(self) -> None:
        self.assertIsNone(
            ImportControlEngine._official_attachment_url(
                '<a href="https://www.mevzuat.gov.tr/MevzuatMetin/ek.exe">Ek</a>'
            )
        )

    def test_an_off_host_annex_is_refused(self) -> None:
        # Resmî metne enjekte edilmiş bir bağlantı bizi başka alan adına götürmemeli.
        self.assertIsNone(
            ImportControlEngine._official_attachment_url('<a href="https://evil.example.com/ek.docx">Ek</a>')
        )

    def test_plain_http_is_refused(self) -> None:
        self.assertIsNone(
            ImportControlEngine._official_attachment_url('<a href="http://www.mevzuat.gov.tr/ek.docx">Ek</a>')
        )


class ShippedConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        config = json.loads(Path("control_sources.json").read_text(encoding="utf-8"))
        self.rules = config["rules"]
        self.exports = {r["code"]: r for r in self.rules if rule_direction(r) == "export"}

    def test_every_import_record_is_untouched(self) -> None:
        imports = [r for r in self.rules if rule_direction(r) == "import"]
        self.assertEqual(len(imports), 25)

    def test_no_export_record_still_waits_for_an_annex_in_the_body_text(self) -> None:
        # Ölçüm: ek, konsolide metinde yok. scope_annex ile beklemek her turda hata üretirdi.
        for code, rule in self.exports.items():
            self.assertNotIn("scope_annex", rule, code)
            self.assertTrue(rule.get("scope_table") or rule.get("scope_attachment"), code)

    def test_the_ozone_table_is_labelled_prohibited_as_the_official_text_says(self) -> None:
        table = self.exports["IHR/OZON"]["scope_table"]
        self.assertEqual(table["list_kind"], "prohibited")

    def test_the_ozone_patterns_match_the_real_text(self) -> None:
        table = self.exports["IHR/OZON"]["scope_table"]
        rows = extract_scope_table(OZON_TEXT, table["start_pattern"], table["end_pattern"])
        self.assertTrue(rows)

    def test_the_flower_bulb_annex_is_downloaded_not_read_from_the_body(self) -> None:
        rule = self.exports["IHR/CICEK-SOGANI"]
        self.assertTrue(rule["scope_attachment"])
        self.assertEqual(rule["scope_attachment_list_kind"], "licence_required")

    def test_every_declared_list_kind_is_a_known_one(self) -> None:
        for code, rule in self.exports.items():
            for kind in (
                (rule.get("scope_table") or {}).get("list_kind"),
                rule.get("scope_attachment_list_kind"),
            ):
                if kind is not None:
                    self.assertIn(kind, _LIST_KINDS, code)

    def test_export_records_stay_optional_so_import_never_blocks(self) -> None:
        for code, rule in self.exports.items():
            self.assertTrue(rule.get("optional"), code)


if __name__ == "__main__":
    unittest.main()
