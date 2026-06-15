"""v1.1.1: purge `template_file` remnants from GMP Word Template.

v1.1.0 made the template file-less (title + tag mappings only) and removed the
`template_file` field from the schema. On sites upgraded from v1.0.0 the field's
artifacts can linger:

    - the `template_file` DB column on `tabGMP Word Template`
    - a leftover Custom Field / Property Setter for it
    - the cached DocType meta still listing the field

Any of these lets stale code (or a not-yet-restarted worker) reach for
`doc.template_file` and raise
``AttributeError: 'GMPWordTemplate' object has no attribute 'template_file'``
on save/create. This idempotent patch removes the remnants and clears the meta
cache so the schema matches the file-less controller.

NOTE: this reconciles the *data/schema* side. If the AttributeError persists
after migrate, the bench is still running the pre-1.1.0 controller in memory —
run `bench restart` (or `bench clear-cache`) so the updated code is loaded.
"""

import frappe


def execute():
    if not frappe.db.exists("DocType", "GMP Word Template"):
        return

    # Drop any leftover Custom Field / Property Setter targeting the field.
    for cf in frappe.get_all(
        "Custom Field",
        filters={"dt": "GMP Word Template", "fieldname": "template_file"},
        pluck="name",
    ):
        frappe.delete_doc("Custom Field", cf, force=True, ignore_permissions=True)

    for ps in frappe.get_all(
        "Property Setter",
        filters={"doc_type": "GMP Word Template", "field_name": "template_file"},
        pluck="name",
    ):
        frappe.delete_doc("Property Setter", ps, force=True, ignore_permissions=True)

    # Drop the physical column if it survived the schema sync.
    if frappe.db.has_column("GMP Word Template", "template_file"):
        frappe.db.sql_ddl("ALTER TABLE `tabGMP Word Template` DROP COLUMN `template_file`")

    # Force the DocType meta to be rebuilt from the current schema so the
    # removed field is no longer reported to controllers/clients.
    frappe.clear_cache(doctype="GMP Word Template")
