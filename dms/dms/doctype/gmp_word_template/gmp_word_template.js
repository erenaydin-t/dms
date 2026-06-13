// Copyright (c) 2026, ErenAydin - GMP DMS Module
// License: MIT

frappe.ui.form.on("GMP Word Template", {
    refresh(frm) {
        refresh_system_field_options(frm);

        if (!frm.is_new() && frm.doc.template_file) {
            frm.add_custom_button(__("Scan Template Tags"), () => scan_template_tags(frm));
        }
    },

    template_file(frm) {
        // Encourage a re-scan whenever the source file changes.
        if (frm.doc.template_file && !frm.is_new()) {
            frappe.show_alert(
                { message: __("Template changed — run 'Scan Template Tags' to refresh the mapping."), indicator: "blue" },
                7
            );
        }
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


function scan_template_tags(frm) {
    if (frm.is_dirty()) {
        frappe.msgprint({
            title: __("Unsaved Changes"),
            message: __("Please save the template before scanning its tags."),
            indicator: "orange",
        });
        return;
    }

    frappe.call({
        method: "dms.dms.doctype.gmp_word_template.gmp_word_template.scan_template_tags",
        args: { template: frm.doc.name },
        freeze: true,
        freeze_message: __("Scanning template…"),
        callback(r) {
            if (!r.message) return;
            const { tags, already_mapped, suggestions } = r.message;

            if (!tags.length) {
                frappe.msgprint({
                    title: __("No Tags Found"),
                    message: __("No {{ tags }} were detected in this template."),
                    indicator: "orange",
                });
                return;
            }

            const existing = new Set(already_mapped);
            const new_tags = tags.filter((t) => !existing.has(t));

            new_tags.forEach((tag) => {
                const row = frm.add_child("field_mappings", { custom_tag: tag });
                if (suggestions[tag]) row.system_field = suggestions[tag];
            });
            frm.refresh_field("field_mappings");

            const unmapped = tags.filter(
                (t) => !existing.has(t) && !suggestions[t]
            );
            let msg = __("Detected {0} tag(s). Added {1} new row(s).", [
                tags.length,
                new_tags.length,
            ]);
            if (unmapped.length) {
                msg +=
                    "<br><br><b>" +
                    __("Choose a system field for:") +
                    "</b><br>" +
                    unmapped.map((t) => frappe.utils.escape_html(t)).join(", ");
            }
            frappe.msgprint({
                title: __("Tag Scan Complete"),
                message: msg,
                indicator: unmapped.length ? "orange" : "green",
            });
        },
    });
}
