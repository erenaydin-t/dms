import frappe


def execute():
    """Cast doc_status from string to integer for GMP Document Workflow states.

    Frappe v16 apply_workflow() compares state doc_status (int) with
    document docstatus (int). The install script previously inserted
    doc_status as strings ('0', '1'), causing auto-submit to silently fail
    on the 'Approved' transition.
    """
    if not frappe.db.exists("Workflow", "GMP Document Workflow"):
        return

    frappe.db.sql("""
        UPDATE `tabWorkflow State`
        SET `doc_status` = CAST(`doc_status` AS SIGNED)
        WHERE `parent` = 'GMP Document Workflow'
    """)
    frappe.db.commit()
