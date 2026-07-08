// Copyright (c) 2026, ErenAydin- GMP DMS Module
// License: MIT

frappe.ui.form.on("GMP Document", {
    onload(frm) {
        toggle_reason_for_change(frm);
    },

    refresh(frm) {
        toggle_reason_for_change(frm);
        add_download_pdf_button(frm);
        add_create_revision_button(frm);
        show_revision_banner(frm);
        show_pending_effective_banner(frm);
        toggle_effective_date_controls(frm);
        toggle_assignment_fields(frm);
        render_reference_tree(frm);
    },

    set_effective_date(frm) {
        // Unticking hands the field back to the system (the server normalizes
        // any manual value away on save, like ERPNext's posting date).
        if (!cint(frm.doc.set_effective_date) && frm.doc.docstatus === 0) {
            frm.set_value("effective_date", null);
        }
    },

    version_number(frm) {
        toggle_reason_for_change(frm);
    },

    amended_from(frm) {
        toggle_reason_for_change(frm);
    },

    revision_of(frm) {
        toggle_reason_for_change(frm);
    },

    reviewer(frm) {
        warn_if_no_signature(frm, "reviewer", __("Reviewer"));
    },

    qa_approver(frm) {
        warn_if_no_signature(frm, "qa_approver", __("QA Approver"));
    },
});


/**
 * Immediate feedback when a Reviewer / QA Approver is selected: if that user has
 * no usable signature image, warn the preparer right away. The hard enforcement
 * is server-side in GMPDocument._validate_signatures (blocks save/submit).
 */
function warn_if_no_signature(frm, fieldname, label) {
    const user = frm.doc[fieldname];
    if (!user) return;
    frappe.call({
        method: "dms.dms.doctype.gmp_document.gmp_document.check_signature",
        args: { user },
        callback(r) {
            if (r.message && r.message.ok === false) {
                frappe.msgprint({
                    title: __("Missing Signature"),
                    indicator: "red",
                    message: __(
                        "{0}: {1} Upload a PNG/JPG/JPEG signature on their Employee record before saving.",
                        [label, r.message.message]
                    ),
                });
            }
        },
    });
}


/**
 * Show and require `reason_for_change` only for revisions/amendments; hide it
 * on the initial version. Keyed off `revision_of`/`amended_from`, not the
 * version number: version numbering now starts at 1, so a version-based rule
 * would wrongly show the field on every first-version document.
 */
function toggle_reason_for_change(frm) {
    const is_revision = Boolean(frm.doc.amended_from || frm.doc.revision_of);

    frm.toggle_display("reason_for_change", is_revision);
    frm.toggle_reqd("reason_for_change", is_revision);
}


/**
 * Non-destructive revision entry point. Offered only on the current effective
 * version (Approved + submitted + active). The server re-enforces every guard
 * (create_revision -> before_insert), so this gate is UX only.
 */
function add_create_revision_button(frm) {
    if (frm.is_new()) return;
    if (frm.doc.docstatus !== 1) return;
    if (frm.doc.workflow_status !== "Approved" || !cint(frm.doc.is_active)) return;

    const can_author =
        frappe.user.has_role("QA Manager") ||
        frappe.user.has_role("DMS Manager") ||
        frappe.user.has_role("System Manager");
    if (!can_author) return;

    frm.add_custom_button(__("Create Revision"), () => {
        frappe.prompt(
            [
                {
                    fieldname: "reason_for_change",
                    fieldtype: "Small Text",
                    label: __("Reason for Change"),
                    reqd: 1,
                },
            ],
            (values) => {
                frappe.call({
                    method: "dms.dms.doctype.gmp_document.gmp_document.create_revision",
                    args: {
                        docname: frm.doc.name,
                        reason_for_change: values.reason_for_change,
                    },
                    freeze: true,
                    freeze_message: __("Creating draft revision..."),
                    callback(r) {
                        if (!r.message) return;
                        frappe.show_alert(
                            {
                                message: __(
                                    "Draft revision {0} created. {1} remains effective until it is approved.",
                                    [r.message, frm.doc.name]
                                ),
                                indicator: "green",
                            },
                            8
                        );
                        frappe.set_route("Form", "GMP Document", r.message);
                    },
                });
            },
            __("Start Revision of {0}", [frm.doc.name]),
            __("Create Draft Revision")
        );
    });
}


/**
 * ERPNext posting-date UX for the Effective Date: the checkbox that unlocks
 * manual entry is only offered to the roles the server accepts (QA/DMS/System
 * Manager) — for everyone else the date stays visibly system-controlled.
 * Server-side enforcement lives in _enforce_effective_date_policy().
 */
function toggle_effective_date_controls(frm) {
    const can_edit =
        frappe.user.has_role("QA Manager") ||
        frappe.user.has_role("DMS Manager") ||
        frappe.user.has_role("System Manager");
    // Keep the checkbox visible when already ticked so its effect is auditable.
    frm.toggle_display(
        "set_effective_date",
        can_edit || cint(frm.doc.set_effective_date)
    );
}


/**
 * Banner for a QA-approved document whose Effective Date lies in the future:
 * approved, but not the effective version until the date arrives.
 */
function show_pending_effective_banner(frm) {
    if (frm.is_new() || frm.doc.docstatus !== 1) return;
    if (cint(frm.doc.is_active)) return;
    if (frm.doc.workflow_status !== "Approved") return;
    if (!frm.doc.effective_date) return;

    frm.set_intro(
        __(
            "QA-approved, scheduled to become effective on {0}. Until then it is not the effective version{1}.",
            [
                frappe.datetime.str_to_user(frm.doc.effective_date),
                frm.doc.revision_of
                    ? " " + __("and {0} remains in force", [frm.doc.revision_of])
                    : "",
            ]
        ),
        "orange"
    );
}


/**
 * Orientation banner for the non-destructive revision flow: on a draft
 * revision, say which version stays effective; on a cancelled revision, say
 * the record is a retained audit artifact.
 */
function show_revision_banner(frm) {
    if (frm.is_new() || !frm.doc.revision_of) return;

    if (frm.doc.workflow_status === "Revision Cancelled") {
        frm.set_intro(
            __(
                "This revision was cancelled and is retained for audit. {0} remains the effective version.",
                [`<a href="/app/gmp-document/${encodeURIComponent(frm.doc.revision_of)}">${frm.doc.revision_of}</a>`]
            ),
            "red"
        );
    } else if (frm.doc.docstatus === 0) {
        frm.set_intro(
            __(
                "Draft revision of {0} — that version remains effective and unchanged until this revision is QA-approved.",
                [`<a href="/app/gmp-document/${encodeURIComponent(frm.doc.revision_of)}">${frm.doc.revision_of}</a>`]
            ),
            "blue"
        );
    }
}


/**
 * Adds the download actions under the standard "Get PDF" menu.
 *
 * - The watermarked controlled-copy PDF is offered to anyone who can read an
 *   approved (docstatus 1) document — including read-only department members.
 * - The clean Word file stays a manager-only control-distribution action.
 *
 * Buttons are gated on the client for UX; the whitelisted methods enforce the
 * real boundary (read permission for the PDF, manager role for the Word file).
 */
function add_download_pdf_button(frm) {
    if (frm.is_new()) return;
    if (frm.doc.docstatus !== 1) return;

    frm.add_custom_button(
        __("Download PDF (Controlled Copy)"),
        () => download_watermarked_pdf(frm),
        __("Get PDF")
    );

    frm.add_custom_button(
        __("Download PDF (Uncontrolled Copy)"),
        () => download_watermarked_pdf(frm, "uncontrolled"),
        __("Get PDF")
    );

    frm.add_custom_button(
        __("Download PDF (Plain)"),
        () => download_watermarked_pdf(frm, "plain"),
        __("Get PDF")
    );

    const is_manager =
        frappe.user.has_role("DMS Manager") ||
        frappe.user.has_role("QA Manager") ||
        frappe.user.has_role("System Manager");

    if (is_manager) {
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


async function download_watermarked_pdf(frm, variant) {
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
    let url = `${endpoint}?docname=${encodeURIComponent(frm.doc.name)}`;
    if (variant) {
        url += `&variant=${encodeURIComponent(variant)}`;
    }

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
    if (!frm.fields_dict.references_tree_html) return;

    // Always clear first: the form control is reused across navigation, so a
    // tree rendered for a previously-open document would otherwise linger in
    // the DOM when switching to a new (unsaved) record. Clearing before the
    // is_new() guard ensures new documents show an empty tree, not stale data.
    const wrapper = frm.fields_dict.references_tree_html.$wrapper;
    wrapper.empty();

    if (frm.is_new()) return;

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
