// Copyright (c) 2026, ErenAydin- GMP DMS Module
// License: MIT

frappe.ui.form.on("GMP Document", {
    onload(frm) {
        toggle_reason_for_change(frm);
    },

    refresh(frm) {
        toggle_reason_for_change(frm);
        add_download_pdf_button(frm);
        toggle_assignment_fields(frm);
        render_reference_tree(frm);
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
        __("Download PDF (signed)"),
        () => download_watermarked_pdf(frm),
        __("Get PDF")
    );

    if (frm.doc.docstatus === 1) {
        frm.add_custom_button(
            __("Download Word (clean)"),
            () => download_word_document(frm),
            __("Get PDF")
        );
    }
}


async function download_word_document(frm) {
    if (frm.is_dirty()) {
        frappe.msgprint({
            title: __("Unsaved Changes"),
            message: __("Please save the document before downloading."),
            indicator: "orange",
        });
        return;
    }

    const endpoint =
        "/api/method/dms.dms.doctype.gmp_document.gmp_document.download_word_document";
    const url = `${endpoint}?docname=${encodeURIComponent(frm.doc.name)}`;

    frappe.dom.freeze(__("Preparing Word file…"));

    try {
        const response = await fetch(url, {
            method: "GET",
            credentials: "same-origin",
            headers: {
                "X-Frappe-CSRF-Token": frappe.csrf_token || "",
            },
        });

        if (!response.ok) {
            throw new Error(await extract_server_error(response));
        }

        const blob = await response.blob();
        const filename = parse_filename(
            response.headers.get("Content-Disposition"),
            `${frm.doc.name}.docx`
        );

        trigger_browser_download(blob, filename);

        frappe.show_alert(
            { message: __("Word file downloaded."), indicator: "green" },
            5
        );
    } catch (error) {
        frappe.msgprint({
            title: __("Download Failed"),
            message: error.message || __("Could not retrieve the Word file."),
            indicator: "red",
        });
    } finally {
        frappe.dom.unfreeze();
    }
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


// -------------------------------------------------------------------- //
//  Workflow                                                             //
// -------------------------------------------------------------------- //
//
// Transitions are driven by Frappe's native Workflow engine via the form
// "Actions" menu — there are deliberately no custom workflow buttons or
// status indicators here. The single authoritative lifecycle indicator is
// the native workflow-state badge in the form header. The only workflow
// helper that remains is the assignment lock below.

const WF_DRAFT = 'Draft';
const WF_REVISION = 'Revision Requested';


function toggle_assignment_fields(frm) {
    // Reviewer / QA Approver are editable only while the doc is sitting
    // with the preparer (Draft or Revision Requested). Once it's in the
    // pipeline the assignments are locked.
    const status = frm.doc.workflow_status;
    const editable = !status || status === WF_DRAFT || status === WF_REVISION;
    frm.toggle_enable('reviewer', editable);
    frm.toggle_enable('qa_approver', editable);
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


// -------------------------------------------------------------------- //
//  Document Reference Tree                                             //
// -------------------------------------------------------------------- //

function render_reference_tree(frm) {
    if (frm.is_new()) return;
    if (!frm.fields_dict.references_tree_html) return;

    const wrapper = frm.fields_dict.references_tree_html.$wrapper;
    wrapper.empty();

    frappe.call({
        method: 'dms.dms.doctype.gmp_document.gmp_document.get_document_reference_tree',
        args: { docname: frm.doc.name, depth: 4 },
        callback(r) {
            if (!r.message) return;
            const container = $('<div class="dms-ref-tree" style="padding:8px 0;"></div>');
            _render_tree_node(r.message, container, 0);
            wrapper.append(container);
        },
    });
}


function _render_tree_node(node, parent_el, depth) {
    const indent = depth * 20;
    const has_children = node.children && node.children.length > 0;

    const row = $(`
        <div style="margin-left:${indent}px; padding:4px 0; display:flex; align-items:center; gap:8px;">
            <span style="color:#888; font-size:12px;">${has_children ? '▶' : '•'}</span>
            <a href="/app/gmp-document/${encodeURIComponent(node.name)}" target="_blank"
               style="font-size:13px; font-weight:500;">${frappe.utils.escape_html(node.label)}</a>
            ${node.reference_type
                ? `<span style="font-size:11px; color:#666; background:#f0f0f0; padding:1px 6px; border-radius:3px;">${frappe.utils.escape_html(node.reference_type)}</span>`
                : ''}
        </div>
    `);

    parent_el.append(row);

    if (has_children) {
        const children_container = $('<div class="dms-tree-children"></div>');
        parent_el.append(children_container);

        row.find('span:first').css('cursor', 'pointer').on('click', function () {
            children_container.toggle();
            $(this).text(children_container.is(':visible') ? '▼' : '▶');
        });

        node.children.forEach(child => {
            _render_tree_node(child, children_container, depth + 1);
        });
    }
}
