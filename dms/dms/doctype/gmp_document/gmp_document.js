// Copyright (c) 2026, ErenAydin- GMP DMS Module
// License: MIT

frappe.ui.form.on("GMP Document", {
    onload(frm) {
        toggle_reason_for_change(frm);
    },

    refresh(frm) {
        render_approval_progress(frm);
        toggle_reason_for_change(frm);
        add_download_pdf_button(frm);
        add_create_revision_button(frm);
        show_revision_banner(frm);
        show_pending_effective_banner(frm);
        show_delegated_approval_banner(frm);
        toggle_effective_date_controls(frm);
        add_qa_queue_ui(frm);
        render_reference_tree(frm);
        show_stored_file_name(frm);
    },

    set_effective_date(frm) {
        // Unticking hands the field back to the system (the server normalizes
        // any manual value away on save, like ERPNext's posting date).
        if (!cint(frm.doc.set_effective_date) && frm.doc.docstatus === 0) {
            frm.set_value("effective_date", null);
        }
    },

    before_workflow_action(frm) {
        return ask_return_reason(frm);
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
 * Every Return action asks why, and stores the answer before the action runs:
 * apply_workflow reloads the document from the database, so the reason has to
 * be saved by its own call first (set_return_reason). Cancelling the prompt
 * cancels the action.
 */
function ask_return_reason(frm) {
    const action = frm.selected_workflow_action || "";
    const workflow = frappe.workflow.workflows[frm.doctype];
    const transition = ((workflow && workflow.transitions) || []).find(
        (t) => t.state === frm.doc.workflow_status && t.action === action
    );
    const is_return =
        action.startsWith("Return") ||
        (transition && transition.next_state === "Revision Requested");
    if (!is_return) return;

    // Frappe freezes the screen before this event; the dialog must sit on top.
    frappe.dom.unfreeze();
    return new Promise((resolve, reject) => {
        let answered = false;
        const dialog = frappe.prompt(
            [
                {
                    fieldname: "reason",
                    fieldtype: "Small Text",
                    label: __("Reason for returning"),
                    reqd: 1,
                },
            ],
            (values) => {
                answered = true;
                frappe.dom.freeze();
                frappe
                    .xcall("dms.dms.doctype.gmp_document.gmp_document.set_return_reason", {
                        docname: frm.doc.name,
                        reason: values.reason,
                    })
                    .then(resolve)
                    .catch((err) => {
                        frappe.dom.unfreeze();
                        reject(err);
                    });
            },
            __(action),
            __("Return")
        );
        dialog.onhide = () => {
            if (!answered) reject();
        };
    });
}


/**
 * The source template is kept in the DMS storage and served by document_file(),
 * so the field holds that endpoint's URL; show just the file name it serves.
 */
function show_stored_file_name(frm) {
    const control = frm.fields_dict.attachment_file;
    const url = frm.doc.attachment_file || "";
    if (!control || !control.$value || !url.startsWith(STORED_FILE_ENDPOINT + "?")) return;
    const file = new URLSearchParams(url.split("?")[1]).get("file");
    if (file) control.$value.find(".attached-file-link").text(file);
}

const STORED_FILE_ENDPOINT = "/api/method/dms.dms.doctype.gmp_document.gmp_document.document_file";


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

    // The clicker becomes the draft's preparer, so offer the button only to
    // someone the workflow lets submit a draft (the server checks the same).
    const workflow = frappe.workflow.workflows[frm.doctype];
    const can_author = workflow
        ? (workflow.transitions || []).some(
              (t) => t.state === "Draft" && frappe.user.has_role(t.allowed)
          )
        : frappe.user.has_role("DMS Initiator");
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
 * Stages a DMS Proxy Approver can have performed for the assigned role-holder.
 * [on-behalf-of field, actual-actor field, stage date field, label]
 */
const DELEGATED_STAGES = [
    ["supervisor_on_behalf_of", "supervisor_approved_by", "supervisor_date", "Supervisor"],
    ["reviewer_on_behalf_of", "reviewed_by", "reviewer_date", "Reviewer"],
    ["qa_supervisor_on_behalf_of", "qa_supervisor_approved_by", "qa_supervisor_date", "QA Supervisor"],
    ["regulatory_on_behalf_of", "regulatory_validated_by", "regulatory_date", "Regulatory"],
    ["manager_on_behalf_of", "manager_approved_by", "manager_date", "Manager"],
    ["qa_approver_on_behalf_of", "approved_by", "qa_approver_date", "QA Approver"],
];


/**
 * Surface delegated approvals on the document itself, not just in the
 * collapsed section: a reader must be able to see at a glance that a stage was
 * signed by a stand-in, who they stood in for, and when. Rendered as a
 * dashboard comment rather than frm.set_intro so it coexists with the revision
 * and pending-effective banners instead of replacing them.
 */
function show_delegated_approval_banner(frm) {
    if (frm.is_new()) return;

    const lines = DELEGATED_STAGES.filter(([on_behalf]) => frm.doc[on_behalf]).map(
        ([on_behalf, actor, date, label]) =>
            __("{0}: {1} on behalf of {2}{3}", [
                __(label),
                frm.doc[actor] || __("(unknown)"),
                frm.doc[on_behalf],
                frm.doc[date] ? ` — ${frappe.datetime.str_to_user(frm.doc[date])}` : "",
            ])
    );
    if (!lines.length) return;

    frm.dashboard.add_comment(
        `${__("Delegated approval — performed by a proxy approver:")}<br>${lines.join("<br>")}`,
        "orange",
        true
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
 * - The watermarked PDF is offered to anyone who can read an approved
 *   (docstatus 1) document — including read-only department members.
 * - The clean Word file stays a manager-only control-distribution action.
 *
 * Buttons are gated on the client for UX; the whitelisted methods enforce the
 * real boundary (read permission for the PDF, manager role for the Word file).
 */
function add_download_pdf_button(frm) {
    if (frm.is_new()) return;
    // Approved, or cancelled (the server stamps a cancelled copy OBSOLETE).
    if (frm.doc.docstatus === 0) return;

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

// -------------------------------------------------------------------- //
//  Sequential QA review queue (delegate / complete / skip)             //
// -------------------------------------------------------------------- //
// All actors are resolved server-side when the preparer submits for
// approval; the queue below is the QA Supervisor's delegation tool. The
// hard authorization lives in the whitelisted endpoints — these buttons
// are convenience UI for the users allowed to act.

const WF_PENDING_QA_SUPERVISOR = 'Pending QA Supervisor';
const WF_QA_IN_PROGRESS = 'QA Review In Progress';


function add_qa_queue_ui(frm) {
    if (frm.is_new() || frm.doc.docstatus !== 0) return;
    const user = frappe.session.user;
    const is_qa_supervisor =
        user === frm.doc.qa_supervisor ||
        user === 'Administrator' ||
        frappe.user.has_role('DMS Manager');

    if (frm.doc.workflow_status === WF_PENDING_QA_SUPERVISOR && is_qa_supervisor) {
        frm.add_custom_button(__('Delegate QA Review'), () => open_delegate_dialog(frm), __('QA Queue'));
    }

    if (frm.doc.workflow_status !== WF_QA_IN_PROGRESS) return;

    const head = (frm.doc.qa_reviews || []).find((r) => r.status === 'Awaiting Review');
    if (!head) return;

    frm.set_intro(
        __('QA review in progress — currently awaiting {0}.', [frappe.utils.escape_html(head.reviewer)]),
        'blue'
    );

    if (user === head.reviewer || user === 'Administrator') {
        frm.add_custom_button(__('Approve My QA Review'), () => complete_review_dialog(frm, 'Approve'), __('QA Queue'));
        frm.add_custom_button(__('Return to QA Supervisor'), () => complete_review_dialog(frm, 'Return'), __('QA Queue'));
    }
    if (is_qa_supervisor) {
        frm.add_custom_button(__('Skip Current Reviewer'), () => skip_reviewer_dialog(frm, head), __('QA Queue'));
    }
}


function open_delegate_dialog(frm) {
    const d = new frappe.ui.Dialog({
        title: __('Delegate Sequential QA Review'),
        fields: [
            {
                fieldtype: 'HTML',
                options: `<p class="text-muted">${__(
                    'Reviewers act one after the other, in this order. The document moves to the Manager only after every listed reviewer has completed (or been skipped with a reason).'
                )}</p>`,
            },
            { fieldname: 'reviewer_1', fieldtype: 'Link', options: 'User', label: __('Reviewer 1'), reqd: 1 },
            { fieldname: 'reviewer_2', fieldtype: 'Link', options: 'User', label: __('Reviewer 2') },
            { fieldname: 'reviewer_3', fieldtype: 'Link', options: 'User', label: __('Reviewer 3') },
        ],
        primary_action_label: __('Delegate'),
        primary_action(values) {
            const reviewers = [values.reviewer_1, values.reviewer_2, values.reviewer_3].filter(Boolean);
            frappe.call({
                method: 'dms.dms.doctype.gmp_document.gmp_document.delegate_qa_review',
                args: { docname: frm.doc.name, reviewers },
                freeze: true,
                freeze_message: __('Delegating…'),
                callback() {
                    d.hide();
                    frm.reload_doc();
                },
            });
        },
    });
    d.show();
}


function complete_review_dialog(frm, decision) {
    const d = new frappe.ui.Dialog({
        title: decision === 'Approve' ? __('Approve QA Review') : __('Return to QA Supervisor'),
        fields: [
            {
                fieldname: 'comments',
                fieldtype: 'Small Text',
                label: __('Comments'),
                reqd: decision === 'Return' ? 1 : 0,
                description:
                    decision === 'Return'
                        ? __('A reason is mandatory when returning the document.')
                        : __('Optional remarks, recorded on the review row and the timeline.'),
            },
        ],
        primary_action_label: decision === 'Approve' ? __('Approve') : __('Return'),
        primary_action(values) {
            frappe.call({
                method: 'dms.dms.doctype.gmp_document.gmp_document.complete_qa_review',
                args: { docname: frm.doc.name, decision, comments: values.comments || '' },
                freeze: true,
                callback() {
                    d.hide();
                    frm.reload_doc();
                },
            });
        },
    });
    d.show();
}


function skip_reviewer_dialog(frm, head) {
    const d = new frappe.ui.Dialog({
        title: __('Skip Current Reviewer'),
        fields: [
            {
                fieldtype: 'HTML',
                options: `<p>${__('The task currently sits with {0}. Skipping hands it to the next reviewer in the queue and is logged with your reason.', [
                    frappe.utils.escape_html(head.reviewer),
                ])}</p>`,
            },
            { fieldname: 'reason', fieldtype: 'Small Text', label: __('Reason'), reqd: 1 },
        ],
        primary_action_label: __('Skip'),
        primary_action(values) {
            frappe.call({
                method: 'dms.dms.doctype.gmp_document.gmp_document.skip_qa_reviewer',
                args: { docname: frm.doc.name, reason: values.reason },
                freeze: true,
                callback() {
                    d.hide();
                    frm.reload_doc();
                },
            });
        },
    });
    d.show();
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

// -------------------------------------------------------------------- //
//  Approval progress bar (top of the form)                             //
// -------------------------------------------------------------------- //
// Every stage of the chain with its person: done (green, with who and
// when), current (blue, with how long it has been waiting) and upcoming
// (grey). The server works out the stages (get_approval_progress); this
// only draws them. The bar follows the page direction, so it reads right
// to left in Persian, and turns into a vertical list on a phone.

function render_approval_progress(frm) {
    const field = frm.fields_dict.approval_progress_html;
    if (!field) return;
    // Clear first: the control is reused when moving between documents.
    field.$wrapper.empty();
    if (frm.is_new()) return;

    const docname = frm.doc.name;
    frappe.call({
        method: "dms.dms.doctype.gmp_document.gmp_document.get_approval_progress",
        args: { docname },
        type: "GET",
        callback(r) {
            // The user may have moved on to another document meanwhile.
            if (!r.message || frm.doc.name !== docname) return;
            ensure_progress_style();
            field.$wrapper.html(progress_html(r.message));
        },
    });
}


const PROGRESS_ICON = { done: "✓", current: "●", upcoming: "", skipped: "" };
const QUEUE_ICON = { Approved: "✓", "Awaiting Review": "●", Queued: "○", Skipped: "↷", "Return Requested": "↩" };


function progress_html(data) {
    const esc = frappe.utils.escape_html;
    // <bdi> keeps "13-10-2026 23:46" in order inside a right-to-left page.
    const when = (value) => (value ? `<bdi>${esc(frappe.datetime.str_to_user(value))}</bdi>` : "");

    const steps = data.steps.map((step) => {
        let who = esc(step.user_name || step.user || "—");
        if (step.on_behalf_of) {
            who = __("{0} for {1}", [who, esc(step.on_behalf_of_name || step.on_behalf_of)]);
        }
        let detail = "";
        if (step.status === "done") {
            detail = when(step.at);
        } else if (step.status === "current" && data.pending && data.pending.since) {
            detail = __("waiting {0}", [esc(frappe.datetime.prettyDate(data.pending.since, true))]);
        } else if (step.status === "skipped") {
            detail = __("not reached");
        }
        return `
            <div class="dms-step ${step.status}">
                <div class="dms-dot">${PROGRESS_ICON[step.status]}</div>
                <div class="dms-step-text">
                    <div class="dms-step-label">${esc(step.label)}</div>
                    <div class="dms-step-user">${who}</div>
                    <div class="dms-step-when">${detail}</div>
                </div>
            </div>`;
    });

    const end = {
        approved: ["done", "✓", __("Approved"), data.effective_date
            ? __("effective {0}", [when(data.effective_date)]) : ""],
        pending_effective: ["scheduled", "⏳", __("Approved"), data.effective_date
            ? __("effective from {0}", [when(data.effective_date)]) : ""],
        obsolete: ["ended", "✕", __("Obsolete"), ""],
        cancelled: ["ended", "✕", __("Revision Cancelled"), ""],
    }[data.end] || ["upcoming", "", __("Approved"), ""];
    steps.push(`
        <div class="dms-step ${end[0]}">
            <div class="dms-dot">${end[1]}</div>
            <div class="dms-step-text">
                <div class="dms-step-label">${end[2]}</div>
                <div class="dms-step-when">${end[3]}</div>
            </div>
        </div>`);

    const notes = [];
    if (data.pending) {
        const since = data.pending.since;
        notes.push(`<div class="dms-progress-note waiting">⏳ ${__("Waiting on {0}", [
            `<b>${esc(data.pending.user_name || data.pending.user)}</b>`,
        ])}${since ? ` · ${__("since {0}", [`${when(since)} (${esc(frappe.datetime.prettyDate(since))})`])}` : ""}</div>`);
    }
    if (data.returned) {
        notes.push(`<div class="dms-progress-note returned">↩ ${__("Returned by {0} on {1}", [
            `<b>${esc(data.returned.user_name || data.returned.user || "")}</b>`,
            when(data.returned.at),
        ])}: ${esc(data.returned.reason || "")}</div>`);
    }
    if (data.qa_queue && data.qa_queue.length) {
        const rows = data.qa_queue.map((row) => {
            const icon = QUEUE_ICON[row.status] || "○";
            const at = row.at ? ` (${when(row.at)})` : "";
            return `<span class="dms-queue-item">${icon} ${esc(row.user_name || row.user)}${at}</span>`;
        });
        notes.push(`<div class="dms-progress-note">${__("QA review queue")}: ${rows.join(" → ")}</div>`);
    }

    return `
        <div class="dms-progress">
            <div class="dms-steps">${steps.join("")}</div>
            ${notes.join("")}
        </div>`;
}


function ensure_progress_style() {
    if (document.getElementById("dms-progress-style")) return;
    const style = document.createElement("style");
    style.id = "dms-progress-style";
    style.textContent = `
        .dms-progress { padding: 4px 0 8px; }
        .dms-progress .dms-steps { display: flex; align-items: flex-start; overflow-x: auto; padding-bottom: 6px; }
        .dms-progress .dms-step { position: relative; flex: 1 0 112px; display: flex; flex-direction: column;
            align-items: center; text-align: center; padding: 0 4px; }
        .dms-progress .dms-step + .dms-step::before { content: ""; position: absolute; top: 13px;
            inset-inline-end: calc(50% + 15px); width: calc(100% - 30px); height: 2px;
            background: var(--border-color, #d1d8dd); }
        .dms-progress .dms-step.done::before, .dms-progress .dms-step.current::before,
        .dms-progress .dms-step.scheduled::before, .dms-progress .dms-step.ended::before {
            background: var(--green-500, #2e9e5b); }
        .dms-progress .dms-dot { width: 28px; height: 28px; border-radius: 50%; display: flex;
            align-items: center; justify-content: center; font-size: 14px; font-weight: 700;
            border: 2px solid var(--gray-400, #b8c2cc); background: var(--fg-color, #fff); color: #fff; }
        .dms-progress .done .dms-dot { background: var(--green-500, #2e9e5b); border-color: var(--green-500, #2e9e5b); }
        .dms-progress .current .dms-dot { border-color: var(--blue-500, #2490ef); color: var(--blue-500, #2490ef);
            box-shadow: 0 0 0 4px var(--blue-100, #d6eaff); }
        .dms-progress .scheduled .dms-dot { background: var(--orange-500, #e8790c); border-color: var(--orange-500, #e8790c); }
        .dms-progress .ended .dms-dot { background: var(--red-500, #e24c4c); border-color: var(--red-500, #e24c4c); }
        .dms-progress .skipped .dms-dot { border-style: dashed; }
        .dms-progress .dms-step-label { margin-top: 6px; font-size: 12px; font-weight: 600; color: var(--text-color); }
        .dms-progress .upcoming .dms-step-label, .dms-progress .skipped .dms-step-label { color: var(--text-muted); }
        .dms-progress .dms-step-user { font-size: 12px; color: var(--text-color); }
        .dms-progress .upcoming .dms-step-user, .dms-progress .skipped .dms-step-user { color: var(--text-muted); }
        .dms-progress .dms-step-when { font-size: 11px; color: var(--text-muted); }
        .dms-progress .current .dms-step-when { color: var(--blue-500, #2490ef); font-weight: 600; }
        .dms-progress .dms-progress-note { margin-top: 6px; font-size: 12px; color: var(--text-color); }
        .dms-progress .dms-progress-note.waiting { color: var(--blue-600, #1c75c8); }
        .dms-progress .dms-progress-note.returned { color: var(--red-600, #c93232); }
        .dms-progress .dms-queue-item { white-space: nowrap; }
        @media (max-width: 640px) {
            .dms-progress .dms-steps { flex-direction: column; overflow: visible; }
            .dms-progress .dms-step { flex: none; flex-direction: row; align-items: flex-start; text-align: start;
                gap: 10px; padding: 0 0 12px; }
            .dms-progress .dms-step + .dms-step::before { display: none; }
            .dms-progress .dms-dot { flex: none; }
            .dms-progress .dms-step-label { margin-top: 0; }
        }`;
    document.head.appendChild(style);
}


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
