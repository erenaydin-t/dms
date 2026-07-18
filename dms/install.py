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
# `allow_edit` is the single role permitted to edit a document while it sits in
# that state (Frappe Workflow allows exactly one role per state). The preparer
# states (Draft / Revision Requested) belong to "DMS Initiator" (the authoring
# role) so authors edit their own drafts; the in-pipeline and submitted states
# are owned by "DMS Manager" (the module-owner/admin role) so an owner can
# correct or override a document anywhere in the controlled lifecycle. A module
# owner who also authors drafts should additionally hold "DMS Initiator".
#
# The approval chain (see GMP_WORKFLOW_TRANSITIONS):
#   Draft → Pending Supervisor Approval → Under Review (supervisor's manager)
#   → Pending QA Supervisor → [QA Review In Progress: sequential delegated
#   queue, driven by the controller's queue engine, not by manual transitions]
#   → Pending Manager Approval (the same manager who reviewed) → Pending
#   Regulatory Validation → Pending Final QA Approval → Approved (Publish).
GMP_WORKFLOW_STATES = [
    {"state": "Draft",                         "doc_status": 0, "allow_edit": "DMS Initiator", "style": "Warning"},
    {"state": "Pending Supervisor Approval",   "doc_status": 0, "allow_edit": "DMS Manager",   "style": "Primary"},
    {"state": "Under Review",                  "doc_status": 0, "allow_edit": "DMS Manager",   "style": "Primary"},
    {"state": "Pending QA Supervisor",         "doc_status": 0, "allow_edit": "DMS Manager",   "style": "Primary"},
    {"state": "QA Review In Progress",         "doc_status": 0, "allow_edit": "DMS Manager",   "style": "Info"},
    {"state": "Pending Manager Approval",      "doc_status": 0, "allow_edit": "DMS Manager",   "style": "Primary"},
    {"state": "Pending Regulatory Validation", "doc_status": 0, "allow_edit": "DMS Manager",   "style": "Primary"},
    {"state": "Pending Final QA Approval",     "doc_status": 0, "allow_edit": "DMS Manager",   "style": "Primary"},
    {"state": "Approved",                      "doc_status": 1, "allow_edit": "DMS Manager",   "style": "Success"},
    {"state": "Revision Requested",            "doc_status": 0, "allow_edit": "DMS Initiator", "style": "Danger"},
    # Terminal state for an abandoned draft revision (a record created via
    # create_revision(), i.e. revision_of is set). The record is retained for
    # audit — on_trash blocks deletion — and the document it revises stays
    # Approved/active throughout. doc_status stays 0: the draft never entered
    # the controlled (submitted) domain. allow_edit "DMS Manager" keeps the
    # dead draft read-only for regular QA Managers.
    {"state": "Revision Cancelled",   "doc_status": 0, "allow_edit": "DMS Manager", "style": "Danger"},
    # Terminal state for cancelled (obsolete) documents. on_cancel() sets
    # workflow_status to this directly (doc_status 2 = cancelled) so the
    # native badge stops reading "Approved". allow_edit is "QA Manager" here so
    # that the workflow does NOT mark a cancelled document read-only for the
    # preparer/approver: Frappe hides the "Amend" (create new version) action
    # when the current workflow state makes the form read-only, so a plain
    # QA Manager must be in this state's allowed-edit roles to revise an
    # obsolete document. (Administrator and module owners who also hold
    # QA Manager are unaffected.)
    {"state": "Obsolete",             "doc_status": 2, "allow_edit": "QA Manager",  "style": "Danger"},
]

# Each transition is gated by role AND a per-actor `condition`: only the
# specific User resolved onto the document (supervisor / reviewer / QA
# supervisor / regulatory manager / QA approver — or Administrator, as an
# escape hatch) may act. frappe.session is exposed in the workflow condition
# eval globals, so `doc.<field> == frappe.session.user` is a safe expression.
# The controller's _apply_workflow_side_effects() reacts to the resulting
# workflow_status change to stamp audit fields and shuffle ToDos.
_PREPARER = 'doc.prepared_by == frappe.session.user or frappe.session.user == "Administrator"'
_SUPERVISOR = 'doc.supervisor == frappe.session.user or frappe.session.user == "Administrator"'
_REVIEWER = 'doc.reviewer == frappe.session.user or frappe.session.user == "Administrator"'
_QA_SUPERVISOR = 'doc.qa_supervisor == frappe.session.user or frappe.session.user == "Administrator"'
_REGULATORY = 'doc.regulatory_manager == frappe.session.user or frappe.session.user == "Administrator"'
_QA = 'doc.qa_approver == frappe.session.user or frappe.session.user == "Administrator"'
# Cancelling a revision is only meaningful on a draft created via
# create_revision() (revision_of set) and is reserved to the preparer, the QA
# approver, or Administrator. A plain draft (no revision_of) never offers it.
_REVISION_CANCEL = (
    "doc.revision_of and ("
    "doc.prepared_by == frappe.session.user "
    "or doc.qa_approver == frappe.session.user "
    'or frappe.session.user == "Administrator")'
)

# allow_self_approval=1 on every transition: without it Frappe blocks a user
# from acting on a document they *own* (doc.owner == user) — which is the
# NORMAL case here: the preparer both creates the draft and submits it for
# review. The real per-actor control is the `condition` on each transition
# (assigned preparer/reviewer/QA approver only), not the ownership check.
# Forward chain + one-level "Return" at every stage (plus a full return to
# the preparer from the pre-QA stages). "QA Review In Progress" has no manual
# forward transition on purpose: it is entered by delegate_qa_review() and
# left by the controller's queue engine when the last delegated reviewer
# completes — only the emergency "Recall Delegation" is a human action.
GMP_WORKFLOW_TRANSITIONS = [
    {"state": "Draft",                         "action": "Submit for Approval",     "next_state": "Pending Supervisor Approval",   "allowed": "DMS Initiator", "condition": _PREPARER, "allow_self_approval": 1},
    {"state": "Revision Requested",            "action": "Submit for Approval",     "next_state": "Pending Supervisor Approval",   "allowed": "DMS Initiator", "condition": _PREPARER, "allow_self_approval": 1},

    {"state": "Pending Supervisor Approval",   "action": "Approve (Supervisor)",    "next_state": "Under Review",                  "allowed": "DMS Approver",  "condition": _SUPERVISOR, "allow_self_approval": 1},
    {"state": "Pending Supervisor Approval",   "action": "Return to Preparer",      "next_state": "Revision Requested",            "allowed": "DMS Approver",  "condition": _SUPERVISOR, "allow_self_approval": 1},

    {"state": "Under Review",                  "action": "Approve as Reviewer",     "next_state": "Pending QA Supervisor",         "allowed": "DMS Approver",  "condition": _REVIEWER, "allow_self_approval": 1},
    {"state": "Under Review",                  "action": "Return to Supervisor",    "next_state": "Pending Supervisor Approval",   "allowed": "DMS Approver",  "condition": _REVIEWER, "allow_self_approval": 1},
    {"state": "Under Review",                  "action": "Return to Preparer",      "next_state": "Revision Requested",            "allowed": "DMS Approver",  "condition": _REVIEWER, "allow_self_approval": 1},

    {"state": "Pending QA Supervisor",         "action": "Approve (QA Supervisor)", "next_state": "Pending Manager Approval",      "allowed": "DMS Approver",  "condition": _QA_SUPERVISOR, "allow_self_approval": 1},
    {"state": "Pending QA Supervisor",         "action": "Return to Reviewer",      "next_state": "Under Review",                  "allowed": "DMS Approver",  "condition": _QA_SUPERVISOR, "allow_self_approval": 1},
    {"state": "Pending QA Supervisor",         "action": "Return to Preparer",      "next_state": "Revision Requested",            "allowed": "DMS Approver",  "condition": _QA_SUPERVISOR, "allow_self_approval": 1},

    {"state": "QA Review In Progress",         "action": "Recall Delegation",       "next_state": "Pending QA Supervisor",         "allowed": "DMS Approver",  "condition": _QA_SUPERVISOR, "allow_self_approval": 1},

    {"state": "Pending Manager Approval",      "action": "Approve (Manager)",       "next_state": "Pending Regulatory Validation", "allowed": "DMS Approver",  "condition": _REVIEWER, "allow_self_approval": 1},
    {"state": "Pending Manager Approval",      "action": "Return to QA Supervisor", "next_state": "Pending QA Supervisor",         "allowed": "DMS Approver",  "condition": _REVIEWER, "allow_self_approval": 1},

    {"state": "Pending Regulatory Validation", "action": "Validate (Regulatory)",   "next_state": "Pending Final QA Approval",     "allowed": "DMS Approver",  "condition": _REGULATORY, "allow_self_approval": 1},
    {"state": "Pending Regulatory Validation", "action": "Return to Manager",       "next_state": "Pending Manager Approval",      "allowed": "DMS Approver",  "condition": _REGULATORY, "allow_self_approval": 1},

    {"state": "Pending Final QA Approval",     "action": "Publish",                 "next_state": "Approved",                      "allowed": "QA Manager",    "condition": _QA, "allow_self_approval": 1},
    {"state": "Pending Final QA Approval",     "action": "Return to Regulatory",    "next_state": "Pending Regulatory Validation", "allowed": "QA Manager",    "condition": _QA, "allow_self_approval": 1},

    # Abandon a draft revision at any pre-QA-delegation stage. Terminal: no
    # transition leaves Revision Cancelled, and the revised document remains
    # the effective version untouched.
    {"state": "Draft",                         "action": "Cancel Revision",         "next_state": "Revision Cancelled",            "allowed": "DMS Initiator", "condition": _REVISION_CANCEL, "allow_self_approval": 1},
    {"state": "Revision Requested",            "action": "Cancel Revision",         "next_state": "Revision Cancelled",            "allowed": "DMS Initiator", "condition": _REVISION_CANCEL, "allow_self_approval": 1},
    {"state": "Pending Supervisor Approval",   "action": "Cancel Revision",         "next_state": "Revision Cancelled",            "allowed": "DMS Initiator", "condition": _REVISION_CANCEL, "allow_self_approval": 1},
    {"state": "Under Review",                  "action": "Cancel Revision",         "next_state": "Revision Cancelled",            "allowed": "DMS Initiator", "condition": _REVISION_CANCEL, "allow_self_approval": 1},
    {"state": "Pending QA Supervisor",         "action": "Cancel Revision",         "next_state": "Revision Cancelled",            "allowed": "DMS Initiator", "condition": _REVISION_CANCEL, "allow_self_approval": 1},
]

# Rows of the pre-v1.3 short chain that the module used to own and now
# actively removes on migrate (states are only removed when no transition
# still references them and no document sits in them — see
# _sync_gmp_workflow). Documents parked in a retired state are remapped by
# patch v1_3_0.upgrade_workflow_chain BEFORE the state row disappears.
GMP_RETIRED_TRANSITIONS = {
    ("Draft", "Submit for Review"),
    ("Revision Requested", "Submit for Review"),
    ("Under Review", "Request Revision (Reviewer)"),
    ("Pending QA Approval", "Approve as QA"),
    ("Pending QA Approval", "Request Revision (QA)"),
    ("Pending QA Approval", "Cancel Revision"),
}
GMP_RETIRED_STATES = {"Pending QA Approval"}


# Module-owned roles:
#   DMS Initiator — authors: create drafts, upload the controlled file,
#                   submit for approval, cancel their own draft revisions.
#   DMS Approver  — line/QA/regulatory actors acting through workflow
#                   transitions (supervisor, reviewer/manager, QA supervisor,
#                   regulatory manager). The per-transition `condition` pins
#                   each action to the specific User resolved on the document.
#   QA Manager    — QA department staff; owns the final Publish.
#   DMS Manager   — module owner / admin.
DMS_ROLES = ("QA Manager", "DMS Manager", "DMS Initiator", "DMS Approver")


def before_install():
    # Created before the DocTypes sync so their permission rows resolve the
    # roles on a fresh install.
    for role in DMS_ROLES:
        _ensure_role(role, desk_access=1)


def after_install():
    _ensure_roles()
    _ensure_department_abbr_field()
    _ensure_employee_signature_field()
    _ensure_amend_naming_rule()
    _ensure_document_types()
    _ensure_gmp_workflow()
    _sync_gmp_workflow()


def after_migrate():
    # Idempotent re-assertion of custom fields; never touches user
    # preferences (default_workspace) so existing customizations stick.
    _ensure_roles()
    _ensure_department_abbr_field()
    _ensure_employee_signature_field()
    _ensure_amend_naming_rule()
    _ensure_document_types()
    _ensure_gmp_workflow()
    _sync_gmp_workflow()


def _ensure_roles():
    for role in DMS_ROLES:
        _ensure_role(role, desk_access=1)


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
    """Issue #3: make amended GMP Documents run autoname() (bumping the trailing
    version segment, e.g. …-0001-2) instead of Frappe's default `-1` counter.

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
        # type_name is a unique field. An earlier version of this seed may have
        # created the same label under a different code (record name); inserting
        # it again would raise IntegrityError(1062, "Duplicate entry … for key
        # 'type_name'") and abort the whole migration. Skip when the label
        # already exists so re-seeding stays idempotent.
        if frappe.db.exists("GMP Document Type", {"type_name": type_name}):
            continue
        frappe.get_doc({
            "doctype": "GMP Document Type",
            "code": code,
            "type_name": type_name,
        }).insert(ignore_permissions=True)


def _sync_gmp_workflow():
    """Idempotently bring an existing GMP Document Workflow up to date with the
    states/transitions this module owns: append any missing state or
    transition and (re)assert the per-actor transition `condition`s. Never
    removes rows the user added by hand. Safe on every migrate."""
    if not frappe.db.exists("Workflow", GMP_WORKFLOW_NAME):
        return

    wf = frappe.get_doc("Workflow", GMP_WORKFLOW_NAME)
    changed = False

    # Append module-owned states that the install predates (e.g. 'Obsolete',
    # 'Revision Cancelled'), seeding the Workflow State master as needed.
    have_states = {s.state for s in wf.states}
    for st in GMP_WORKFLOW_STATES:
        if st["state"] in have_states:
            continue
        if not frappe.db.exists("Workflow State", st["state"]):
            frappe.get_doc({
                "doctype": "Workflow State",
                "workflow_state_name": st["state"],
                "style": st["style"],
            }).insert(ignore_permissions=True)
        wf.append("states", {
            "state": st["state"],
            "doc_status": st["doc_status"],
            "allow_edit": st["allow_edit"],
            "style": st["style"],
        })
        changed = True

    # Append module-owned transitions that the install predates (e.g. the
    # 'Cancel Revision' fan), seeding the Workflow Action Master as needed.
    have_transitions = {(tr.state, tr.action) for tr in wf.transitions}
    for tr in GMP_WORKFLOW_TRANSITIONS:
        if (tr["state"], tr["action"]) in have_transitions:
            continue
        if not frappe.db.exists("Workflow Action Master", tr["action"]):
            frappe.get_doc({
                "doctype": "Workflow Action Master",
                "workflow_action_name": tr["action"],
            }).insert(ignore_permissions=True)
        wf.append("transitions", {
            "state": tr["state"],
            "action": tr["action"],
            "next_state": tr["next_state"],
            "allowed": tr["allowed"],
            "condition": tr["condition"],
            "allow_self_approval": tr.get("allow_self_approval", 1),
        })
        changed = True

    # Re-assert conditions, the allowed role and the self-approval flag on
    # rows that exist but drifted. Keyed by (state, action) — the same action
    # may fan out from several states.
    #
    # `allowed` drifts when a site admin RENAMES a role: rename_doc rewrites
    # every link, so the transition suddenly requires e.g. a Persian-named
    # role while the doctype permissions and code role-checks keep using the
    # canonical English name that migrate re-creates — silently locking every
    # real user out of the workflow. Re-asserting restores the canonical role
    # (rename roles for display via Translation, not by renaming the Role).
    cond_by_key = {(tr["state"], tr["action"]): tr for tr in GMP_WORKFLOW_TRANSITIONS}
    for tr in wf.transitions:
        desired = cond_by_key.get((tr.state, tr.action))
        if not desired:
            continue
        if tr.condition != desired["condition"]:
            tr.condition = desired["condition"]
            changed = True
        if tr.allowed != desired["allowed"]:
            tr.allowed = desired["allowed"]
            changed = True
        # next_state drifts when the module re-routes a kept action (v1.3
        # points 'Approve as Reviewer' at 'Pending QA Supervisor' instead of
        # the retired 'Pending QA Approval').
        if tr.next_state != desired["next_state"]:
            tr.next_state = desired["next_state"]
            changed = True
        want_self = desired.get("allow_self_approval", 1)
        if int(tr.allow_self_approval or 0) != want_self:
            tr.allow_self_approval = want_self
            changed = True

    # Remove transitions of the retired pre-v1.3 short chain. Unlike the
    # append/assert passes this DOES delete rows — but only the exact
    # (state, action) pairs the module itself used to ship; anything a site
    # admin added by hand has a different key and is left alone.
    before_count = len(wf.transitions)
    wf.transitions = [
        tr for tr in wf.transitions if (tr.state, tr.action) not in GMP_RETIRED_TRANSITIONS
    ]
    if len(wf.transitions) != before_count:
        changed = True

    # Retire states once nothing references them: no remaining transition
    # from/into the state and no document parked in it (patch
    # v1_3_0.upgrade_workflow_chain remaps those first).
    for retired in GMP_RETIRED_STATES:
        if any(tr.state == retired or tr.next_state == retired for tr in wf.transitions):
            continue
        if frappe.db.exists("GMP Document", {"workflow_status": retired}):
            continue
        before_count = len(wf.states)
        wf.states = [s for s in wf.states if s.state != retired]
        if len(wf.states) != before_count:
            changed = True

    # Re-assert allow_edit so existing installs pick up the DMS Manager
    # (module-owner) editing rights on the in-pipeline / submitted states.
    edit_by_state = {st["state"]: st["allow_edit"] for st in GMP_WORKFLOW_STATES}
    for s in wf.states:
        desired_edit = edit_by_state.get(s.state)
        if desired_edit and s.allow_edit != desired_edit:
            s.allow_edit = desired_edit
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
