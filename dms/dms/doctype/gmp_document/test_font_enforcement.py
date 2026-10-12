# Copyright (c) 2026, ErenAydin - GMP DMS Module
# License: MIT
"""Unit tests for font enforcement.

Pure-Python: font_enforcement.py has no Frappe imports on purpose, so these run
without a site and without LibreOffice. They assert on the OOXML that comes out
of the rewriter, which is the thing that decides what LibreOffice will draw.
"""

import os
import shutil
import tempfile
import unittest
import zipfile
from xml.etree import ElementTree

from dms.dms.doctype.gmp_document.font_enforcement import (
    detect_enforced_font,
    enforce_docx_font,
    enforce_vsdx_font,
    enforce_xlsx_font,
    font_covers,
    has_rtl,
    is_symbol_font,
    shape_rtl,
)

try:
    import reportlab
except ImportError:  # the overlay tests need it; the rewriter tests do not
    reportlab = None

try:
    import openpyxl
except ImportError:
    openpyxl = None

FA = "نسخه کنترل‌شده"

_DOCUMENT = """<?xml version="1.0"?><w:document xmlns:w="ns"><w:body>
<w:p><w:r><w:rPr><w:rFonts w:ascii="Calibri" w:hAnsi="Calibri" w:cs="Arial"
 w:asciiTheme="minorHAnsi" w:cstheme="minorBidi"/></w:rPr><w:t>text</w:t></w:r></w:p>
<w:p><w:r><w:rPr><w:rFonts w:ascii="Wingdings" w:hAnsi="Wingdings"/></w:rPr><w:t>&#61608;</w:t></w:r></w:p>
<w:p><w:r><w:t>no explicit font — inherits docDefaults</w:t></w:r></w:p>
</w:body></w:document>"""

_STYLES = """<?xml version="1.0"?><w:styles xmlns:w="ns"><w:docDefaults><w:rPrDefault><w:rPr>
<w:rFonts w:asciiTheme="minorHAnsi" w:eastAsiaTheme="minorEastAsia" w:cstheme="minorBidi"/>
</w:rPr></w:rPrDefault></w:docDefaults></w:styles>"""

_HEADER = '<?xml version="1.0"?><w:hdr xmlns:w="ns"><w:rFonts w:ascii="Times New Roman" w:cs="Times New Roman"/></w:hdr>'
_FOOTER = '<?xml version="1.0"?><w:ftr xmlns:w="ns"><w:rFonts w:ascii="Arial" w:cs="Arial"/></w:ftr>'
_THEME = '<?xml version="1.0"?><a:theme xmlns:a="ns2"><a:latin typeface="Calibri Light"/><a:cs typeface="Arial"/></a:theme>'
_PNG = b"\x89PNG\r\n\x1a\nbinary-payload"


def _make_docx():
    path = tempfile.mktemp(suffix=".docx")
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("word/document.xml", _DOCUMENT)
        z.writestr("word/styles.xml", _STYLES)
        z.writestr("word/header1.xml", _HEADER)
        z.writestr("word/footer1.xml", _FOOTER)
        z.writestr("word/theme/theme1.xml", _THEME)
        z.writestr("word/media/image1.png", _PNG)
    return path


def _read(path, part):
    with zipfile.ZipFile(path) as z:
        return z.read(part).decode("utf-8")


class TestDocxFontEnforcement(unittest.TestCase):
    def setUp(self):
        self.path = _make_docx()
        self.addCleanup(lambda: os.path.exists(self.path) and os.unlink(self.path))

    def test_all_four_font_slots_are_set(self):
        enforce_docx_font(self.path, "Vazir")
        body = _read(self.path, "word/document.xml")
        for slot in ("w:ascii", "w:hAnsi", "w:cs", "w:eastAsia"):
            self.assertIn(f'{slot}="Vazir"', body)

    def test_complex_script_slot_is_set(self):
        """Persian is complex-script, so w:cs is the slot that decides its font.
        Setting only w:ascii is the classic 'I changed the font but the Persian
        text didn't change' failure."""
        enforce_docx_font(self.path, "Vazir")
        self.assertIn('w:cs="Vazir"', _read(self.path, "word/document.xml"))
        self.assertNotIn("Arial", _read(self.path, "word/document.xml"))

    def test_theme_attributes_are_removed(self):
        """Theme references outrank the explicit family; leaving them in place
        means the rewrite appears to do nothing in the rendered output."""
        enforce_docx_font(self.path, "Vazir")
        body = _read(self.path, "word/document.xml")
        styles = _read(self.path, "word/styles.xml")
        for attr in ("asciiTheme", "hAnsiTheme", "cstheme", "eastAsiaTheme"):
            self.assertNotIn(attr, body)
            self.assertNotIn(attr, styles)

    def test_doc_defaults_are_set_so_unstyled_runs_inherit(self):
        enforce_docx_font(self.path, "Vazir")
        styles = _read(self.path, "word/styles.xml")
        self.assertIn('w:ascii="Vazir"', styles)
        self.assertIn('w:cs="Vazir"', styles)

    def test_headers_and_footers_are_rewritten(self):
        """Separate parts — miss them and the page furniture keeps the old font
        while the body changes."""
        enforce_docx_font(self.path, "Vazir")
        self.assertIn('w:ascii="Vazir"', _read(self.path, "word/header1.xml"))
        self.assertIn('w:cs="Vazir"', _read(self.path, "word/footer1.xml"))
        self.assertNotIn("Times New Roman", _read(self.path, "word/header1.xml"))

    def test_theme_typefaces_are_rewritten(self):
        enforce_docx_font(self.path, "Vazir")
        theme = _read(self.path, "word/theme/theme1.xml")
        self.assertIn('typeface="Vazir"', theme)
        self.assertNotIn("Calibri Light", theme)

    def test_symbol_fonts_are_preserved_by_default(self):
        """GMP forms draw checkboxes as Wingdings glyphs; rewriting those to a
        text font silently turns every checkbox into a stray letter."""
        enforce_docx_font(self.path, "Vazir")
        self.assertIn('w:ascii="Wingdings"', _read(self.path, "word/document.xml"))

    def test_symbol_fonts_are_replaced_when_preservation_is_off(self):
        enforce_docx_font(self.path, "Vazir", preserve_symbols=False)
        self.assertNotIn("Wingdings", _read(self.path, "word/document.xml"))

    def test_binary_parts_survive_the_round_trip(self):
        enforce_docx_font(self.path, "Vazir")
        with zipfile.ZipFile(self.path) as z:
            self.assertEqual(z.read("word/media/image1.png"), _PNG)

    def test_empty_family_is_a_no_op(self):
        before = _read(self.path, "word/document.xml")
        self.assertEqual(enforce_docx_font(self.path, ""), 0)
        self.assertEqual(_read(self.path, "word/document.xml"), before)

    def test_rewrite_is_idempotent(self):
        first = enforce_docx_font(self.path, "Vazir")
        body_once = _read(self.path, "word/document.xml")
        enforce_docx_font(self.path, "Vazir")
        self.assertEqual(_read(self.path, "word/document.xml"), body_once)
        self.assertGreater(first, 0)

    def test_family_is_xml_escaped(self):
        """The family is free text; written raw, & or " would leave every font
        part malformed and the package unopenable by soffice."""
        family = 'B Nazanin & "Co"'
        enforce_docx_font(self.path, family)
        for part in ("word/document.xml", "word/styles.xml", "word/theme/theme1.xml"):
            ElementTree.fromstring(_read(self.path, part))  # raises if malformed
        self.assertIn('w:cs="B Nazanin &amp; &quot;Co&quot;"', _read(self.path, "word/document.xml"))
        self.assertEqual(detect_enforced_font(self.path), family)

    def test_failed_rewrite_leaves_the_original_intact(self):
        """A failure part way must not leave a truncated .docx that the PDF
        conversion and the integrity hash would then treat as valid."""
        workdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, workdir)
        path = shutil.copy(self.path, os.path.join(workdir, "doc.docx"))
        with zipfile.ZipFile(path, "a") as z:
            z.writestr("word/footer2.xml", b"\xff\xfe not utf-8")
        with open(path, "rb") as fh:
            before = fh.read()
        with self.assertRaises(UnicodeDecodeError):
            enforce_docx_font(path, "Vazir")
        with open(path, "rb") as fh:
            self.assertEqual(fh.read(), before)
        self.assertEqual(os.listdir(workdir), ["doc.docx"])  # no temp file left behind


class TestDocxFontInheritance(unittest.TestCase):
    """A run that names no font inherits it from its style chain; expanding it
    into explicit slots would override a symbol font set on the style."""

    _DOC = """<?xml version="1.0"?><w:document xmlns:w="ns"><w:body>
<w:p><w:r><w:rPr><w:rStyle w:val="Checkbox"/><w:rFonts w:hint="cs"/></w:rPr><w:t>&#61694;</w:t></w:r></w:p>
</w:body></w:document>"""
    _STYLES = """<?xml version="1.0"?><w:styles xmlns:w="ns">
<w:docDefaults><w:rPrDefault><w:rPr><w:rFonts w:hint="eastAsia"/></w:rPr></w:rPrDefault></w:docDefaults>
<w:style w:type="character" w:styleId="Checkbox"><w:rPr><w:rFonts w:ascii="Wingdings" w:hAnsi="Wingdings"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="Normal"><w:rPr><w:rFonts w:hint="cs"/></w:rPr></w:style>
</w:styles>"""

    def setUp(self):
        self.path = tempfile.mktemp(suffix=".docx")
        self.addCleanup(lambda: os.path.exists(self.path) and os.unlink(self.path))
        with zipfile.ZipFile(self.path, "w") as z:
            z.writestr("word/document.xml", self._DOC)
            z.writestr("word/styles.xml", self._STYLES)
        enforce_docx_font(self.path, "Vazir")

    def test_hint_only_run_keeps_inheriting_its_symbol_style(self):
        body = _read(self.path, "word/document.xml")
        self.assertIn('<w:rFonts w:hint="cs"/>', body)
        self.assertNotIn("Vazir", body)
        self.assertIn('w:ascii="Wingdings"', _read(self.path, "word/styles.xml"))

    def test_doc_defaults_are_always_set(self):
        """docDefaults is the root of the chain: nothing above it to inherit."""
        styles = _read(self.path, "word/styles.xml")
        defaults = styles[styles.index("<w:docDefaults>") : styles.index("</w:docDefaults>")]
        self.assertIn('w:cs="Vazir"', defaults)
        self.assertIn('<w:rFonts w:hint="cs"/>', styles)  # the Normal style is left alone

    def test_hint_only_runs_do_not_hide_the_enforcement_signature(self):
        self.assertEqual(detect_enforced_font(self.path), "Vazir")

    def test_segoe_ui_symbol_is_a_symbol_font(self):
        self.assertTrue(is_symbol_font("Segoe UI Symbol"))


class TestVsdxFontEnforcement(unittest.TestCase):
    def test_face_names_are_rewritten(self):
        src = tempfile.mktemp(suffix=".vsdx")
        out = tempfile.mktemp(suffix=".vsdx")
        with zipfile.ZipFile(src, "w") as z:
            z.writestr(
                "visio/document.xml",
                '<FaceNames><FaceName ID="1" Name="Calibri"/>'
                '<FaceName ID="2" Name="Wingdings"/></FaceNames>',
            )
            z.writestr("visio/media/img.png", _PNG)
        try:
            enforce_vsdx_font(src, out, "Vazir")
            doc = _read(out, "visio/document.xml")
            self.assertIn('Name="Vazir"', doc)
            self.assertIn('Name="Wingdings"', doc)  # symbols preserved
            with zipfile.ZipFile(out) as z:
                self.assertEqual(z.read("visio/media/img.png"), _PNG)
        finally:
            for p in (src, out):
                if os.path.exists(p):
                    os.unlink(p)

    def test_rewrite_in_place_with_an_escaped_family(self):
        path = tempfile.mktemp(suffix=".vsdx")
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        with zipfile.ZipFile(path, "w") as z:
            z.writestr("visio/document.xml", '<FaceNames><FaceName ID="1" Name="Calibri"/></FaceNames>')
        enforce_vsdx_font(path, path, "A & B")
        doc = _read(path, "visio/document.xml")
        ElementTree.fromstring(doc)
        self.assertIn('Name="A &amp; B"', doc)
        self.assertEqual(detect_enforced_font(path), "A & B")


class TestRtlShaping(unittest.TestCase):
    def test_detects_persian(self):
        self.assertTrue(has_rtl(FA))
        self.assertFalse(has_rtl("CONTROLLED COPY"))
        self.assertFalse(has_rtl(""))

    def test_latin_is_returned_untouched(self):
        self.assertEqual(shape_rtl("UNCONTROLLED COPY"), "UNCONTROLLED COPY")

    def test_persian_never_raises_even_without_the_shaping_libraries(self):
        """A watermark is not worth failing a controlled PDF over: with
        arabic-reshaper/python-bidi missing the text must come back unchanged,
        not blow up the download."""
        self.assertIsInstance(shape_rtl(FA), str)
        self.assertTrue(shape_rtl(FA))

    def test_symbol_font_detection_is_case_insensitive(self):
        self.assertTrue(is_symbol_font("WingDings"))
        self.assertTrue(is_symbol_font(" symbol "))
        self.assertFalse(is_symbol_font("Vazir"))
        self.assertFalse(is_symbol_font(None))


@unittest.skipUnless(reportlab, "reportlab not installed")
class TestFontCoverage(unittest.TestCase):
    """reportlab draws a missing glyph as a blank box without complaint, so the
    watermark checks coverage first and falls back to Helvetica."""

    @classmethod
    def setUpClass(cls):
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont

        vera = os.path.join(os.path.dirname(reportlab.__file__), "fonts", "Vera.ttf")
        pdfmetrics.registerFont(TTFont("DMSTestVera", vera))

    def test_font_with_the_glyphs_covers_the_text(self):
        self.assertTrue(font_covers("DMSTestVera", "OBSOLETE"))

    def test_missing_glyphs_are_detected(self):
        """Vera has no Arabic script: the mirror image of a Persian-only font
        with no Latin, and exactly the same check."""
        self.assertFalse(font_covers("DMSTestVera", FA))

    def test_builtin_fonts_count_as_covering(self):
        self.assertTrue(font_covers("Helvetica-Bold", "CONTROLLED COPY"))

    def test_unknown_font_does_not_count_as_covering(self):
        self.assertFalse(font_covers("No Such Font", "OBSOLETE"))


class TestEnforcedFontDetection(unittest.TestCase):
    """detect_enforced_font backs the 2.7.1 backfill: it must recognise what an
    enforcement pass produced and not mistake an authored file for it."""

    def setUp(self):
        self.path = _make_docx()
        self.addCleanup(lambda: os.path.exists(self.path) and os.unlink(self.path))

    def test_enforced_docx_is_recognised_despite_preserved_symbols(self):
        enforce_docx_font(self.path, "Vazir")
        self.assertIn("Wingdings", _read(self.path, "word/document.xml"))
        self.assertEqual(detect_enforced_font(self.path), "Vazir")

    def test_authored_docx_is_not_mistaken_for_enforced(self):
        self.assertIsNone(detect_enforced_font(self.path))

    def test_drawing_reports_its_uniform_font(self):
        """A drawing can be uniform in one font without any enforcement, which is
        why the backfill compares the result with the configured family."""
        src = tempfile.mktemp(suffix=".vsdx")
        out = tempfile.mktemp(suffix=".vsdx")
        with zipfile.ZipFile(src, "w") as z:
            z.writestr(
                "visio/document.xml",
                '<FaceNames><FaceName ID="1" Name="Calibri"/>'
                '<FaceName ID="2" Name="Wingdings"/></FaceNames>',
            )
        try:
            self.assertEqual(detect_enforced_font(src), "Calibri")
            enforce_vsdx_font(src, out, "Vazir")
            self.assertEqual(detect_enforced_font(out), "Vazir")
        finally:
            for p in (src, out):
                if os.path.exists(p):
                    os.unlink(p)

    @unittest.skipUnless(openpyxl, "openpyxl not installed")
    def test_enforced_sheet_is_recognised(self):
        from openpyxl.styles import Font

        path = tempfile.mktemp(suffix=".xlsx")
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        wb = openpyxl.Workbook()
        wb.active["A1"] = "Title"
        wb.active["A1"].font = Font(name="Arial")
        wb.active["A2"] = "Body"
        wb.save(path)
        self.assertIsNone(detect_enforced_font(path))  # Arial + default Calibri

        enforce_xlsx_font(wb, "Vazir")
        wb.save(path)
        self.assertEqual(detect_enforced_font(path), "Vazir")

    def test_other_formats_are_never_recognised(self):
        self.assertIsNone(detect_enforced_font("document.pdf"))


if __name__ == "__main__":
    unittest.main()
