"""Backfill workflow_status and prepared_by for GMP Documents that
existed before the workflow feature shipped (v0.3.0)."""

import frappe


def execute():
    if not frappe.db.has_column("GMP Document", "workflow_status"):
        return

    # Submitted docs are Approved by definition; everything else stays Draft.
    frappe.db.sql("""
        UPDATE `tabGMP Document`
        SET workflow_status = CASE
            WHEN docstatus = 1 THEN 'Approved'
            ELSE 'Draft'
        END
        WHERE workflow_status IS NULL OR workflow_status = ''
    """)

    # prepared_by defaults to the doc's owner if not set explicitly.
    frappe.db.sql("""
        UPDATE `tabGMP Document`
        SET prepared_by = owner
        WHERE prepared_by IS NULL OR prepared_by = ''
    """)
