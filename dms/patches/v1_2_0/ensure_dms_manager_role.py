"""v1.2.0: create the "DMS Manager" role before the DocType permissions sync.

GMP Document / GMP Word Template / GMP Document Type now ship a "DMS Manager"
permission row (the module-owner/admin role: full CRUD + cancel, every
department). Runs in pre_model_sync so the role exists when those permission
rows are imported on an existing site. Idempotent."""

import frappe


def execute():
    if not frappe.db.exists("Role", "DMS Manager"):
        frappe.get_doc(
            {
                "doctype": "Role",
                "role_name": "DMS Manager",
                "desk_access": 1,
            }
        ).insert(ignore_permissions=True)
