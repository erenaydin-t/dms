// Copyright (c) 2026, ErenAydin - GMP DMS Module
// License: MIT
//
// Custom tree view: Department -> Document Type -> Latest submitted version.
// Server data is supplied by
//   dms.dms.doctype.gmp_document.gmp_document.get_dms_tree_children

frappe.pages['gmp-document-tree'].on_page_load = function (wrapper) {
    const page = frappe.ui.make_app_page({
        parent: wrapper,
        title: __('GMP Document Tree'),
        single_column: true,
    });

    page.set_secondary_action(__('Refresh'), () => render_tree(), 'refresh');
    page.add_menu_item(__('GMP Document List'), () => frappe.set_route('List', 'GMP Document'));

    const $container = $('<div class="dms-tree" style="padding: 1rem; font-size: 14px;"></div>').appendTo(page.body);

    function fetch_children(parent) {
        return frappe.call({
            method: 'dms.dms.doctype.gmp_document.gmp_document.get_dms_tree_children',
            args: { parent: parent || '' },
        }).then((r) => r.message || []);
    }

    function indicator_pill(text, color) {
        return `<span class="indicator-pill ${color}" style="margin-left: 8px; font-size: 11px;">${frappe.utils.escape_html(text)}</span>`;
    }

    function render_node(node, $parent, depth) {
        const isLeaf = !node.expandable;
        const $row = $(`
            <div class="tree-node" style="padding: 6px 0; padding-left: ${depth * 24}px; cursor: pointer; user-select: none; border-bottom: 1px solid #f0f0f0;">
                <span class="caret" style="display: inline-block; width: 16px; color: #888;">${node.expandable ? '▸' : ''}</span>
                <span class="title" style="${isLeaf ? 'color: #1f73b7; text-decoration: none;' : 'font-weight: 500;'}">${frappe.utils.escape_html(node.title || node.value)}</span>
                ${node.indicator ? indicator_pill(node.indicator, node.indicator_color || 'gray') : ''}
            </div>
        `);
        const $children = $('<div class="children"></div>').hide();

        $row.on('click', async function (e) {
            e.stopPropagation();
            if (isLeaf) {
                frappe.set_route('Form', 'GMP Document', node.value);
                return;
            }
            if ($children.is(':visible')) {
                $children.hide();
                $row.find('.caret').text('▸');
                return;
            }
            if (!$children.data('loaded')) {
                $children.append('<div class="text-muted" style="padding: 4px 0; padding-left: ' + (depth + 1) * 24 + 'px;">Loading…</div>');
                const kids = await fetch_children(node.value);
                $children.empty();
                if (!kids.length) {
                    $children.append('<div class="text-muted" style="padding: 4px 0; padding-left: ' + (depth + 1) * 24 + 'px;">' + __('No items') + '</div>');
                } else {
                    kids.forEach((k) => render_node(k, $children, depth + 1));
                }
                $children.data('loaded', true);
            }
            $children.show();
            $row.find('.caret').text('▾');
        });

        $parent.append($row).append($children);
    }

    async function render_tree() {
        $container.empty().html('<div class="text-muted">' + __('Loading…') + '</div>');
        const roots = await fetch_children('');
        $container.empty();
        if (!roots.length) {
            $container.html('<div class="text-muted" style="padding: 1rem;">' + __('No submitted GMP Documents yet.') + '</div>');
            return;
        }
        roots.forEach((r) => render_node(r, $container, 0));
    }

    render_tree();
};
