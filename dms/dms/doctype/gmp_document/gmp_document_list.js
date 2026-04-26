// Copyright (c) 2026, ErenAydin - GMP DMS Module
// License: MIT

frappe.listview_settings['GMP Document'] = {
    add_fields: ['is_active', 'docstatus', 'version_number', 'document_type'],

    get_indicator: function (doc) {
        if (doc.docstatus === 1 && doc.is_active) {
            return [__('Controlled'), 'green', 'docstatus,=,1|is_active,=,1'];
        }
        if (cint(doc.docstatus) === 2 || !cint(doc.is_active)) {
            return [__('Obsolete'), 'red', 'is_active,=,0'];
        }
        if (doc.docstatus === 1) {
            return [__('Submitted'), 'blue', 'docstatus,=,1'];
        }
        return [__('Draft'), 'orange', 'docstatus,=,0'];
    },

    onload: function (listview) {
        listview.page.add_inner_button(__('Tree View'), () => {
            frappe.set_route('gmp-document-tree');
        });
    },
};
