# Copyright (c) 2026, EspadPharmed - GMP DMS Module
# License: MIT
"""Bootstrap routines for `bench install-app dms`.

Creates the QA Manager role referenced by GMP Document permissions so that
the DocType import does not fail on a fresh site.
"""

import frappe


def before_install():
    _ensure_role("QA Manager", desk_access=1)


def _ensure_role(role_name, desk_access=1):
    if frappe.db.exists("Role", role_name):
        return
    frappe.get_doc({
        "doctype": "Role",
        "role_name": role_name,
        "desk_access": desk_access,
    }).insert(ignore_permissions=True)
