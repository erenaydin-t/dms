# Copyright (c) 2026, ErenAydin - GMP DMS Module
# License: MIT
"""v1.3.0: the two-step review chain (Draft → Under Review → Pending QA
Approval → Approved) becomes the full GMP approval chain (supervisor →
reviewer → QA supervisor [+ sequential delegated queue] → manager →
regulatory → final QA → publish).

This patch prepares existing sites BEFORE install.after_migrate re-syncs the
workflow (which retires the old transitions and, once empty, the 'Pending QA
Approval' state):

1. Documents parked in 'Pending QA Approval' move to 'Pending Final QA
   Approval' — in both chains that is the state acted on by the already-
   assigned qa_approver, so nobody loses a pending task. Documents in
   'Under Review' keep their state and reviewer.
2. Every enabled user holding 'QA Manager' additionally gets the new
   'DMS Initiator' authoring role, because the Draft / Revision Requested
   states' allow_edit moves from QA Manager to DMS Initiator. Without this,
   existing authors would lose the ability to edit their own drafts.

NOT automated (site-specific, must be configured by the admin):
- DMS Settings (QA Supervisor / Regulatory Manager / final QA Approver,
  globally or per department) — submission is blocked with a clear message
  until set.
- Granting 'DMS Approver' to supervisors/managers/regulatory users.
- Employee.reports_to chains for preparers and supervisors.
"""

import frappe

OLD_STATE = "Pending QA Approval"
NEW_STATE = "Pending Final QA Approval"


def execute():
    from dms.install import _ensure_roles

    _ensure_roles()

    remapped = frappe.get_all(
        "GMP Document", filters={"workflow_status": OLD_STATE}, pluck="name"
    )
    for name in remapped:
        frappe.db.set_value(
            "GMP Document", name, "workflow_status", NEW_STATE, update_modified=False
        )
    if remapped:
        print(f"dms v1.3.0: remapped {len(remapped)} document(s) '{OLD_STATE}' -> '{NEW_STATE}'")

    qa_managers = frappe.get_all(
        "Has Role",
        filters={"role": "QA Manager", "parenttype": "User"},
        pluck="parent",
    )
    for user in set(qa_managers):
        if user in ("Administrator", "Guest"):
            continue
        if not frappe.db.get_value("User", user, "enabled"):
            continue
        if frappe.db.exists("Has Role", {"parenttype": "User", "parent": user, "role": "DMS Initiator"}):
            continue
        user_doc = frappe.get_doc("User", user)
        user_doc.append("roles", {"role": "DMS Initiator"})
        user_doc.save(ignore_permissions=True)
