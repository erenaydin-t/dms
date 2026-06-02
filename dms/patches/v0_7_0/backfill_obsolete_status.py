"""Issue #2: cancelled GMP Documents should read 'Obsolete', not 'Approved'.

Backfills existing cancelled documents (docstatus=2) that predate the
on_cancel() status transition shipped in v0.7.0.
"""

import frappe


def execute():
    if not frappe.db.has_column("GMP Document", "workflow_status"):
        return

    frappe.db.sql("""
        UPDATE `tabGMP Document`
        SET workflow_status = 'Obsolete', is_active = 0
        WHERE docstatus = 2 AND workflow_status != 'Obsolete'
    """)
