# Copyright (c) 2026, ErenAydin - GMP DMS Module
# License: MIT
"""Runtime permission / visibility / reference-tree tests for GMP Document.

Validates the v1.2.0–v1.2.2 access-control model end-to-end on a real Frappe
site:

    - department-scoped read for plain members (Employee role)
    - full access for DMS Manager / QA Manager / System Manager
    - get_permission_query_conditions / has_permission / _visibility_scope
    - get_document_reference_tree hardening: existing refs, dangling refs,
      missing root, cross-department filtering, multi-level nesting, circular
      references, large graphs, and depth clamping
    - get_dms_tree_children department scoping
    - the workflow `allow_edit` configuration (DMS Manager editing rights)

Hermetic: no LibreOffice / submit needed. The approved+active state
(docstatus=1, is_active=1) that a member is allowed to see is fabricated via
db_set so the permission paths can be exercised without the soffice render
pipeline.
"""

import unittest

import frappe
from frappe.tests.utils import FrappeTestCase

from dms.dms.doctype.gmp_document.gmp_document import (
    MAX_REFERENCE_TREE_DEPTH,
    _is_unrestricted,
    _user_departments,
    _visibility_scope,
    get_dms_tree_children,
    get_document_reference_tree,
    get_permission_query_conditions,
    has_permission,
)
from dms.install import _sync_gmp_workflow

QA_DEPT = "GMP-Perm-QA Department"
PROD_DEPT = "GMP-Perm-PROD Department"
QA_ABBR = "PQA"
PROD_ABBR = "PPR"

MEMBER_QA = "gmp-perm-member-qa@example.com"
MEMBER_PROD = "gmp-perm-member-prod@example.com"
DMS_MGR = "gmp-perm-dms-manager@example.com"
QA_MGR = "gmp-perm-qa-manager@example.com"
NO_ROLE = "gmp-perm-norole@example.com"

WORD_TEMPLATE = "GMP-Perm-Template"


# ---------------------------------------------------------------------- #
#  Fixtures (idempotent)                                                 #
# ---------------------------------------------------------------------- #


def _ensure_department(name, abbr):
    """Mirror the working pattern in test_gmp_document: a company-less
    Department so its record name equals department_name (required by the
    autoname abbr lookup)."""
    if not frappe.db.exists("Department", name):
        d = frappe.new_doc("Department")
        d.department_name = name
        d.is_group = 0
        d.flags.ignore_mandatory = True
        d.insert(ignore_permissions=True)
    frappe.db.set_value("Department", name, "custom_abbr", abbr)


def _ensure_document_types():
    for type_name, code in (("SOP", "SOP"),):
        if not frappe.db.exists("GMP Document Type", code):
            frappe.get_doc(
                {"doctype": "GMP Document Type", "code": code, "type_name": type_name}
            ).insert(ignore_permissions=True)


def _ensure_word_template():
    if not frappe.db.exists("GMP Word Template", WORD_TEMPLATE):
        frappe.get_doc(
            {
                "doctype": "GMP Word Template",
                "template_title": WORD_TEMPLATE,
                "field_mappings": [
                    {"custom_tag": "my_title", "system_field": "document_name_en"}
                ],
            }
        ).insert(ignore_permissions=True)


def _ensure_user(email, roles):
    if not frappe.db.exists("User", email):
        u = frappe.new_doc("User")
        u.email = email
        u.first_name = email.split("@")[0]
        u.user_type = "System User"
        u.send_welcome_email = 0
        u.insert(ignore_permissions=True)
    # Insert Has Role rows directly. add_roles()/User.save() reconciliation
    # silently strips the hrms-managed "Employee" role (normally assigned by
    # Employee.on_update, which our db_insert fixture bypasses), so we write the
    # child rows at the DB layer and refresh the role cache.
    existing = set(
        frappe.get_all("Has Role", filters={"parent": email, "parenttype": "User"}, pluck="role")
    )
    for role in roles:
        if role not in existing:
            hr = frappe.new_doc("Has Role")
            hr.name = frappe.generate_hash(length=12)
            hr.update({"parent": email, "parenttype": "User", "parentfield": "roles", "role": role})
            hr.db_insert()
    frappe.db.commit()
    frappe.clear_cache(user=email)


def _ensure_employee(email, dept):
    name = frappe.db.get_value("Employee", {"user_id": email}, "name")
    if name:
        frappe.db.set_value("Employee", name, "department", dept)
        return name
    # _user_departments() only reads Employee.user_id -> department, so write the
    # row directly with db_insert(): this bypasses Employee.validate / on_update
    # (the hrms hook chain pulls in erpnext stock code, which is broken under the
    # frappe-17/erpnext-16 base-image mismatch). We just need the column values.
    e = frappe.new_doc("Employee")
    e.name = f"GMP-PERM-EMP-{frappe.generate_hash(length=8)}"
    e.first_name = email.split("@")[0]
    e.employee_name = email.split("@")[0]
    e.user_id = email
    e.department = dept
    e.status = "Active"
    e.flags.ignore_mandatory = True
    e.db_insert()
    frappe.db.commit()
    return e.name


def _hard_delete(name):
    """Delete a test GMP Document regardless of its (fabricated) docstatus.
    force bypasses link checks but not the submitted-record guard, so reset
    docstatus to 0 first."""
    if not frappe.db.exists("GMP Document", name):
        return
    frappe.db.set_value("GMP Document", name, "docstatus", 0, update_modified=False)
    frappe.delete_doc("GMP Document", name, ignore_permissions=True, force=True)


def _purge_docs():
    for name in frappe.get_all(
        "GMP Document",
        filters=[["document_name_en", "like", "GMP-Perm-%"]],
        pluck="name",
    ):
        try:
            _hard_delete(name)
        except Exception:
            pass
    frappe.db.commit()


def _purge_employees():
    for email in (MEMBER_QA, MEMBER_PROD, DMS_MGR, QA_MGR, NO_ROLE):
        emp = frappe.db.get_value("Employee", {"user_id": email}, "name")
        if emp:
            # Raw delete to avoid the hrms Employee on_trash hook chain (same
            # erpnext-stock breakage as on_update under the version mismatch).
            frappe.db.delete("Employee", {"name": emp})
    frappe.db.commit()


class TestGMPPermissions(FrappeTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        _ensure_department(QA_DEPT, QA_ABBR)
        _ensure_department(PROD_DEPT, PROD_ABBR)
        _ensure_document_types()
        _ensure_word_template()
        _ensure_user(MEMBER_QA, ["Employee"])
        _ensure_user(MEMBER_PROD, ["Employee"])
        _ensure_user(DMS_MGR, ["DMS Manager"])
        _ensure_user(QA_MGR, ["QA Manager"])
        _ensure_user(NO_ROLE, [])
        _ensure_employee(MEMBER_QA, QA_DEPT)
        _ensure_employee(MEMBER_PROD, PROD_DEPT)
        _purge_docs()

    @classmethod
    def tearDownClass(cls):
        _purge_docs()
        _purge_employees()
        super().tearDownClass()

    def setUp(self):
        # Department membership is memoised per request via frappe.flags; the
        # test runner is one long-lived process, so clear it between tests.
        frappe.flags.pop("dms_user_departments", None)

    def tearDown(self):
        frappe.set_user("Administrator")
        frappe.flags.pop("dms_user_departments", None)
        _purge_docs()

    # ------------------------------------------------------------------ #
    #  Helpers                                                           #
    # ------------------------------------------------------------------ #

    def _dummy_attachment(self, en):
        fname = f"{en}-{frappe.generate_hash(length=6)}.docx"
        return frappe.get_doc(
            {
                "doctype": "File",
                "file_name": fname,
                "is_private": 1,
                "content": b"PK\x03\x04 dummy",
            }
        ).insert(ignore_permissions=True)

    def _make_doc(self, en, dept=QA_DEPT, active=True):
        doc = frappe.new_doc("GMP Document")
        doc.update(
            {
                "document_name_fa": "تست",
                "document_name_en": en,
                "document_type": "SOP",
                "department": dept,
                "gmp_impact": "Major",
                "validity_period": "3 Years",
                "version_number": 0,
                "reviewer": QA_MGR,
                "qa_approver": QA_MGR,
                "word_template": WORD_TEMPLATE,
            }
        )
        doc.attachment_file = self._dummy_attachment(en).file_url
        doc.insert(ignore_permissions=True)
        if active:
            # Fabricate the approved+active state without the soffice submit path.
            frappe.db.set_value(
                "GMP Document",
                doc.name,
                {"docstatus": 1, "is_active": 1},
                update_modified=False,
            )
            doc.reload()
        return doc

    def _add_reference(self, parent_name, target_name):
        """Insert a reference child row directly so we can build graphs the
        controller's circular-reference validation would otherwise reject."""
        frappe.get_doc(
            {
                "doctype": "GMP Document Reference",
                "parenttype": "GMP Document",
                "parentfield": "references",
                "parent": parent_name,
                "referenced_document": target_name,
                "reference_type": "References",
            }
        ).insert(ignore_permissions=True)

    # ------------------------------------------------------------------ #
    #  _user_departments / _is_unrestricted / _visibility_scope          #
    # ------------------------------------------------------------------ #

    def test_user_departments_resolves_from_employee(self):
        self.assertEqual(_user_departments(MEMBER_QA), [QA_DEPT])
        self.assertEqual(_user_departments(MEMBER_PROD), [PROD_DEPT])
        self.assertEqual(_user_departments(NO_ROLE), [])

    def test_is_unrestricted_by_role(self):
        self.assertTrue(_is_unrestricted("Administrator"))
        self.assertTrue(_is_unrestricted(DMS_MGR))
        self.assertTrue(_is_unrestricted(QA_MGR))
        self.assertFalse(_is_unrestricted(MEMBER_QA))
        self.assertFalse(_is_unrestricted(NO_ROLE))

    def test_visibility_scope(self):
        self.assertEqual(_visibility_scope(DMS_MGR), (None, False))
        self.assertEqual(_visibility_scope(MEMBER_QA), ({QA_DEPT}, True))

    # ------------------------------------------------------------------ #
    #  get_permission_query_conditions                                    #
    # ------------------------------------------------------------------ #

    def test_query_conditions_unrestricted_empty(self):
        self.assertEqual(get_permission_query_conditions("Administrator"), "")
        self.assertEqual(get_permission_query_conditions(DMS_MGR), "")
        self.assertEqual(get_permission_query_conditions(QA_MGR), "")

    def test_query_conditions_member_scoped(self):
        cond = get_permission_query_conditions(MEMBER_QA)
        self.assertIn(QA_DEPT, cond)
        self.assertIn("is_active = 1", cond)
        self.assertIn("docstatus = 1", cond)
        # Named-participant clauses keep workflow assignees visible.
        self.assertIn("prepared_by", cond)
        self.assertIn(MEMBER_QA, cond)
        # Must not leak another department.
        self.assertNotIn(PROD_DEPT, cond)

    def test_query_conditions_no_department_user(self):
        cond = get_permission_query_conditions(NO_ROLE)
        # No department clause, only the participant clauses.
        self.assertNotIn(QA_DEPT, cond)
        self.assertIn(NO_ROLE, cond)

    # ------------------------------------------------------------------ #
    #  has_permission                                                     #
    # ------------------------------------------------------------------ #

    def test_has_permission_member_in_dept_active_read(self):
        doc = self._make_doc("GMP-Perm-InDept", dept=QA_DEPT, active=True)
        self.assertTrue(has_permission(doc, "read", MEMBER_QA))

    def test_has_permission_member_other_dept_denied(self):
        doc = self._make_doc("GMP-Perm-OtherDept", dept=PROD_DEPT, active=True)
        self.assertFalse(has_permission(doc, "read", MEMBER_QA))

    def test_has_permission_member_draft_denied(self):
        doc = self._make_doc("GMP-Perm-Draft", dept=QA_DEPT, active=False)
        # Draft (docstatus 0) is not an approved controlled copy.
        self.assertFalse(has_permission(doc, "read", MEMBER_QA))

    def test_has_permission_member_write_denied(self):
        doc = self._make_doc("GMP-Perm-NoWrite", dept=QA_DEPT, active=True)
        self.assertFalse(has_permission(doc, "write", MEMBER_QA))

    def test_has_permission_named_participant_cross_dept(self):
        # MEMBER_QA is the qa_approver on a PROD doc -> may read it though it is
        # outside their department.
        doc = self._make_doc("GMP-Perm-Named", dept=PROD_DEPT, active=False)
        frappe.db.set_value("GMP Document", doc.name, "qa_approver", MEMBER_QA)
        doc.reload()
        self.assertTrue(has_permission(doc, "read", MEMBER_QA))

    def test_has_permission_dms_manager_all(self):
        doc = self._make_doc("GMP-Perm-DMSAll", dept=PROD_DEPT, active=False)
        self.assertTrue(has_permission(doc, "read", DMS_MGR))
        self.assertTrue(has_permission(doc, "write", DMS_MGR))

    def test_has_permission_unauthorized(self):
        doc = self._make_doc("GMP-Perm-Unauth", dept=QA_DEPT, active=True)
        self.assertFalse(has_permission(doc, "read", NO_ROLE))

    def test_has_permission_doc_none_defers(self):
        # Doctype-level check (no doc) must defer to role perms, not deny.
        self.assertIsNone(has_permission(None, "read", MEMBER_QA))

    # ------------------------------------------------------------------ #
    #  get_document_reference_tree                                        #
    # ------------------------------------------------------------------ #

    def test_ref_tree_existing_reference(self):
        target = self._make_doc("GMP-Perm-RT-Target", active=True)
        root = self._make_doc("GMP-Perm-RT-Root", active=True)
        self._add_reference(root.name, target.name)

        tree = get_document_reference_tree(root.name)
        names = [c["name"] for c in tree["children"]]
        self.assertIn(target.name, names)

    def test_ref_tree_deleted_reference_omitted(self):
        survivor = self._make_doc("GMP-Perm-RT-Survivor", active=True)
        doomed = self._make_doc("GMP-Perm-RT-Doomed", active=True)
        root = self._make_doc("GMP-Perm-RT-Root2", active=True)
        self._add_reference(root.name, survivor.name)
        self._add_reference(root.name, doomed.name)
        _hard_delete(doomed.name)

        tree = get_document_reference_tree(root.name)
        names = [c["name"] for c in tree["children"]]
        self.assertIn(survivor.name, names)
        self.assertNotIn(doomed.name, names)

    def test_ref_tree_missing_root_raises(self):
        with self.assertRaises(frappe.DoesNotExistError):
            get_document_reference_tree("SOP-NOPE-99-v0")

    def test_ref_tree_cross_department_filtered_for_member(self):
        prod_child = self._make_doc("GMP-Perm-RT-ProdChild", dept=PROD_DEPT, active=True)
        qa_root = self._make_doc("GMP-Perm-RT-QARoot", dept=QA_DEPT, active=True)
        self._add_reference(qa_root.name, prod_child.name)

        frappe.set_user(MEMBER_QA)
        try:
            tree = get_document_reference_tree(qa_root.name)
        finally:
            frappe.set_user("Administrator")
        names = [c["name"] for c in tree["children"]]
        # The PROD child is outside the member's department -> omitted.
        self.assertNotIn(prod_child.name, names)

    def test_ref_tree_unauthorized_root_raises_for_member(self):
        prod_root = self._make_doc("GMP-Perm-RT-ProdRoot", dept=PROD_DEPT, active=True)
        frappe.set_user(MEMBER_QA)
        try:
            with self.assertRaises(frappe.PermissionError):
                get_document_reference_tree(prod_root.name)
        finally:
            frappe.set_user("Administrator")

    def test_ref_tree_multi_level_nesting(self):
        c = self._make_doc("GMP-Perm-RT-C", active=True)
        b = self._make_doc("GMP-Perm-RT-B", active=True)
        a = self._make_doc("GMP-Perm-RT-A", active=True)
        self._add_reference(a.name, b.name)
        self._add_reference(b.name, c.name)

        tree = get_document_reference_tree(a.name, depth=4)
        self.assertEqual(tree["name"], a.name)
        self.assertEqual(tree["children"][0]["name"], b.name)
        self.assertEqual(tree["children"][0]["children"][0]["name"], c.name)

    def test_ref_tree_circular_terminates(self):
        a = self._make_doc("GMP-Perm-RT-CycA", active=True)
        b = self._make_doc("GMP-Perm-RT-CycB", active=True)
        self._add_reference(a.name, b.name)
        self._add_reference(b.name, a.name)  # cycle

        # Must terminate (no infinite recursion / RecursionError).
        tree = get_document_reference_tree(a.name, depth=8)
        self.assertEqual(tree["name"], a.name)
        b_node = tree["children"][0]
        self.assertEqual(b_node["name"], b.name)
        # B references A again, but A is already on the path -> rendered as a
        # leaf with no further expansion.
        a_again = b_node["children"][0]
        self.assertEqual(a_again["name"], a.name)
        self.assertEqual(a_again["children"], [])

    def test_ref_tree_depth_clamped(self):
        # A chain longer than the clamp; an absurd depth must not over-expand.
        docs = [self._make_doc(f"GMP-Perm-RT-Chain{i}", active=True) for i in range(MAX_REFERENCE_TREE_DEPTH + 3)]
        for i in range(len(docs) - 1):
            self._add_reference(docs[i].name, docs[i + 1].name)

        tree = get_document_reference_tree(docs[0].name, depth=9999)

        # Walk the single chain and count depth; must not exceed the clamp.
        depth = 0
        node = tree
        while node["children"]:
            node = node["children"][0]
            depth += 1
        self.assertLessEqual(depth, MAX_REFERENCE_TREE_DEPTH)

    def test_ref_tree_depth_bad_value_defaults(self):
        root = self._make_doc("GMP-Perm-RT-BadDepth", active=True)
        # Non-numeric depth must not raise.
        tree = get_document_reference_tree(root.name, depth="not-a-number")
        self.assertEqual(tree["name"], root.name)

    # ------------------------------------------------------------------ #
    #  get_dms_tree_children                                              #
    # ------------------------------------------------------------------ #

    def test_dms_tree_member_sees_only_their_department(self):
        self._make_doc("GMP-Perm-Tree-QA", dept=QA_DEPT, active=True)
        self._make_doc("GMP-Perm-Tree-PROD", dept=PROD_DEPT, active=True)

        frappe.set_user(MEMBER_QA)
        try:
            roots = get_dms_tree_children()
        finally:
            frappe.set_user("Administrator")
        values = {n["value"] for n in roots}
        self.assertIn(f"Dept::{QA_DEPT}", values)
        self.assertNotIn(f"Dept::{PROD_DEPT}", values)

    def test_dms_tree_manager_sees_all_departments(self):
        self._make_doc("GMP-Perm-Tree2-QA", dept=QA_DEPT, active=True)
        self._make_doc("GMP-Perm-Tree2-PROD", dept=PROD_DEPT, active=True)

        frappe.set_user(DMS_MGR)
        try:
            roots = get_dms_tree_children()
        finally:
            frappe.set_user("Administrator")
        values = {n["value"] for n in roots}
        self.assertIn(f"Dept::{QA_DEPT}", values)
        self.assertIn(f"Dept::{PROD_DEPT}", values)

    def test_dms_tree_member_denied_other_department_node(self):
        self._make_doc("GMP-Perm-Tree3-PROD", dept=PROD_DEPT, active=True)
        frappe.set_user(MEMBER_QA)
        try:
            children = get_dms_tree_children(parent=f"Dept::{PROD_DEPT}")
        finally:
            frappe.set_user("Administrator")
        self.assertEqual(children, [])

    # ------------------------------------------------------------------ #
    #  Workflow allow_edit configuration (finding #1 fix)                 #
    # ------------------------------------------------------------------ #

    def test_workflow_allow_edit_grants_dms_manager(self):
        _sync_gmp_workflow()
        wf = frappe.get_doc("Workflow", "GMP Document Workflow")
        allow = {s.state: s.allow_edit for s in wf.states}
        self.assertEqual(allow.get("Draft"), "QA Manager")
        self.assertEqual(allow.get("Revision Requested"), "QA Manager")
        self.assertEqual(allow.get("Under Review"), "DMS Manager")
        self.assertEqual(allow.get("Pending QA Approval"), "DMS Manager")
        self.assertEqual(allow.get("Approved"), "DMS Manager")

    def test_obsolete_state_allows_qa_manager_amend(self):
        """Regression: after a document is cancelled (workflow state Obsolete),
        a plain QA Manager must be able to amend it into a new version. Frappe
        hides the Amend action when the current workflow state makes the form
        read-only (i.e. the user lacks the state's allow_edit role), so the
        Obsolete state must allow QA Manager *and* QA Manager must hold amend
        permission — together that's exactly Frappe's can_amend() condition
        (docstatus == 2 and perm.amend and not workflow-read-only)."""
        _sync_gmp_workflow()
        wf = frappe.get_doc("Workflow", "GMP Document Workflow")
        obsolete_roles = [s.allow_edit for s in wf.states if s.state == "Obsolete"]
        self.assertIn(
            "QA Manager",
            obsolete_roles,
            "Obsolete must be editable by QA Manager or the Amend button is hidden after cancel",
        )
        self.assertTrue(
            frappe.permissions.has_permission("GMP Document", "amend", user=QA_MGR)
        )
