// Copyright (c) 2026, ErenAydin - GMP DMS Module
// License: MIT

frappe.listview_settings['GMP Document'] = {
    add_fields: [
        'is_active',
        'docstatus',
        'version_number',
        'document_type',
        'workflow_status',
        'reviewer',
        'qa_approver',
        'prepared_by',
        'supervisor',
        'qa_supervisor',
        'regulatory_manager',
        'effective_date',
    ],

    get_indicator: function (doc) {
        // Approved + active = green Controlled Copy
        if (doc.docstatus === 1 && doc.is_active) {
            return [__('Controlled'), 'green', 'docstatus,=,1|is_active,=,1'];
        }
        // QA-approved but not yet the effective version: scheduled with a
        // future Effective Date (activated by the daily sweep on that date).
        // Not date-gated — a due document stays "pending" until the sweep
        // flips it, and must never read as Obsolete.
        if (
            doc.docstatus === 1 &&
            doc.workflow_status === 'Approved' &&
            !cint(doc.is_active)
        ) {
            return [
                doc.effective_date
                    ? __('Effective {0}', [frappe.datetime.str_to_user(doc.effective_date)])
                    : __('Pending Effective'),
                'orange',
                'docstatus,=,1|is_active,=,0|workflow_status,=,Approved',
            ];
        }
        // Abandoned draft revision, retained for audit. Checked before the
        // generic inactive branch: these records also carry is_active=0 but
        // were never an effective version, so 'Obsolete' would be misleading.
        if (doc.workflow_status === 'Revision Cancelled') {
            return [__('Revision Cancelled'), 'gray', 'workflow_status,=,Revision Cancelled'];
        }
        // Cancelled or inactive
        if (cint(doc.docstatus) === 2 || !cint(doc.is_active)) {
            return [__('Obsolete'), 'red', 'is_active,=,0'];
        }
        // Draft docs use the workflow status for color
        const wf = doc.workflow_status;
        if (wf === 'Under Review') return [__('Under Review'), 'orange', 'workflow_status,=,Under Review'];
        if (wf === 'Revision Requested') return [__('Revision Requested'), 'red', 'workflow_status,=,Revision Requested'];
        return [__('Draft'), 'gray', 'docstatus,=,0|workflow_status,=,Draft'];
    },

    onload: function (listview) {
        listview.page.add_inner_button(__('Tree View'), () => {
            frappe.set_route('gmp-document-tree');
        });

        const me = frappe.session.user;

        // One entry per stage that waits on a named person — the same stages
        // get_my_pending_count() counts. (The delegated QA review queue lives
        // in a child table, so it is reached from the document, not here.)
        const stages = [
            [__('Awaiting My Supervisor Approval'), 'Pending Supervisor Approval', 'supervisor'],
            [__('Awaiting My Review'), 'Under Review', 'reviewer'],
            [__('Awaiting Me as QA Supervisor'), 'Pending QA Supervisor', 'qa_supervisor'],
            [__('Awaiting My Regulatory Validation'), 'Pending Regulatory Validation', 'regulatory_manager'],
            [__('Awaiting My Manager Approval'), 'Pending Manager Approval', 'reviewer'],
            [__('Awaiting My QA Approval'), 'Pending Final QA Approval', 'qa_approver'],
            [__('My Revisions to Address'), 'Revision Requested', 'prepared_by'],
        ];
        for (const [label, state, actor_field] of stages) {
            listview.page.add_inner_button(
                label,
                () => {
                    listview.filter_area.clear();
                    listview.filter_area.add('GMP Document', 'workflow_status', '=', state);
                    listview.filter_area.add('GMP Document', actor_field, '=', me);
                    listview.refresh();
                },
                __('My Pending')
            );
        }
    },
};
