# Copyright (c) 2026, ErenAydin - GMP DMS Module
# License: MIT
"""Bootstrap routines for `bench install-app dms`.

Wired in hooks.py:
    before_install -> seeds the QA Manager role referenced in DocType perms
    after_install  -> seeds Department.custom_abbr (required by autoname)
    after_migrate  -> idempotently re-asserts Department.custom_abbr so a
                      missing column never silently breaks GMP Document.autoname()
"""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_field


DEPARTMENT_ABBR_FIELD = {
    "fieldname": "custom_abbr",
    "label": "Abbreviation",
    "fieldtype": "Data",
    "insert_after": "department_name",
    "length": 10,
    "description": (
        "Short abbreviation used in GMP Document IDs (e.g. QA, QC, PROD). "
        "Required by the DMS module."
    ),
}


EMPLOYEE_SIGNATURE_FIELD = {
    "fieldname": "custom_signature_image",
    "label": "Signature (PNG)",
    "fieldtype": "Attach Image",
    "insert_after": "image",
    "description": (
        "PNG image of the employee's handwritten signature. Will be inlined "
        "in the PDF version of GMP Documents this user prepared, reviewed, "
        "or QA-approved. Not embedded in the Word version."
    ),
}


def before_install():
    _ensure_role("QA Manager", desk_access=1)


def after_install():
    _ensure_department_abbr_field()
    _ensure_employee_signature_field()
    _set_dms_as_default_workspace()


def after_migrate():
    # Idempotent re-assertion of custom fields; never touches user
    # preferences (default_workspace) so existing customizations stick.
    _ensure_department_abbr_field()
    _ensure_employee_signature_field()


def _ensure_role(role_name, desk_access=1):
    if frappe.db.exists("Role", role_name):
        return
    frappe.get_doc({
        "doctype": "Role",
        "role_name": role_name,
        "desk_access": desk_access,
    }).insert(ignore_permissions=True)


def _ensure_department_abbr_field():
    """Idempotent: create_custom_field is a no-op when the field already exists."""
    create_custom_field("Department", DEPARTMENT_ABBR_FIELD, ignore_validate=True)


def _ensure_employee_signature_field():
    """Idempotent: create_custom_field is a no-op when the field already exists."""
    create_custom_field("Employee", EMPLOYEE_SIGNATURE_FIELD, ignore_validate=True)


def _set_dms_as_default_workspace():
    """Make /app land on the DMS workspace for every existing real user.

    Only runs in after_install (fresh install). Skipping after_migrate is
    intentional — once a user picks a different default we don't want to
    clobber it on every redeploy.
    """
    users = frappe.get_all("User", filters={"name": ["!=", "Guest"]}, pluck="name")
    for u in users:
        frappe.db.set_value("User", u, "default_workspace", "DMS")
