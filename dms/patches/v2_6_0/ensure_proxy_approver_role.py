# Copyright (c) 2026, ErenAydin - GMP DMS Module
# License: MIT
"""v2.6.0: create the "DMS Proxy Approver" role before the DocType permissions
sync.

GMP Document now ships a "DMS Proxy Approver" permission row (delegated
approval authority: read/write/submit on every document). Runs in
pre_model_sync so the role exists when that permission row is imported on an
existing site — without it the sync hits a broken Role link. Idempotent."""

import frappe


def execute():
    if not frappe.db.exists("Role", "DMS Proxy Approver"):
        frappe.get_doc(
            {
                "doctype": "Role",
                "role_name": "DMS Proxy Approver",
                "desk_access": 1,
            }
        ).insert(ignore_permissions=True)
