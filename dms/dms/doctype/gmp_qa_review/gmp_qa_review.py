# Copyright (c) 2026, ErenAydin - GMP DMS Module
# License: MIT
"""Child row of GMP Document.qa_reviews: one entry per delegated QA reviewer
in the sequential review queue. Rows are appended and mutated exclusively by
the queue engine in gmp_document.py (delegate_qa_review / complete_qa_review /
skip_qa_reviewer); they are never edited through the grid and never deleted —
superseded rounds are retained as the audit record of the delegation history."""

from frappe.model.document import Document


class GMPQAReview(Document):
    pass
