# Copyright (c) 2026, ErenAydin - GMP DMS Module
# License: MIT
"""Single doctype holding the workflow actors that cannot be derived from the
Employee reporting chain: the QA Supervisor, the Regulatory Manager (Technical
Lead) and the final QA Approver. Global defaults with optional per-department
overrides; resolution happens in GMPDocument._resolve_workflow_actors() when a
draft is submitted for approval."""

import frappe
from frappe import _
from frappe.model.document import Document


class DMSSettings(Document):
    def validate(self):
        seen = set()
        for row in self.department_actors:
            if row.department in seen:
                frappe.throw(
                    _("Department {0} appears more than once in the overrides table.").format(
                        frappe.bold(row.department)
                    )
                )
            seen.add(row.department)


def resolve_department_actors(department):
    """Return {qa_supervisor, regulatory_manager, qa_approver} for a
    department: the override row's value when set, else the global default.
    Missing values come back as None — the caller decides whether to throw."""
    settings = frappe.get_cached_doc("DMS Settings")
    override = None
    for row in settings.department_actors:
        if row.department == department:
            override = row
            break

    def pick(fieldname):
        if override and override.get(fieldname):
            return override.get(fieldname)
        return settings.get(fieldname)

    return {
        "qa_supervisor": pick("qa_supervisor"),
        "regulatory_manager": pick("regulatory_manager"),
        "qa_approver": pick("qa_approver"),
    }
