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
    ],

    get_indicator: function (doc) {
        // Approved + active = green Controlled Copy
        if (doc.docstatus === 1 && doc.is_active) {
            return [__('Controlled'), 'green', 'docstatus,=,1|is_active,=,1'];
        }
        // Cancelled or inactive
        if (cint(doc.docstatus) === 2 || !cint(doc.is_active)) {
            return [__('Obsolete'), 'red', 'is_active,=,0'];
        }
        // Draft docs use the workflow status for color
        const wf = doc.workflow_status;
        if (wf === 'Under Review') return [__('Under Review'), 'orange', 'workflow_status,=,Under Review'];
        if (wf === 'Pending QA Approval') return [__('Pending QA'), 'blue', 'workflow_status,=,Pending QA Approval'];
        if (wf === 'Revision Requested') return [__('Revision Requested'), 'red', 'workflow_status,=,Revision Requested'];
        return [__('Draft'), 'gray', 'docstatus,=,0|workflow_status,=,Draft'];
    },

    onload: function (listview) {
        listview.page.add_inner_button(__('Tree View'), () => {
            frappe.set_route('gmp-document-tree');
        });

        const me = frappe.session.user;

        listview.page.add_inner_button(
            __('Awaiting My Review'),
            () => {
                listview.filter_area.clear();
                listview.filter_area.add('GMP Document', 'workflow_status', '=', 'Under Review');
                listview.filter_area.add('GMP Document', 'reviewer', '=', me);
                listview.refresh();
            },
            __('My Pending')
        );

        listview.page.add_inner_button(
            __('Awaiting My QA Approval'),
            () => {
                listview.filter_area.clear();
                listview.filter_area.add('GMP Document', 'workflow_status', '=', 'Pending QA Approval');
                listview.filter_area.add('GMP Document', 'qa_approver', '=', me);
                listview.refresh();
            },
            __('My Pending')
        );

        listview.page.add_inner_button(
            __('My Revisions to Address'),
            () => {
                listview.filter_area.clear();
                listview.filter_area.add('GMP Document', 'workflow_status', '=', 'Revision Requested');
                listview.filter_area.add('GMP Document', 'prepared_by', '=', me);
                listview.refresh();
            },
            __('My Pending')
        );
    },
};
