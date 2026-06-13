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

    def _normalize_code(self):
        self.code = (self.code or "").strip().upper()
        if not self.code:
            frappe.throw(_("Code is mandatory."))
        if any(ch in self.code for ch in r' /\:*?"<>|'):
            frappe.throw(_("Code may not contain spaces, slashes or path characters."))
