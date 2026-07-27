"""v1.4.0: the app no longer ships default GMP Document Types — the master is
user-maintained. Remove the rows the old seed created, but only where doing so
is provably safe: the code AND label still match the seed exactly (a relabelled
row is a user decision) and no GMP Document of any docstatus references the
type. Anything else is left untouched."""

import frappe

# Frozen copy of the retired install.py seed (label, code).
SEEDED_TYPES = [
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


def execute():
    if not frappe.db.exists("DocType", "GMP Document Type"):
        return

    for type_name, code in SEEDED_TYPES:
        if frappe.db.get_value("GMP Document Type", code, "type_name") != type_name:
            continue
        if frappe.db.exists("GMP Document", {"document_type": code}):
            continue
        frappe.delete_doc(
            "GMP Document Type", code,
            ignore_permissions=True, ignore_missing=True,
        )
