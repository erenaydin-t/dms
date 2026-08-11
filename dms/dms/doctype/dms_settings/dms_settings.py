# Copyright (c) 2026, ErenAydin - GMP DMS Module
# License: MIT
"""Single doctype holding the workflow actors that cannot be derived from the
Employee reporting chain: the QA Supervisor, the Regulatory Manager (Technical
Lead), the final QA Approver and the (optional) CEO. Global defaults with
optional per-department overrides; the three routing actors are resolved in
GMPDocument._resolve_workflow_actors_on_submit_for_approval() when a draft is
submitted for approval, the CEO in _stamp_ceo_authorization() at publication."""

import os
import subprocess

import frappe
from frappe import _
from frappe.model.document import Document

_FONT_CACHE_KEY = "dms_document_font_file"


class DMSSettings(Document):
    def on_update(self):
        # The font-file lookup is cached per family (fc-match is a subprocess);
        # drop it so a changed family or a newly attached .ttf takes effect on
        # the next render instead of after a worker recycle.
        frappe.cache.delete_key(_FONT_CACHE_KEY)

    def validate(self):
        if self.document_font_file and not self.document_font_file.lower().endswith(".ttf"):
            frappe.throw(
                _(
                    "The document font file must be a .ttf — reportlab cannot embed "
                    "OpenType/CFF (.otf) fonts, so the watermark would silently fall "
                    "back to Helvetica."
                ),
                title=_("Unsupported Font Format"),
            )

        seen = set()
        for row in self.department_actors:
            if row.department in seen:
                frappe.throw(
                    _("Department {0} appears more than once in the overrides table.").format(
                        frappe.bold(row.department)
                    )
                )
            seen.add(row.department)


def resolve_document_font():
    """Return the enforced-font policy:
    ``{"enforce": bool, "family": str, "preserve_symbols": bool}``.

    Only the family NAME matters for the document body — LibreOffice resolves it
    through fontconfig at conversion time. The actual font file is needed only
    by reportlab for the watermark/footer overlay; see resolve_font_file()."""
    settings = frappe.get_cached_doc("DMS Settings")
    family = (settings.document_font or "").strip()
    return {
        "enforce": bool(settings.enforce_document_font) and bool(family),
        "family": family,
        "preserve_symbols": bool(settings.preserve_symbol_fonts),
    }


def resolve_font_file(family):
    """Absolute path to a .ttf for ``family``, or None.

    Order: the explicitly attached file, then fontconfig.

    The fontconfig step verifies the family it got back. ``fc-match`` never
    fails — asked for a font that is not installed it silently returns the best
    substitute — so matching on the file alone would happily register DejaVu
    Sans under the name "Vazir" and produce Persian-shaped boxes with no error
    anywhere. We compare the returned family and reject a mismatch.

    Cached per family: this runs on every watermark render, and fc-match is a
    subprocess. Cleared whenever DMS Settings is saved."""
    if not family:
        return None

    cached = frappe.cache.hget(_FONT_CACHE_KEY, family)
    if cached is not None:
        return cached or None

    resolved = _resolve_font_file_uncached(family)
    frappe.cache.hset(_FONT_CACHE_KEY, family, resolved or "")
    return resolved


def _resolve_font_file_uncached(family):
    attached = frappe.db.get_single_value("DMS Settings", "document_font_file")
    if attached:
        name = frappe.db.get_value("File", {"file_url": attached}, "name")
        if name:
            path = frappe.get_doc("File", name).get_full_path()
            if os.path.exists(path) and path.lower().endswith(".ttf"):
                return path
            frappe.log_error(
                f"Attached document font is unusable: {path} "
                f"(must be an existing .ttf; reportlab cannot embed .otf)",
                "DMS: document font",
            )

    try:
        out = subprocess.run(
            ["fc-match", "-f", "%{family}|%{file}", family],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None

    matched_family, _, path = out.partition("|")
    if not path or not os.path.exists(path):
        return None
    # fc-match returns a substitute rather than nothing; accept only a real hit.
    wanted = family.strip().lower()
    families = [f.strip().lower() for f in matched_family.split(",")]
    if not any(wanted == f or wanted in f for f in families):
        frappe.log_error(
            f"Font '{family}' is not installed on this host — fc-match fell back "
            f"to '{matched_family}'. Install the font in every container that "
            f"runs soffice, or attach a .ttf in DMS Settings.",
            "DMS: document font",
        )
        return None
    if not path.lower().endswith(".ttf"):
        return None  # reportlab TTFont cannot embed CFF/OTF outlines
    return path


def resolve_department_actors(department):
    """Return {qa_supervisor, regulatory_manager, qa_approver, ceo} for a
    department: the override row's value when set, else the global default.
    Missing values come back as None — the caller decides whether to throw.

    `ceo` is deliberately optional: unlike the three routing actors it gates
    no transition, it only supplies the CEO authorization block stamped at
    publication, so a site with no CEO sign-off simply leaves it empty."""
    settings = frappe.get_cached_doc("DMS Settings")
    override = None
    for row in settings.department_actors:
        if row.department == department:
            override = row
            break

    def pick(fieldname):
        if override and override.get(fieldname):
            return override.get(fieldname)
        return settings.get(fieldname)

    return {
        "qa_supervisor": pick("qa_supervisor"),
        "regulatory_manager": pick("regulatory_manager"),
        "qa_approver": pick("qa_approver"),
        "ceo": pick("ceo"),
    }
