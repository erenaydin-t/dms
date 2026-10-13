// Copyright (c) 2026, ErenAydin - GMP DMS Module
// License: MIT

frappe.ui.form.on("DMS Settings", {
    refresh(frm) {
        show_storage_summary(frm);

        if (!frappe.user.has_role("System Manager") && !frappe.user.has_role("DMS Manager")) return;
        frm.add_custom_button(__("Move Stored Files"), () => {
            if (frm.is_dirty()) {
                frappe.msgprint(__("Save the settings first."));
                return;
            }
            const target = cint(frm.doc.store_files_on_s3) ? "S3" : __("this server's disk");
            frappe.confirm(
                __(
                    "Move every stored GMP Document file to {0}? Each file is copied, checked against its recorded SHA-256, and only then removed from its old place. This runs in the background; you get a message when it is done.",
                    [target]
                ),
                () =>
                    frappe
                        .xcall("dms.dms.doctype.dms_settings.dms_settings.move_stored_files")
                        .then(() =>
                            frappe.show_alert(
                                { message: __("Moving stored files in the background."), indicator: "blue" },
                                8
                            )
                        )
            );
        });
    },
});


function show_storage_summary(frm) {
    const wrapper = frm.fields_dict.storage_summary && frm.fields_dict.storage_summary.$wrapper;
    if (!wrapper) return;
    wrapper.empty();
    if (!frappe.user.has_role("System Manager") && !frappe.user.has_role("DMS Manager")) return;

    frappe
        .xcall("dms.dms.doctype.dms_settings.dms_settings.get_storage_summary")
        .then(({ target, summary, s3_unavailable }) => {
            const rows = Object.entries(summary || {}).map(
                ([where, stats]) =>
                    `<tr><td>${frappe.utils.escape_html(where)}</td><td>${stats.files}</td><td>${frappe.form.formatters.FileSize(stats.bytes)}</td></tr>`
            );
            const elsewhere = Object.entries(summary || {})
                .filter(([where]) => where !== target)
                .reduce((n, [, stats]) => n + stats.files, 0);
            wrapper.html(`
                <table class="table table-bordered table-sm" style="max-width: 420px;">
                    <thead><tr><th>${__("Storage")}</th><th>${__("Files")}</th><th>${__("Size")}</th></tr></thead>
                    <tbody>${rows.join("") || `<tr><td colspan="3" class="text-muted">${__("No files stored yet.")}</td></tr>`}</tbody>
                </table>
                ${
                    elsewhere
                        ? `<p class="text-warning">${__("{0} files are not yet in the selected storage. Click Move Stored Files to move them.", [elsewhere])}</p>`
                        : ""
                }
                ${s3_unavailable ? `<p class="text-muted">${__("S3: {0}", [frappe.utils.escape_html(s3_unavailable)])}</p>` : ""}
            `);
        });
}
