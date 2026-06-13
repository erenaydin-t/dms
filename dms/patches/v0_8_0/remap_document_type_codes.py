"""v0.8.0: GMP Document.document_type is now a Link to the GMP Document Type
master, whose record name is a short code. Three of the legacy Select values
were stored as full words and no longer match a master record:

    Form     -> FORM
    Protocol -> PROT
    Policy   -> POL

(SOP and WI already equal their codes, so they need no remap.)

This one-time patch repoints those field values onto the correct codes so the
Link resolves. It deliberately does NOT rename the documents themselves: a GMP
Document ID (e.g. "Form-QA-01-v0") is a controlled identifier and must remain
immutable for traceability. Only the document_type field is updated, via a
direct SQL write so submitted (docstatus=1) records are not re-validated and no
workflow side effects fire.
"""

import frappe

from dms.install import _ensure_document_types

# Legacy stored value -> new master code.
LEGACY_TO_CODE = {
    "Form": "FORM",
    "Protocol": "PROT",
    "Policy": "POL",
}


def execute():
    if not frappe.db.has_column("GMP Document", "document_type"):
        return

    # The post_model_sync patch phase runs before after_migrate seeds the
    # master, so make sure the target GMP Document Type records exist before we
    # point links at them. Idempotent.
    if frappe.db.exists("DocType", "GMP Document Type"):
        _ensure_document_types()

    for legacy_value, code in LEGACY_TO_CODE.items():
        frappe.db.sql(
            """
            UPDATE `tabGMP Document`
            SET document_type = %(code)s
            WHERE document_type = %(legacy)s
            """,
            {"code": code, "legacy": legacy_value},
        )
