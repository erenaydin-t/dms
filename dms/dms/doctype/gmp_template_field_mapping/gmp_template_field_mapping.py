# Copyright (c) 2026, ErenAydin - GMP DMS Module
# License: MIT
"""GMP Template Field Mapping (child table).

One row maps a custom Word tag (``custom_tag``, used as ``{{ custom_tag }}`` in
the template) to a system value (``system_field``, a key from
gmp_document.TEMPLATE_FIELDS). Applied as an alias at render time by
GMPDocument._build_template_context(). Validation lives on the parent
(GMP Word Template) so a row's identifier/catalog checks see sibling rows.
"""

from frappe.model.document import Document


class GMPTemplateFieldMapping(Document):
    pass
