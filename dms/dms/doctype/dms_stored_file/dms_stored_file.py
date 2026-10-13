# Copyright (c) 2026, ErenAydin - GMP DMS Module
# License: MIT
"""Registry of the files the DMS stores for GMP Documents (see dms.dms.storage).
Rows are written only by the storage module; the desk shows them read-only."""

import frappe
from frappe.model.document import Document


class DMSStoredFile(Document):
    pass


def on_doctype_update():
    frappe.db.add_unique("DMS Stored File", ["document", "file_name"])
