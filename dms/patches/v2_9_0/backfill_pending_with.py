# Copyright (c) 2026, ErenAydin - GMP DMS Module
# License: MIT
"""2.9.0: fill "Waiting On" / "Waiting Since" on the documents in progress.

The fields are kept up to date at every workflow step from now on; documents
already in the approval chain get the person they wait on now, and as the time
the wait started the latest workflow event in their timeline (the last
transition, delegation or hand-over), else their last modification."""

import frappe
from frappe.query_builder.functions import Max

from dms.dms.doctype.gmp_document.gmp_document import _full_names


def execute():
    for name in frappe.get_all("GMP Document", filters={"docstatus": 0}, pluck="name"):
        doc = frappe.get_doc("GMP Document", name)
        who = doc._pending_actor()
        if not who:
            continue
        comment = frappe.qb.DocType("Comment")
        since = (
            frappe.qb.from_(comment)
            .select(Max(comment.creation))
            .where(
                (comment.reference_doctype == "GMP Document")
                & (comment.reference_name == name)
                & (comment.comment_type == "Workflow")
            )
        ).run()[0][0]
        doc.db_set(
            {
                "pending_with": who,
                "pending_with_name": _full_names([who]).get(who),
                "pending_since": since or doc.modified,
            },
            update_modified=False,
        )
