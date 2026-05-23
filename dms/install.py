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


GMP_WORKFLOW_NAME = "GMP Document Workflow"

# State.doc_status:
#   0 = Draft, 1 = Submitted, 2 = Cancelled.
# Transitioning into a state with doc_status=1 makes apply_workflow()
# automatically submit the document — that's how 'Approved' becomes the
# trigger for on_submit (PDF render etc.).
GMP_WORKFLOW_STATES = [
    {"state": "Draft",                "doc_status": 0, "allow_edit": "QA Manager",     "style": "Warning"},
    {"state": "Under Review",         "doc_status": 0, "allow_edit": "System Manager", "style": "Primary"},
    {"state": "Pending QA Approval",  "doc_status": 0, "allow_edit": "System Manager", "style": "Primary"},
    {"state": "Approved",             "doc_status": 1, "allow_edit": "System Manager", "style": "Success"},
    {"state": "Revision Requested",   "doc_status": 0, "allow_edit": "QA Manager",     "style": "Danger"},
]

# Role-level perms only — the controller adds User-level actor enforcement
# on top (the user must literally be the assigned Reviewer / QA Approver,
# not just anyone with the role).
GMP_WORKFLOW_TRANSITIONS = [
    {"state": "Draft",               "action": "Submit for Review",          "next_state": "Under Review",        "allowed": "QA Manager"},
    {"state": "Revision Requested",  "action": "Submit for Review",          "next_state": "Under Review",        "allowed": "QA Manager"},
    {"state": "Under Review",        "action": "Approve as Reviewer",        "next_state": "Pending QA Approval", "allowed": "QA Manager"},
    {"state": "Under Review",        "action": "Request Revision (Reviewer)","next_state": "Revision Requested",  "allowed": "QA Manager"},
    {"state": "Pending QA Approval", "action": "Approve as QA",              "next_state": "Approved",            "allowed": "QA Manager"},
    {"state": "Pending QA Approval", "action": "Request Revision (QA)",      "next_state": "Under Review",        "allowed": "QA Manager"},
]


def before_install():
    _ensure_role("QA Manager", desk_access=1)


def after_install():
    _ensure_department_abbr_field()
    _ensure_employee_signature_field()
    _ensure_gmp_workflow()


def after_migrate():
    # Idempotent re-assertion of custom fields; never touches user
    # preferences (default_workspace) so existing customizations stick.
    _ensure_department_abbr_field()
    _ensure_employee_signature_field()
    _ensure_gmp_workflow()


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


def _ensure_gmp_workflow():
    """Inject the Frappe Workflow that mirrors the controller's state machine.

    Idempotent: skipped if a workflow with this name already exists, so the
    user can edit transitions in the desk UI without us clobbering them on
    every migrate.

    Coexistence model: the custom whitelisted methods in gmp_document.py
    (submit_for_review, reviewer_approve, qa_approve, ...) call
    frappe.model.workflow.apply_workflow() under the hood, so the form's
    custom buttons and the Frappe-native Action dropdown drive the same
    state transitions. apply_workflow enforces the role on each transition
    (QA Manager); the controller's _ensure_actor() runs first and tightens
    that to the specific assigned User (Reviewer / QA Approver).
    """
    if frappe.db.exists("Workflow", GMP_WORKFLOW_NAME):
        return

    # Workflow rows reference Workflow State + Workflow Action Master records
    # by name; seed any that don't exist yet so the parent insert succeeds.
    for st in GMP_WORKFLOW_STATES:
        if not frappe.db.exists("Workflow State", st["state"]):
            frappe.get_doc({
                "doctype": "Workflow State",
                "workflow_state_name": st["state"],
                "style": st["style"],
            }).insert(ignore_permissions=True)

    seen_actions = set()
    for tr in GMP_WORKFLOW_TRANSITIONS:
        action = tr["action"]
        if action in seen_actions:
            continue
        seen_actions.add(action)
        if not frappe.db.exists("Workflow Action Master", action):
            frappe.get_doc({
                "doctype": "Workflow Action Master",
                "workflow_action_name": action,
            }).insert(ignore_permissions=True)

    frappe.get_doc({
        "doctype": "Workflow",
        "workflow_name": GMP_WORKFLOW_NAME,
        "document_type": "GMP Document",
        "workflow_state_field": "workflow_status",
        "is_active": 1,
        "send_email_alert": 0,
        "states": GMP_WORKFLOW_STATES,
        "transitions": GMP_WORKFLOW_TRANSITIONS,
    }).insert(ignore_permissions=True)

    # get_workflow_name() caches an empty string on first miss; without
    # invalidating, every apply_workflow() call after install will keep
    # returning '' and raise DoesNotExistError until the worker recycles.
    frappe.cache.delete_key("workflow")
