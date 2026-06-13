# Copyright (c) 2026, ErenAydin - GMP DMS Module
# License: MIT
"""GMP Word Template controller.

A reusable, file-less definition: a title plus a table of custom_tag ->
system_field mappings. It carries no physical document. A GMP Document selects
a template and uploads its own .docx; at submit the controller renders the
user's file by replacing each {{ tag }} with the mapped system field.
"""

import re

import frappe
from frappe import _
from frappe.model.document import Document

from dms.dms.doctype.gmp_document.gmp_document import TEMPLATE_FIELD_KEYS

# A Jinja-safe tag is a plain identifier so it can be used as {{ tag }}.
_TAG_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class GMPWordTemplate(Document):
    def before_insert(self):
        # autoname is `field:template_title`; trim before the name is derived so
        # the record name and stored title can't drift over stray whitespace.
        self.template_title = (self.template_title or "").strip()

    def validate(self):
        self.template_title = (self.template_title or "").strip()
        if "/" in self.template_title:
            frappe.throw(_("Template Title may not contain '/'."))
        self._validate_mappings()

    def _validate_mappings(self):
        seen_tags = set()
        for row in (self.field_mappings or []):
            tag = (row.custom_tag or "").strip()
            row.custom_tag = tag
            if not tag:
                frappe.throw(_("Row {0}: Word Tag is required.").format(row.idx))
            if not _TAG_RE.match(tag):
                frappe.throw(
                    _("Row {0}: '{1}' is not a valid tag — use letters, digits and underscores, starting with a letter or underscore.").format(
                        row.idx, tag
                    )
                )
            if tag in seen_tags:
                frappe.throw(_("Row {0}: duplicate Word Tag '{1}'.").format(row.idx, tag))
            seen_tags.add(tag)
            if row.system_field not in TEMPLATE_FIELD_KEYS:
                frappe.throw(
                    _("Row {0}: '{1}' is not a known system field.").format(row.idx, row.system_field or "")
                )
