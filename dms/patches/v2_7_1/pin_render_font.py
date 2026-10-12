# Copyright (c) 2026, ErenAydin - GMP DMS Module
# License: MIT
"""v2.7.1: record the font already-approved documents were rendered in.

From 2.7.1 the enforced font is frozen on each document at QA approval
(`render_font_family`), and every later re-render — retirement, cancellation,
expiry, regeneration of a missing file — reuses it instead of reading
DMS Settings. A document with nothing recorded re-renders in its template's own
fonts. That is right for everything approved before 2.7.0, but a document
approved under 2.7.0 with enforcement active was rendered in the configured
family, and re-rendering it in template fonts would change approved content —
the very thing this release stops.

So the font is read back from the record itself: the stored deliverable is
inspected, and when it carries the enforcement signature for the configured
family (detect_enforced_font) that family is recorded. Only the configured
family is accepted: 2.7.0 could not have applied any other, and a sheet that is
merely uniform in Calibri must not pass for an enforced one. A family changed
in DMS Settings after some approvals therefore leaves those documents
unrecorded.

Symbol-font preservation cannot be read back reliably — the template may simply
have had no symbol fonts — so the current setting is recorded with the family.

Documents whose deliverable is missing or unreadable are left unrecorded and
listed in one Error Log entry for review. Idempotent: only documents with no
recorded font are inspected.
"""

import os

import frappe
from frappe.utils import cint

from dms.dms.doctype.gmp_document.font_enforcement import detect_enforced_font


def execute():
    settings = frappe.get_single("DMS Settings")
    family = (settings.document_font or "").strip()
    if not family:
        return

    unreadable = []
    for name in frappe.get_all(
        "GMP Document",
        filters={
            "docstatus": 1,
            "attachment_file": ["is", "set"],
            "render_font_family": ["is", "not set"],
        },
        pluck="name",
    ):
        path = _deliverable_path(frappe.get_doc("GMP Document", name))
        try:
            detected = detect_enforced_font(path) if path else None
        except Exception:
            path = None
        if not path:
            unreadable.append(name)
            continue
        if detected and detected.strip().lower() == family.lower():
            frappe.db.set_value(
                "GMP Document",
                name,
                {
                    "render_font_family": detected,
                    "render_preserve_symbol_fonts": cint(settings.preserve_symbol_fonts),
                },
                update_modified=False,
            )

    if unreadable:
        frappe.log_error(
            "These approved documents have no readable deliverable, so the font "
            "they were approved in could not be recorded; they re-render in their "
            "template's own fonts. That is correct for documents approved before "
            "2.7.0 — check the rest against their approved PDF:\n"
            + "\n".join(unreadable),
            "DMS: document font backfill",
        )


def _deliverable_path(doc):
    for file_name in frappe.get_all(
        "File",
        filters={
            "attached_to_doctype": doc.doctype,
            "attached_to_name": doc.name,
            "file_name": doc._deliverable_filename(),
        },
        pluck="name",
    ):
        path = frappe.get_doc("File", file_name).get_full_path()
        if os.path.exists(path):
            return path
    return None
