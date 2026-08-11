# Copyright (c) 2026, ErenAydin - GMP DMS Module
# License: MIT
"""v2.6.0: populate the new approval-timeline Date fields on documents that
were already in flight, so reports built on them are not blind to history.

Each stage's Date mirror is derived from the precise timestamp that stage has
always recorded (`reviewed_on` -> `reviewer_date`, and so on). Two stages have
no historical source and are therefore left blank rather than guessed at:

  preparer_date     — `submitted_on` is new in this release; nothing recorded
                      when a draft entered the chain before it.
  qa_supervisor_date— the QA Supervisor step stamped no actor or timestamp at
                      all before this release (that gap is what the release
                      fixes), so there is nothing to derive from.

`publish_date` is backfilled from `effective_date` for documents that are
already the effective version: for those, the day they entered force is the
best record available, and it is the value on_submit would have written.

The CEO block is deliberately NOT backfilled. It is stamped at publication
from DMS Settings, and inventing a CEO authorization for documents that were
approved without one would fabricate a record — exactly what a controlled
document must never do. Already-approved documents therefore show an empty
CEO block until they are next revised.

Idempotent: only ever fills a field that is still NULL.
"""

import frappe

# (source timestamp field, Date field to fill)
_DERIVED = (
    ("supervisor_approved_on", "supervisor_date"),
    ("reviewed_on", "reviewer_date"),
    ("regulatory_validated_on", "regulatory_date"),
    ("manager_approved_on", "manager_date"),
    ("approved_on", "qa_approver_date"),
)


def execute():
    for source, target in _DERIVED:
        frappe.db.sql(
            f"""
            UPDATE `tabGMP Document`
               SET `{target}` = DATE(`{source}`)
             WHERE `{target}` IS NULL
               AND `{source}` IS NOT NULL
            """
        )

    # Effective, submitted documents were published on their effective date.
    frappe.db.sql(
        """
        UPDATE `tabGMP Document`
           SET `publish_date` = `effective_date`
         WHERE `publish_date` IS NULL
           AND `effective_date` IS NOT NULL
           AND `docstatus` = 1
           AND `is_active` = 1
        """
    )
