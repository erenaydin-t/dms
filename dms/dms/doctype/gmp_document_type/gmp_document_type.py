# Copyright (c) 2026, ErenAydin - GMP DMS Module
# License: MIT
"""GMP Document Type master.

A small, searchable master of controlled-document categories (SOP, Policy,
Master Formula/BMR, ...). `GMP Document.document_type` links here, so the
category list is managed as data instead of a hardcoded Select.

The record `name` is the short `code` (e.g. "BMR") used inside GMP Document
IDs; `type_name` is the human label shown in the Link field and on the PDF.
Codes are used for naming because some labels contain "/" which is illegal in
Frappe document names and physical filenames.
"""

import frappe
from frappe import _
from frappe.model.document import Document


class GMPDocumentType(Document):
    def before_insert(self):
        # autoname is `field:code`, and the framework reads `code` to build the
        # record name *before* validate() runs — so normalisation has to happen
        # here, in before_insert (which runs before naming), or the name and the
        # stored code would diverge for lower/mixed-case input.
        self._normalize_code()

    def validate(self):
        # Re-assert on every save (including rename/edit). `code` becomes a
        # segment of every GMP Document ID and the renamed .docx/.pdf on disk,
        # so it must stay terse and filesystem/name safe.
        self._normalize_code()

    def before_rename(self, old, new, merge=False):
        # Renaming rewrites the Link on every GMP Document — approved, signed
        # records included — and the code is part of their IDs. Allowed only
        # while nothing uses the type (e.g. to fix a typo in a new code).
        if frappe.db.exists("GMP Document", {"document_type": old}):
            frappe.throw(
                _(
                    "Document type {0} is used by GMP Documents and cannot be renamed: "
                    "its code is part of their IDs, and renaming would rewrite approved records."
                ).format(frappe.bold(old))
            )
        return _checked_code(new)

    def _normalize_code(self):
        self.code = _checked_code(self.code)


def _checked_code(code):
    """The code, upper-cased, or a throw when it cannot be a name segment.

    The GMP Document ID is exactly four dash-separated segments, so a dash
    would add one; "_" and "%" are LIKE wildcards in the chain-number lookup;
    the rest are illegal in names or file names."""
    code = (code or "").strip().upper()
    if not code:
        frappe.throw(_("Code is mandatory."))
    if any(ch in code for ch in r' -_%/\:*?"<>|'):
        frappe.throw(_("Code may not contain spaces, dashes, underscores, % or path characters."))
    return code
