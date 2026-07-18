# Copyright (c) 2026, ErenAydin - GMP DMS Module
# License: MIT
"""Per-department override row of DMS Settings.department_actors. An empty
actor field falls back to the global default on the parent single."""

from frappe.model.document import Document


class DMSDepartmentActors(Document):
    pass
