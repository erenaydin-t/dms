# Copyright (c) 2026, ErenAydin - GMP DMS Module
# License: MIT
"""Force one font family across everything the DMS generates.

The problem this solves: the body font of a generated document comes from the
*template*, not from us. LibreOffice renders whatever the .docx asks for, so a
site standardising on a Persian font (Vazir) would otherwise have to re-author
every template — and would still be one careless upload away from a document in
Calibri. These functions rewrite the font references in the rendered file just
before conversion, so the configured family wins regardless of what the author
chose.

Persian specifics, which is what makes this worth doing properly:

* OOXML gives each run THREE font slots — ``w:ascii``/``w:hAnsi`` (Latin),
  ``w:eastAsia`` (CJK) and ``w:cs`` (**complex script**). Persian and Arabic are
  complex-script, so they take ``w:cs``. Setting the font in Word's normal font
  box only writes ``w:ascii``, which is the usual reason "I set the font but the
  Persian text didn't change". We write all four.
* Theme attributes (``w:asciiTheme``, ``w:cstheme``, …) take precedence over the
  explicit family, so they are REMOVED rather than rewritten — otherwise the
  theme silently wins and nothing appears to happen.
* Shaping, ligatures and bidi in the document body are LibreOffice's job and it
  does them correctly (HarfBuzz) once the font resolves. We deliberately do NOT
  touch paragraph direction: forcing RTL would wreck Latin paragraphs, and
  correctly-authored Persian already carries its own direction marks.

reportlab, used for the watermark and footer overlay, is the opposite case: it
has no shaping and no bidi at all, so ``shape_rtl`` below must pre-process any
Persian string before it is drawn.

Deliberately free of Frappe imports so it stays unit-testable in isolation,
matching format_renderers.py.
"""

import re
import zipfile

# Fonts whose glyphs are pictures, not letters. GMP forms routinely draw
# checkboxes as Wingdings characters, and rewriting those to a text font turns
# every ☑ into a stray letter — a silent corruption of the printed record. Kept
# by default; the caller can switch it off for a literal every-font-replaced
# pass.
SYMBOL_FONTS = frozenset(
    {
        "wingdings",
        "wingdings 2",
        "wingdings 3",
        "webdings",
        "symbol",
        "zapfdingbats",
        "marlett",
        "bookshelf symbol 7",
        "ms outlook",
        "segoe mdl2 assets",
        "segoe fluent icons",
    }
)

# .docx parts that carry font references. Headers and footers are separate parts
# — miss them and the page furniture keeps the old font while the body changes.
_DOCX_FONT_PARTS = re.compile(
    r"^word/("
    r"document\d*\.xml"
    r"|styles\.xml"
    r"|header\d*\.xml"
    r"|footer\d*\.xml"
    r"|footnotes\.xml"
    r"|endnotes\.xml"
    r"|numbering\.xml"
    r"|comments\.xml"
    r"|theme/theme\d*\.xml"
    r")$"
)

_RFONTS = re.compile(r"<w:rFonts\b([^>]*?)(/?)>")
_ATTR = re.compile(r'(\w+:\w+)\s*=\s*"([^"]*)"')
# DrawingML theme typefaces: <a:latin typeface="..."/>, <a:cs .../>, <a:ea .../>
_THEME_TYPEFACE = re.compile(r'(<a:(?:latin|cs|ea)\b[^>]*?typeface=")([^"]*)(")')

_FONT_SLOTS = ("w:ascii", "w:hAnsi", "w:cs", "w:eastAsia")
_THEME_SLOTS = ("w:asciiTheme", "w:hAnsiTheme", "w:cstheme", "w:eastAsiaTheme")


def is_symbol_font(name):
    return (name or "").strip().lower() in SYMBOL_FONTS


def _rewrite_rfonts(match, family, preserve_symbols):
    attrs = dict(_ATTR.findall(match.group(1)))
    self_closing = match.group(2)

    if preserve_symbols and any(is_symbol_font(attrs.get(slot)) for slot in _FONT_SLOTS):
        return match.group(0)

    for slot in _FONT_SLOTS:
        attrs[slot] = family
    # Theme references outrank the explicit family, so drop them entirely.
    for slot in _THEME_SLOTS:
        attrs.pop(slot, None)

    rendered = " ".join(f'{k}="{v}"' for k, v in attrs.items())
    return f"<w:rFonts {rendered}{self_closing}>"


def enforce_docx_font(path, family, preserve_symbols=True):
    """Rewrite every font reference in the .docx at ``path`` to ``family``.

    Covers the body, styles (including ``docDefaults``, which is what runs with
    no explicit font inherit), headers, footers, footnotes, endnotes, numbering
    and the theme. Rewritten in place via a full zip round-trip; parts without
    font references are copied byte-for-byte.

    Returns the number of font references rewritten, so a caller can log that
    the pass actually did something.
    """
    if not family:
        return 0

    with zipfile.ZipFile(path) as zin:
        items = [(i.filename, zin.read(i.filename)) for i in zin.infolist()]

    rewrites = 0
    out_items = []
    for name, data in items:
        if _DOCX_FONT_PARTS.match(name):
            xml = data.decode("utf-8")
            xml, n = _RFONTS.subn(
                lambda m: _rewrite_rfonts(m, family, preserve_symbols), xml
            )
            rewrites += n
            xml, n = _THEME_TYPEFACE.subn(
                lambda m: (
                    m.group(0)
                    if (preserve_symbols and is_symbol_font(m.group(2)))
                    else f"{m.group(1)}{family}{m.group(3)}"
                ),
                xml,
            )
            rewrites += n
            data = xml.encode("utf-8")
        out_items.append((name, data))

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zout:
        for name, data in out_items:
            zout.writestr(name, data)
    return rewrites


def enforce_xlsx_font(workbook, family, preserve_symbols=True):
    """Set ``family`` on every populated cell of an openpyxl workbook.

    Applied to cells rather than the workbook's shared style table, which is a
    private openpyxl structure whose shape changes between versions. That means
    a font set on an empty, never-touched cell is not rewritten — harmless,
    since an empty cell prints nothing.

    Returns the number of cells restyled.
    """
    if not family:
        return 0

    from copy import copy

    changed = 0
    for ws in workbook.worksheets:
        for row in ws.iter_rows():
            for cell in row:
                if cell.value is None:
                    continue
                font = cell.font
                if preserve_symbols and is_symbol_font(getattr(font, "name", None)):
                    continue
                if getattr(font, "name", None) == family:
                    continue
                new_font = copy(font)
                new_font.name = family
                cell.font = new_font
                changed += 1
    return changed


# Visio keeps a table of face names that shapes reference by index, so renaming
# the faces themselves redirects every piece of text on the page.
_VSDX_FACENAME = re.compile(r'(<FaceName\b[^>]*?\bName=")([^"]*)(")')


def enforce_vsdx_font(source_path, out_path, family, preserve_symbols=True):
    """Best-effort font enforcement for a .vsdx, by rewriting the FaceNames
    table that shapes reference.

    Unverified against libvisio's PDF export, like the rest of the Visio image
    path in this module — Visio support in LibreOffice is thin. Never raises:
    a Visio template that does not match this structure is copied through
    unchanged rather than failing the render. Returns the number of face names
    rewritten.
    """
    if not family:
        return 0

    with zipfile.ZipFile(source_path) as zin:
        items = [(i.filename, zin.read(i.filename)) for i in zin.infolist()]

    rewrites = 0
    out_items = []
    for name, data in items:
        if name.startswith("visio/") and name.endswith(".xml"):
            try:
                xml = data.decode("utf-8")
            except UnicodeDecodeError:
                out_items.append((name, data))
                continue
            xml, n = _VSDX_FACENAME.subn(
                lambda m: (
                    m.group(0)
                    if (preserve_symbols and is_symbol_font(m.group(2)))
                    else f"{m.group(1)}{family}{m.group(3)}"
                ),
                xml,
            )
            rewrites += n
            data = xml.encode("utf-8")
        out_items.append((name, data))

    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zout:
        for name, data in out_items:
            zout.writestr(name, data)
    return rewrites


# --------------------------------------------------------------------------- #
#  RTL text shaping for reportlab                                             #
# --------------------------------------------------------------------------- #

_ARABIC_RANGE = re.compile(r"[؀-ۿݐ-ݿࢠ-ࣿﭐ-﷿ﹰ-﻿]")


def has_rtl(text):
    """True when the string contains Arabic-script (incl. Persian) characters."""
    return bool(text) and bool(_ARABIC_RANGE.search(text))


def shape_rtl(text):
    """Return ``text`` ready to be drawn by a renderer with no text shaping.

    reportlab draws glyphs left to right, exactly as they appear in the string,
    and applies no Arabic joining — so Persian handed to it raw comes out as
    disconnected, mirrored letterforms. This applies the two missing steps:
    contextual joining (arabic_reshaper) and the Unicode bidi algorithm
    (python-bidi), producing a visually-ordered string of presentation forms.

    Latin text is returned untouched, so it is safe to call on everything. If
    either library is unavailable the original string is returned rather than
    raising: a watermark is not worth failing a PDF over, and Latin — the only
    thing that rendered before this existed — is unaffected either way.
    """
    if not has_rtl(text):
        return text
    try:
        import arabic_reshaper

        try:
            from bidi import get_display
        except ImportError:  # python-bidi < 0.6
            from bidi.algorithm import get_display

        return get_display(arabic_reshaper.reshape(text))
    except Exception:
        return text
