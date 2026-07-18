# Copyright (c) 2026, ErenAydin - GMP DMS Module
# License: MIT
"""v1.3.0 approval-chain tests: dynamic routing (Employee.reports_to +
DMS Settings) and the sequential QA review queue.

Hermetic: no LibreOffice / submit needed. Documents are steered into
mid-chain states via db_set (bypassing the Workflow engine on purpose — the
queue endpoints themselves do server-driven transitions the same way), and
the routing test drives the real Draft → Pending Supervisor Approval save as
Administrator, which every transition condition accepts as escape hatch.
"""

import unittest

import frappe
from frappe.tests.utils import FrappeTestCase

from dms.dms.doctype.gmp_document.gmp_document import (
    QA_APPROVED,
    QA_AWAITING,
    QA_QUEUED,
    QA_RETURNED,
    QA_SKIPPED,
    QA_SUPERSEDED,
    WF_PENDING_MANAGER,
    WF_PENDING_QA_SUPERVISOR,
    WF_PENDING_SUPERVISOR,
    WF_QA_IN_PROGRESS,
    complete_qa_review,
    delegate_qa_review,
    skip_qa_reviewer,
)
from dms.install import _ensure_gmp_workflow, _sync_gmp_workflow

DEPT = "GMP-Chain Department"
DEPT_ABBR = "CHN"

PREPARER = "gmp-chain-preparer@example.com"
SUPERVISOR = "gmp-chain-supervisor@example.com"
MANAGER = "gmp-chain-manager@example.com"
QA_SUP = "gmp-chain-qa-supervisor@example.com"
REG_MGR = "gmp-chain-regulatory@example.com"
QA_APPR = "gmp-chain-qa-approver@example.com"
QA_R1 = "gmp-chain-qa-r1@example.com"
QA_R2 = "gmp-chain-qa-r2@example.com"
QA_R3 = "gmp-chain-qa-r3@example.com"

ALL_USERS = (PREPARER, SUPERVISOR, MANAGER, QA_SUP, REG_MGR, QA_APPR, QA_R1, QA_R2, QA_R3)

WORD_TEMPLATE = "GMP-Chain-Template"

_SIG_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\xf8\x0f\x00"
    b"\x01\x01\x01\x00\x18\xdd\x8d\xb0\x00\x00\x00\x00IEND\xaeB`\x82"
)


# ---------------------------------------------------------------------- #
#  Fixtures (idempotent; db_insert to dodge the hrms hook chain)         #
# ---------------------------------------------------------------------- #


def _ensure_department():
    if not frappe.db.exists("Department", DEPT):
        d = frappe.new_doc("Department")
        d.department_name = DEPT
        d.is_group = 0
        d.flags.ignore_mandatory = True
        d.insert(ignore_permissions=True)
    frappe.db.set_value("Department", DEPT, "custom_abbr", DEPT_ABBR)


def _ensure_document_types():
    if not frappe.db.exists("GMP Document Type", "SOP"):
        frappe.get_doc(
            {"doctype": "GMP Document Type", "code": "SOP", "type_name": "SOP"}
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


def _ensure_user(email):
    if not frappe.db.exists("User", email):
        u = frappe.new_doc("User")
        u.email = email
        u.first_name = email.split("@")[0]
        u.user_type = "System User"
        u.send_welcome_email = 0
        u.insert(ignore_permissions=True)


def _ensure_employee(email, reports_to=None):
    name = frappe.db.get_value("Employee", {"user_id": email}, "name")
    if name:
        frappe.db.set_value(
            "Employee", name, {"department": DEPT, "reports_to": reports_to}
        )
        return name
    e = frappe.new_doc("Employee")
    e.name = f"GMP-CHN-EMP-{frappe.generate_hash(length=8)}"
    e.first_name = email.split("@")[0]
    e.employee_name = email.split("@")[0]
    e.user_id = email
    e.department = DEPT
    e.reports_to = reports_to
    e.status = "Active"
    e.flags.ignore_mandatory = True
    e.db_insert()
    frappe.db.commit()
    return e.name


def _ensure_signature(email):
    emp = frappe.db.get_value("Employee", {"user_id": email}, "name")
    if not frappe.db.get_value("Employee", emp, "custom_signature_image"):
        f = frappe.get_doc(
            {
                "doctype": "File",
                "file_name": f"sig-{frappe.generate_hash(length=6)}.png",
                "is_private": 1,
                "content": _SIG_PNG,
            }
        ).insert(ignore_permissions=True)
        frappe.db.set_value("Employee", emp, "custom_signature_image", f.file_url)


def _configure_settings():
    settings = frappe.get_doc("DMS Settings")
    settings.qa_supervisor = QA_SUP
    settings.regulatory_manager = REG_MGR
    settings.qa_approver = QA_APPR
    settings.department_actors = []
    settings.save(ignore_permissions=True)
    frappe.db.commit()


def _purge_docs():
    for name in frappe.get_all(
        "GMP Document",
        filters=[["document_name_en", "like", "GMP-Chain-%"]],
        pluck="name",
    ):
        try:
            frappe.db.set_value("GMP Document", name, "docstatus", 0, update_modified=False)
            frappe.delete_doc("GMP Document", name, ignore_permissions=True, force=True)
        except Exception:
            pass
    frappe.db.commit()


def _purge_employees():
    for email in ALL_USERS:
        emp = frappe.db.get_value("Employee", {"user_id": email}, "name")
        if emp:
            frappe.db.delete("Employee", {"name": emp})
    frappe.db.commit()


class TestWorkflowChain(FrappeTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        _ensure_department()
        _ensure_document_types()
        _ensure_word_template()
        for email in ALL_USERS:
            _ensure_user(email)
        # Reporting chain: preparer → supervisor → manager (the Reviewer).
        mgr_emp = _ensure_employee(MANAGER)
        sup_emp = _ensure_employee(SUPERVISOR, reports_to=mgr_emp)
        _ensure_employee(PREPARER, reports_to=sup_emp)
        for email in (QA_SUP, REG_MGR, QA_APPR, QA_R1, QA_R2, QA_R3):
            _ensure_employee(email)
        # Signature validation fires once reviewer/qa_approver resolve.
        _ensure_signature(MANAGER)
        _ensure_signature(QA_APPR)
        _configure_settings()
        _ensure_gmp_workflow()
        _sync_gmp_workflow()
        frappe.db.commit()
        _purge_docs()

    @classmethod
    def tearDownClass(cls):
        _purge_docs()
        _purge_employees()
        super().tearDownClass()

    def tearDown(self):
        frappe.set_user("Administrator")
        _purge_docs()

    # ------------------------------------------------------------------ #
    #  Helpers                                                           #
    # ------------------------------------------------------------------ #

    def _dummy_attachment(self, en):
        return frappe.get_doc(
            {
                "doctype": "File",
                "file_name": f"{en}-{frappe.generate_hash(length=6)}.docx",
                "is_private": 1,
                "content": b"PK\x03\x04 dummy",
            }
        ).insert(ignore_permissions=True)

    def _make_draft(self, en):
        doc = frappe.new_doc("GMP Document")
        doc.update(
            {
                "document_name_fa": "تست",
                "document_name_en": en,
                "document_type": "SOP",
                "department": DEPT,
                "gmp_impact": "Major",
                "validity_period": "3 Years",
                "version_number": 0,
                "prepared_by": PREPARER,
                "word_template": WORD_TEMPLATE,
            }
        )
        doc.attachment_file = self._dummy_attachment(en).file_url
        doc.insert(ignore_permissions=True)
        return doc

    def _submit_for_approval(self, doc):
        doc.reload()
        doc.workflow_status = WF_PENDING_SUPERVISOR
        doc.save(ignore_permissions=True)
        doc.reload()
        return doc

    def _make_delegation_ready(self, en):
        """A document parked at Pending QA Supervisor with actors resolved."""
        doc = self._submit_for_approval(self._make_draft(en))
        doc.db_set("workflow_status", WF_PENDING_QA_SUPERVISOR, update_modified=False)
        doc.reload()
        return doc

    def _statuses(self, doc):
        doc.reload()
        return [(r.reviewer, r.status) for r in doc.qa_reviews]

    # ------------------------------------------------------------------ #
    #  Dynamic routing                                                   #
    # ------------------------------------------------------------------ #

    def test_actors_resolved_on_submit_for_approval(self):
        doc = self._submit_for_approval(self._make_draft("GMP-Chain-Routing"))
        self.assertEqual(doc.workflow_status, WF_PENDING_SUPERVISOR)
        self.assertEqual(doc.supervisor, SUPERVISOR)
        self.assertEqual(doc.reviewer, MANAGER)
        self.assertEqual(doc.qa_supervisor, QA_SUP)
        self.assertEqual(doc.regulatory_manager, REG_MGR)
        self.assertEqual(doc.qa_approver, QA_APPR)
        # The submitting transition must leave a ToDo with the supervisor.
        self.assertTrue(
            frappe.db.exists(
                "ToDo",
                {
                    "reference_type": "GMP Document",
                    "reference_name": doc.name,
                    "allocated_to": SUPERVISOR,
                    "status": "Open",
                },
            )
        )

    def test_department_override_beats_global(self):
        settings = frappe.get_doc("DMS Settings")
        settings.append(
            "department_actors", {"department": DEPT, "qa_supervisor": QA_R3}
        )
        settings.save(ignore_permissions=True)
        try:
            doc = self._submit_for_approval(self._make_draft("GMP-Chain-Override"))
            self.assertEqual(doc.qa_supervisor, QA_R3)   # overridden
            self.assertEqual(doc.regulatory_manager, REG_MGR)  # global fallback
        finally:
            _configure_settings()

    def test_missing_reports_to_blocks_submission(self):
        emp = frappe.db.get_value("Employee", {"user_id": PREPARER}, "name")
        frappe.db.set_value("Employee", emp, "reports_to", None)
        try:
            doc = self._make_draft("GMP-Chain-NoSup")
            doc.workflow_status = WF_PENDING_SUPERVISOR
            with self.assertRaises(frappe.ValidationError):
                doc.save(ignore_permissions=True)
        finally:
            sup_emp = frappe.db.get_value("Employee", {"user_id": SUPERVISOR}, "name")
            frappe.db.set_value("Employee", emp, "reports_to", sup_emp)

    def test_missing_settings_actor_blocks_submission(self):
        settings = frappe.get_doc("DMS Settings")
        settings.regulatory_manager = None
        settings.save(ignore_permissions=True)
        try:
            doc = self._make_draft("GMP-Chain-NoReg")
            doc.workflow_status = WF_PENDING_SUPERVISOR
            with self.assertRaises(frappe.ValidationError):
                doc.save(ignore_permissions=True)
        finally:
            _configure_settings()

    # ------------------------------------------------------------------ #
    #  Sequential QA review queue                                        #
    # ------------------------------------------------------------------ #

    def test_delegate_creates_sequential_queue(self):
        doc = self._make_delegation_ready("GMP-Chain-Queue")
        delegate_qa_review(doc.name, [QA_R1, QA_R2, QA_R3])
        doc.reload()
        self.assertEqual(doc.workflow_status, WF_QA_IN_PROGRESS)
        self.assertEqual(
            self._statuses(doc),
            [(QA_R1, QA_AWAITING), (QA_R2, QA_QUEUED), (QA_R3, QA_QUEUED)],
        )

    def test_sequential_completion_advances_to_manager(self):
        doc = self._make_delegation_ready("GMP-Chain-Seq")
        delegate_qa_review(doc.name, [QA_R1, QA_R2])

        complete_qa_review(doc.name, "Approve", "looks good")
        self.assertEqual(
            self._statuses(doc), [(QA_R1, QA_APPROVED), (QA_R2, QA_AWAITING)]
        )

        complete_qa_review(doc.name, "Approve")
        doc.reload()
        self.assertEqual(doc.workflow_status, WF_PENDING_MANAGER)
        self.assertEqual(int(doc.qa_review_complete), 1)

    def test_only_queue_head_may_complete(self):
        doc = self._make_delegation_ready("GMP-Chain-Head")
        delegate_qa_review(doc.name, [QA_R1, QA_R2])
        frappe.set_user(QA_R2)  # not their turn — QA_R1 holds the head
        try:
            with self.assertRaises(frappe.PermissionError):
                complete_qa_review(doc.name, "Approve")
        finally:
            frappe.set_user("Administrator")

    def test_return_halts_queue(self):
        doc = self._make_delegation_ready("GMP-Chain-Return")
        delegate_qa_review(doc.name, [QA_R1, QA_R2])
        with self.assertRaises(frappe.ValidationError):
            complete_qa_review(doc.name, "Return")  # reason mandatory
        complete_qa_review(doc.name, "Return", "needs rework")
        doc.reload()
        self.assertEqual(doc.workflow_status, WF_PENDING_QA_SUPERVISOR)
        self.assertEqual(
            self._statuses(doc), [(QA_R1, QA_RETURNED), (QA_R2, QA_QUEUED)]
        )

    def test_skip_requires_reason_and_advances(self):
        doc = self._make_delegation_ready("GMP-Chain-Skip")
        delegate_qa_review(doc.name, [QA_R1, QA_R2])
        with self.assertRaises(frappe.ValidationError):
            skip_qa_reviewer(doc.name, "")
        skip_qa_reviewer(doc.name, "on leave this week")
        self.assertEqual(
            self._statuses(doc), [(QA_R1, QA_SKIPPED), (QA_R2, QA_AWAITING)]
        )
        # One real approval remains → the queue may still complete forward.
        complete_qa_review(doc.name, "Approve")
        doc.reload()
        self.assertEqual(doc.workflow_status, WF_PENDING_MANAGER)

    def test_all_skipped_round_returns_to_qa_supervisor(self):
        doc = self._make_delegation_ready("GMP-Chain-AllSkip")
        delegate_qa_review(doc.name, [QA_R1, QA_R2])
        skip_qa_reviewer(doc.name, "on leave")
        skip_qa_reviewer(doc.name, "also on leave")
        doc.reload()
        self.assertEqual(doc.workflow_status, WF_PENDING_QA_SUPERVISOR)
        self.assertEqual(int(doc.qa_review_complete), 0)

    def test_redelegation_supersedes_open_rows(self):
        doc = self._make_delegation_ready("GMP-Chain-Redelegate")
        delegate_qa_review(doc.name, [QA_R1, QA_R2])
        complete_qa_review(doc.name, "Return", "wrong scope")
        doc.reload()
        self.assertEqual(doc.workflow_status, WF_PENDING_QA_SUPERVISOR)
        delegate_qa_review(doc.name, [QA_R3])
        doc.reload()
        statuses = self._statuses(doc)
        self.assertEqual(
            statuses,
            [(QA_R1, QA_RETURNED), (QA_R2, QA_SUPERSEDED), (QA_R3, QA_AWAITING)],
        )
        self.assertEqual(max(int(r.round) for r in doc.qa_reviews), 2)

    def test_preparer_cannot_be_delegated(self):
        doc = self._make_delegation_ready("GMP-Chain-SoD")
        with self.assertRaises(frappe.ValidationError):
            delegate_qa_review(doc.name, [PREPARER])

    def test_delegate_requires_qa_supervisor(self):
        doc = self._make_delegation_ready("GMP-Chain-Authz")
        frappe.set_user(QA_R1)
        try:
            with self.assertRaises(frappe.PermissionError):
                delegate_qa_review(doc.name, [QA_R2])
        finally:
            frappe.set_user("Administrator")

    # ------------------------------------------------------------------ #
    #  Workflow definition                                               #
    # ------------------------------------------------------------------ #

    def test_workflow_has_new_chain_and_no_retired_rows(self):
        wf = frappe.get_doc("Workflow", "GMP Document Workflow")
        states = {s.state for s in wf.states}
        for needed in (
            "Pending Supervisor Approval",
            "Pending QA Supervisor",
            "QA Review In Progress",
            "Pending Manager Approval",
            "Pending Regulatory Validation",
            "Pending Final QA Approval",
        ):
            self.assertIn(needed, states)
        transitions = {(t.state, t.action) for t in wf.transitions}
        self.assertIn(("Pending Final QA Approval", "Publish"), transitions)
        self.assertNotIn(("Draft", "Submit for Review"), transitions)
        self.assertNotIn(("Pending QA Approval", "Approve as QA"), transitions)
        # Kept action re-routed to the new state.
        reroute = next(
            t for t in wf.transitions if (t.state, t.action) == ("Under Review", "Approve as Reviewer")
        )
        self.assertEqual(reroute.next_state, "Pending QA Supervisor")


if __name__ == "__main__":
    unittest.main()
