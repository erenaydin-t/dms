# Copyright (c) 2026, ErenAydin - GMP DMS Module
# License: MIT
"""Single doctype holding the workflow actors that cannot be derived from the
Employee reporting chain: the QA Supervisor, the Regulatory Manager (Technical
Lead), the final QA Approver and the (optional) CEO. Global defaults with
optional per-department overrides; the three routing actors are resolved in
GMPDocument._resolve_workflow_actors_on_submit_for_approval() when a draft is
submitted for approval, the CEO in _stamp_ceo_authorization() at publication."""

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
    """Return {qa_supervisor, regulatory_manager, qa_approver, ceo} for a
    department: the override row's value when set, else the global default.
    Missing values come back as None — the caller decides whether to throw.

    `ceo` is deliberately optional: unlike the three routing actors it gates
    no transition, it only supplies the CEO authorization block stamped at
    publication, so a site with no CEO sign-off simply leaves it empty."""
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
        "ceo": pick("ceo"),
    }
