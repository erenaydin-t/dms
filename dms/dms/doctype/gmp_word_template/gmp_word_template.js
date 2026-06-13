// Copyright (c) 2026, ErenAydin - GMP DMS Module
// License: MIT

frappe.ui.form.on("GMP Word Template", {
    refresh(frm) {
        refresh_system_field_options(frm);
    },
});


/**
 * Populate the child-table `system_field` Select from the live Python catalog
 * so the options never drift from _build_template_context().
 */
function refresh_system_field_options(frm) {
    frappe.call({
        method: "dms.dms.doctype.gmp_document.gmp_document.get_template_field_catalog",
        callback(r) {
            if (!r.message) return;
            const options = [""].concat(r.message.map((f) => f.value));
            frappe.meta.get_docfield(
                "GMP Template Field Mapping",
                "system_field",
                frm.doc.name
            ).options = options.join("\n");
            frm.fields_dict.field_mappings.grid.refresh();
        },
    });
}
