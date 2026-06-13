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


# GMP Document Type master seed: (type_name, code). `code` becomes the record
# name and the type segment of every GMP Document ID, so it must be filesystem
# /name safe (no spaces or slashes) — hence "Master Formula/BMR" -> "BMR".
GMP_DOCUMENT_TYPES = [
    ("Policy", "POL"),
    ("Manual", "MAN"),
    ("SOP", "SOP"),
    ("Work Instruction", "WI"),
    ("Specification", "SPEC"),
    ("Master Formula/BMR", "BMR"),
    ("Protocol", "PROT"),
    ("Schedule/Plan", "PLAN"),
    ("Form", "FORM"),
    ("Record", "REC"),
    ("Report", "REP"),
    ("Certificate", "CERT"),
    ("Log Book", "LOG"),
    ("Register", "REG"),
    ("List", "LIST"),
    ("Map", "MAP"),
    ("Chart", "CHART"),
    ("Drawing", "DWG"),
    ("Matrix", "MTX"),
    ("Checklist", "CHK"),
]


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
    # Terminal state for cancelled (obsolete) documents. on_cancel() sets
    # workflow_status to this directly (doc_status 2 = cancelled) so the
    # native badge stops reading "Approved".
    {"state": "Obsolete",             "doc_status": 2, "allow_edit": "System Manager", "style": "Danger"},
]

# Each transition is gated by role (QA Manager) AND a per-actor `condition`:
# only the assigned preparer / reviewer / QA approver (or Administrator, as an
# escape hatch) may act. frappe.session is exposed in the workflow condition
# eval globals, so `doc.<field> == frappe.session.user` is a safe expression.
# The controller's _apply_workflow_side_effects() reacts to the resulting
# workflow_status change to stamp audit fields and shuffle ToDos.
_PREPARER = 'doc.prepared_by == frappe.session.user or frappe.session.user == "Administrator"'
_REVIEWER = 'doc.reviewer == frappe.session.user or frappe.session.user == "Administrator"'
_QA = 'doc.qa_approver == frappe.session.user or frappe.session.user == "Administrator"'

GMP_WORKFLOW_TRANSITIONS = [
    {"state": "Draft",               "action": "Submit for Review",          "next_state": "Under Review",        "allowed": "QA Manager", "condition": _PREPARER},
    {"state": "Revision Requested",  "action": "Submit for Review",          "next_state": "Under Review",        "allowed": "QA Manager", "condition": _PREPARER},
    {"state": "Under Review",        "action": "Approve as Reviewer",        "next_state": "Pending QA Approval", "allowed": "QA Manager", "condition": _REVIEWER},
    {"state": "Under Review",        "action": "Request Revision (Reviewer)","next_state": "Revision Requested",  "allowed": "QA Manager", "condition": _REVIEWER},
    {"state": "Pending QA Approval", "action": "Approve as QA",              "next_state": "Approved",            "allowed": "QA Manager", "condition": _QA},
    {"state": "Pending QA Approval", "action": "Request Revision (QA)",      "next_state": "Under Review",        "allowed": "QA Manager", "condition": _QA},
]


def before_install():
    _ensure_role("QA Manager", desk_access=1)


def after_install():
    _ensure_department_abbr_field()
    _ensure_employee_signature_field()
    _ensure_amend_naming_rule()
    _ensure_document_types()
    _ensure_gmp_workflow()
    _sync_gmp_workflow()


def after_migrate():
    # Idempotent re-assertion of custom fields; never touches user
    # preferences (default_workspace) so existing customizations stick.
    _ensure_department_abbr_field()
    _ensure_employee_signature_field()
    _ensure_amend_naming_rule()
    _ensure_document_types()
    _ensure_gmp_workflow()
    _sync_gmp_workflow()


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


def _ensure_amend_naming_rule():
    """Issue #3: make amended GMP Documents run autoname() (…-v1) instead of
    Frappe's default `-1` counter.

    Amend naming is governed by a per-doctype row in the Document Naming
    Settings single's `amend_naming_override` child table (falling back to the
    global `default_amend_naming`, which ships as 'Amend Counter'). With the
    GMP Document row set to 'Default Naming', `_set_amended_name()` returns
    early and the controller's autoname() produces the versioned name.

    Idempotent: upserts the child row directly (without re-saving the single,
    which carries unrelated naming-series side effects)."""
    existing = frappe.db.get_value(
        "Amended Document Naming Settings",
        {"document_type": "GMP Document", "parent": "Document Naming Settings"},
        "name",
    )
    if existing:
        frappe.db.set_value("Amended Document Naming Settings", existing, "action", "Default Naming")
        return

    frappe.get_doc({
        "doctype": "Amended Document Naming Settings",
        "parent": "Document Naming Settings",
        "parenttype": "Document Naming Settings",
        "parentfield": "amend_naming_override",
        "document_type": "GMP Document",
        "action": "Default Naming",
    }).insert(ignore_permissions=True)


def _ensure_document_types():
    """Seed the GMP Document Type master. Idempotent: inserts missing rows by
    code (the record name) and keeps the label in step; never deletes types the
    user may have added or relabelled by hand."""
    for type_name, code in GMP_DOCUMENT_TYPES:
        if frappe.db.exists("GMP Document Type", code):
            if frappe.db.get_value("GMP Document Type", code, "type_name") != type_name:
                frappe.db.set_value("GMP Document Type", code, "type_name", type_name)
            continue
        frappe.get_doc({
            "doctype": "GMP Document Type",
            "code": code,
            "type_name": type_name,
        }).insert(ignore_permissions=True)


def _sync_gmp_workflow():
    """Idempotently bring an existing GMP Document Workflow up to date with the
    states/transitions this module owns: add the 'Obsolete' state and
    (re)assert the per-actor transition `condition`s. Safe on every migrate."""
    if not frappe.db.exists("Workflow", GMP_WORKFLOW_NAME):
        return

    wf = frappe.get_doc("Workflow", GMP_WORKFLOW_NAME)

    have_obsolete = any(s.state == "Obsolete" for s in wf.states)
    if not have_obsolete:
        if not frappe.db.exists("Workflow State", "Obsolete"):
            frappe.get_doc({
                "doctype": "Workflow State",
                "workflow_state_name": "Obsolete",
                "style": "Danger",
            }).insert(ignore_permissions=True)
        wf.append("states", {
            "state": "Obsolete",
            "doc_status": 2,
            "allow_edit": "System Manager",
            "style": "Danger",
        })

    cond_by_action = {tr["action"]: tr["condition"] for tr in GMP_WORKFLOW_TRANSITIONS}
    changed = not have_obsolete
    for tr in wf.transitions:
        desired = cond_by_action.get(tr.action)
        if desired and tr.condition != desired:
            tr.condition = desired
            changed = True

    if changed:
        wf.save(ignore_permissions=True)
        frappe.cache.delete_key("workflow")


def _ensure_gmp_workflow():
    """Inject the Frappe Workflow that mirrors the controller's state machine.

    Idempotent: skipped if a workflow with this name already exists, so the
    user can edit transitions in the desk UI without us clobbering them on
    every migrate.

    Authorisation model: transitions are driven entirely by Frappe's native
    Workflow engine (the form "Actions" menu). apply_workflow() enforces the
    role on each transition (QA Manager) and the per-transition `condition`
    tightens that to the specific assigned User (preparer / Reviewer / QA
    Approver). The controller reacts to the resulting workflow_status change
    in _apply_workflow_side_effects() to stamp audit fields and hand off
    ToDos. _sync_gmp_workflow() keeps existing installs in step.
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
