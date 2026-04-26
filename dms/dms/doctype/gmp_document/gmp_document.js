// Copyright (c) 2026, ErenAydin- GMP DMS Module
// License: MIT

frappe.ui.form.on("GMP Document", {
    onload(frm) {
        toggle_reason_for_change(frm);
    },

    refresh(frm) {
        toggle_reason_for_change(frm);
        add_download_pdf_button(frm);
        render_compliance_dashboard(frm);
    },

    version_number(frm) {
        toggle_reason_for_change(frm);
    },

    amended_from(frm) {
        toggle_reason_for_change(frm);
    },
});


/**
 * Hide `reason_for_change` for first-version docs (v0) and mark it
 * mandatory whenever the doc is an amendment. These two rules don't
 * conflict because amended docs always have version_number > 0.
 */
function toggle_reason_for_change(frm) {
    const version = frm.doc.version_number || 0;
    const is_amended = Boolean(frm.doc.amended_from);

    // Show whenever the field is (or could be) mandatory. version_number is
    // bumped server-side in before_insert, so for a freshly-amended draft the
    // client still sees version=0 even though it will become 1 on save.
    frm.toggle_display("reason_for_change", version > 0 || is_amended);
    frm.toggle_reqd("reason_for_change", is_amended);
}


/**
 * Adds the "Download PDF" action under the standard "Get PDF" menu.
 * Visibility is gated by role on the client; the server-side whitelisted
 * method enforces read permission as the actual security boundary.
 */
function add_download_pdf_button(frm) {
    if (frm.is_new()) return;
    if (!frappe.user.has_role("QA Manager")) return;

    frm.add_custom_button(
        __("Download PDF"),
        () => download_watermarked_pdf(frm),
        __("Get PDF")
    );
}


/**
 * Renders a small status indicator showing the document's compliance state
 * (active/obsolete/draft) so QA users can see at a glance which watermark
 * the downloaded PDF will carry.
 */
function render_compliance_dashboard(frm) {
    if (frm.is_new()) return;

    let label, color;
    if (frm.doc.docstatus === 1 && frm.doc.is_active) {
        label = __("CONTROLLED COPY");
        color = "green";
    } else if (!frm.doc.is_active) {
        label = __("OBSOLETE");
        color = "red";
    } else {
        label = __("DRAFT - NOT FOR USE");
        color = "orange";
    }
    frm.dashboard.add_indicator(label, color);
}


async function download_watermarked_pdf(frm) {
    if (frm.is_dirty()) {
        frappe.msgprint({
            title: __("Unsaved Changes"),
            message: __("Please save the document before downloading the PDF."),
            indicator: "orange",
        });
        return;
    }

    const endpoint =
        "/api/method/dms.dms.doctype.gmp_document.gmp_document.download_watermarked_pdf";
    const url = `${endpoint}?docname=${encodeURIComponent(frm.doc.name)}`;

    frappe.dom.freeze(__("Generating watermarked PDF - please wait..."));

    try {
        const response = await fetch(url, {
            method: "GET",
            credentials: "same-origin",
            headers: {
                "X-Frappe-CSRF-Token": frappe.csrf_token || "",
                Accept: "application/pdf",
            },
        });

        if (!response.ok) {
            throw new Error(await extract_server_error(response));
        }

        const blob = await response.blob();
        const filename = parse_filename(
            response.headers.get("Content-Disposition"),
            `${frm.doc.name}.pdf`
        );

        trigger_browser_download(blob, filename);

        frappe.show_alert(
            { message: __("PDF downloaded successfully."), indicator: "green" },
            5
        );
    } catch (error) {
        frappe.msgprint({
            title: __("PDF Download Failed"),
            message: error.message || __("Could not generate the watermarked PDF."),
            indicator: "red",
        });
    } finally {
        frappe.dom.unfreeze();
    }
}


async function extract_server_error(response) {
    const fallback = __("Server returned status {0}.", [response.status]);
    try {
        const text = await response.text();
        const json = JSON.parse(text);
        if (json._server_messages) {
            const messages = JSON.parse(json._server_messages);
            return messages
                .map((m) => {
                    try { return JSON.parse(m).message; }
                    catch (e) { return m; }
                })
                .join("\n");
        }
        if (json.exception) return json.exception;
        return fallback;
    } catch (_e) {
        return fallback;
    }
}


function parse_filename(content_disposition, default_name) {
    if (!content_disposition) return default_name;
    const match = content_disposition.match(/filename\*?=(?:UTF-8'')?["']?([^"';\r\n]+)["']?/i);
    return match ? decodeURIComponent(match[1]) : default_name;
}


function trigger_browser_download(blob, filename) {
    const blob_url = window.URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = blob_url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    window.URL.revokeObjectURL(blob_url);
}
