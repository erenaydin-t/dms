# Copyright (c) 2026, ErenAydin - GMP DMS Module
# License: MIT
"""GMP Word Template controller.

A controlled library of .docx templates. Each record holds the template file
and a table of custom_tag -> system_field mappings. A GMP Document links to one
template; at submit the controller renders that template (filling the mapped
tags) into the document's own controlled .docx + signed PDF.
"""

import os
import re

import frappe
from frappe import _
from frappe.model.document import Document

from docxtpl import DocxTemplate

from dms.dms.doctype.gmp_document.gmp_document import TEMPLATE_FIELD_KEYS

# A Jinja-safe tag is a plain identifier so it can be used as {{ tag }}.
_TAG_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_ALLOWED_EXTENSIONS = (".docx",)


class GMPWordTemplate(Document):
    def before_insert(self):
        # autoname is `field:template_title`; trim before the name is derived so
        # the record name and stored title can't drift over stray whitespace.
        self.template_title = (self.template_title or "").strip()

    def validate(self):
        self.template_title = (self.template_title or "").strip()
        if "/" in self.template_title:
            frappe.throw(_("Template Title may not contain '/'."))
        self._validate_template_file()
        self._validate_mappings()

    def _validate_template_file(self):
        if not self.template_file:
            frappe.throw(_("A .docx template file is required."))
        if not self.template_file.lower().endswith(_ALLOWED_EXTENSIONS):
            frappe.throw(_("The template file must be a .docx file."))

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

    def get_template_physical_path(self):
        """On-disk path of the template .docx, or throw if it is missing."""
        file_name = frappe.db.get_value("File", {"file_url": self.template_file}, "name")
        if not file_name:
            frappe.throw(_("File record not found for template: {0}").format(self.template_file))
        path = frappe.get_doc("File", file_name).get_full_path()
        if not os.path.exists(path):
            frappe.throw(_("Template file is missing on disk: {0}").format(self.template_file))
        return path


@frappe.whitelist()
def scan_template_tags(template):
    """Introspect the template .docx and return the {{ tags }} it actually uses.

    Returns a dict:
        tags            - sorted list of every undeclared Jinja variable found
        already_mapped  - tags that already have a mapping row
        suggestions     - {tag: system_field} where the tag name equals a known
                          catalog key (offered as a default in the UI)
    Used by the 'Scan Template Tags' button to pre-fill mapping rows so the user
    never types tag names by hand.
    """
    doc = frappe.get_doc("GMP Word Template", template)
    doc.check_permission("read")
    path = doc.get_template_physical_path()

    try:
        variables = DocxTemplate(path).get_undeclared_template_variables()
    except Exception:
        frappe.log_error(frappe.get_traceback(), "GMP Word Template: tag scan failed")
        frappe.throw(_("Could not read tags from the template. Is it a valid .docx with Jinja tags?"))

    tags = sorted(v for v in variables if _TAG_RE.match(v))
    mapped = {(r.custom_tag or "").strip() for r in (doc.field_mappings or [])}
    suggestions = {t: t for t in tags if t in TEMPLATE_FIELD_KEYS}

    return {
        "tags": tags,
        "already_mapped": sorted(mapped),
        "suggestions": suggestions,
    }
