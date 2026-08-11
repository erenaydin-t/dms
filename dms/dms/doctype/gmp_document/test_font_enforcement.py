# Copyright (c) 2026, ErenAydin - GMP DMS Module
# License: MIT
"""Unit tests for font enforcement.

Pure-Python: font_enforcement.py has no Frappe imports on purpose, so these run
without a site and without LibreOffice. They assert on the OOXML that comes out
of the rewriter, which is the thing that decides what LibreOffice will draw.
"""

import os
import tempfile
import unittest
import zipfile

from dms.dms.doctype.gmp_document.font_enforcement import (
    enforce_docx_font,
    enforce_vsdx_font,
    has_rtl,
    is_symbol_font,
    shape_rtl,
)

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


if __name__ == "__main__":
    unittest.main()
