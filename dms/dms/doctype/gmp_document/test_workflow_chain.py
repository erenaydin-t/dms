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
from frappe.model.workflow import apply_workflow
from frappe.tests.utils import FrappeTestCase

from dms.dms import storage
from dms.dms.doctype.gmp_document.gmp_document import (
    PROXY_ROLE,
    QA_APPROVED,
    QA_AWAITING,
    QA_QUEUED,
    QA_RETURNED,
    QA_SKIPPED,
    QA_SUPERSEDED,
    WF_PENDING_FINAL_QA,
    WF_PENDING_MANAGER,
    WF_PENDING_QA_SUPERVISOR,
    WF_PENDING_REGULATORY,
    WF_PENDING_SUPERVISOR,
    WF_QA_IN_PROGRESS,
    WF_REVISION,
    WF_UNDER_REVIEW,
    _user_departments,
    complete_qa_review,
    create_revision,
    delegate_qa_review,
    get_approval_progress,
    get_my_pending_count,
    has_permission,
    set_return_reason,
    skip_qa_reviewer,
)
from dms.install import _ensure_gmp_workflow, _ensure_role, restore_workflow_defaults

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
# Holds DMS Proxy Approver: acts for any role-holder on any document.
PROXY = "gmp-chain-proxy@example.com"

ALL_USERS = (
    PREPARER, SUPERVISOR, MANAGER, QA_SUP, REG_MGR, QA_APPR, QA_R1, QA_R2, QA_R3, PROXY
)

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


def _sig_png_for(email):
    """A valid PNG whose trailing bytes differ per user, so a test can prove
    WHICH person's signature was applied. Bytes after IEND are ignored by
    readers, so this stays a decodable image."""
    return _SIG_PNG + b"#" + email.encode()


def _ensure_signature(email):
    emp = frappe.db.get_value("Employee", {"user_id": email}, "name")
    url = frappe.db.get_value("Employee", emp, "custom_signature_image")
    # Repair a dangling pointer too, not just an empty one: a URL whose File row
    # has gone leaves the employee permanently unusable as an approver, and the
    # fixture is the only thing that can put it back.
    if url and frappe.db.exists("File", {"file_url": url}):
        return
    f = frappe.get_doc(
        {
            "doctype": "File",
            "file_name": f"sig-{frappe.generate_hash(length=6)}.png",
            "is_private": 1,
            "content": _sig_png_for(email),
        }
    ).insert(ignore_permissions=True)
    frappe.db.set_value("Employee", emp, "custom_signature_image", f.file_url)


def _grant_role(email, role):
    _ensure_role(role)
    user = frappe.get_doc("User", email)
    if not any(r.role == role for r in user.roles):
        user.append("roles", {"role": role})
        user.save(ignore_permissions=True)
    # frappe.get_roles caches per user; _acts_on_behalf reads it.
    frappe.clear_cache(user=email)


def _configure_settings():
    settings = frappe.get_doc("DMS Settings")
    settings.qa_supervisor = QA_SUP
    settings.regulatory_manager = REG_MGR
    settings.qa_approver = QA_APPR
    settings.ceo = None  # opt-in per test; the CEO block is optional by design
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
        for email in (QA_SUP, REG_MGR, QA_APPR, QA_R1, QA_R2, QA_R3, PROXY):
            _ensure_employee(email)
        # Signature validation fires once reviewer/qa_approver resolve.
        _ensure_signature(MANAGER)
        _ensure_signature(QA_APPR)
        # Supervisor, proxy and QA_R1 (stand-in CEO) carry their own distinct
        # signatures, so the delegation tests can prove WHICH one was applied.
        _ensure_signature(SUPERVISOR)
        _ensure_signature(PROXY)
        _ensure_signature(QA_R1)
        _grant_role(PROXY, PROXY_ROLE)
        # The roles the shipped workflow gates on (install.py). Without them a
        # user cannot even read the document — get_transitions checks read
        # permission — let alone take a transition.
        _grant_role(PREPARER, "DMS Initiator")
        for email in (SUPERVISOR, MANAGER, QA_SUP, REG_MGR, QA_R1, QA_R2, QA_R3):
            _grant_role(email, "DMS Approver")
        _grant_role(QA_APPR, "QA Manager")
        _configure_settings()
        _ensure_gmp_workflow()
        restore_workflow_defaults()
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

    def _advance(self, doc, state, as_user):
        """Move the document one stage forward AS a given user.

        Saving (rather than db_set) is what fires on_update ->
        _apply_workflow_side_effects, which is the code under test; the session
        user is what the stamps are derived from."""
        frappe.set_user(as_user)
        try:
            doc.reload()
            doc.workflow_status = state
            doc.save(ignore_permissions=True)
        finally:
            frappe.set_user("Administrator")
        doc.reload()
        return doc

    def _signature_of(self, email):
        return frappe.db.get_value(
            "Employee", {"user_id": email}, "custom_signature_image"
        )

    def _file_bytes(self, file_url):
        stored = storage.parse_file_url(file_url)
        if stored:
            return storage.read(*stored)
        path = frappe.get_doc("File", {"file_url": file_url}).get_full_path()
        with open(path, "rb") as fh:
            return fh.read()

    def _assert_snapshot_of(self, snapshot_url, email):
        """The snapshot is a COPY owned by the document, so identity is proved
        by content, not by URL."""
        self.assertTrue(snapshot_url, "no signature was snapshotted")
        self.assertNotEqual(snapshot_url, self._signature_of(email))
        self.assertEqual(
            self._file_bytes(snapshot_url), self._file_bytes(self._signature_of(email))
        )

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

    def test_sequential_completion_advances_to_regulatory(self):
        doc = self._make_delegation_ready("GMP-Chain-Seq")
        delegate_qa_review(doc.name, [QA_R1, QA_R2])

        complete_qa_review(doc.name, "Approve", "looks good")
        self.assertEqual(
            self._statuses(doc), [(QA_R1, QA_APPROVED), (QA_R2, QA_AWAITING)]
        )

        complete_qa_review(doc.name, "Approve")
        doc.reload()
        self.assertEqual(doc.workflow_status, WF_PENDING_REGULATORY)
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
        self.assertEqual(doc.workflow_status, WF_PENDING_REGULATORY)

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
    #  Approval timeline                                                 #
    # ------------------------------------------------------------------ #

    def test_every_stage_stamps_its_actor_and_date(self):
        today = frappe.utils.today()
        doc = self._make_draft("GMP-Chain-Timeline")

        doc = self._advance(doc, WF_PENDING_SUPERVISOR, PREPARER)
        self.assertIsNotNone(doc.submitted_on)
        self.assertEqual(str(doc.preparer_date), today)

        doc = self._advance(doc, WF_UNDER_REVIEW, SUPERVISOR)
        self.assertEqual(doc.supervisor_approved_by, SUPERVISOR)
        self.assertEqual(str(doc.supervisor_date), today)
        self.assertFalse(doc.supervisor_on_behalf_of)  # acted for themselves
        self._assert_snapshot_of(doc.supervisor_signature, SUPERVISOR)

        doc = self._advance(doc, WF_PENDING_QA_SUPERVISOR, MANAGER)
        self.assertEqual(doc.reviewed_by, MANAGER)
        self.assertEqual(str(doc.reviewer_date), today)

        doc = self._advance(doc, WF_PENDING_REGULATORY, QA_SUP)
        self.assertEqual(doc.qa_supervisor_approved_by, QA_SUP)
        self.assertEqual(str(doc.qa_supervisor_date), today)

        doc = self._advance(doc, WF_PENDING_MANAGER, REG_MGR)
        self.assertEqual(doc.regulatory_validated_by, REG_MGR)
        self.assertEqual(str(doc.regulatory_date), today)

        doc = self._advance(doc, WF_PENDING_FINAL_QA, MANAGER)
        self.assertEqual(doc.manager_approved_by, MANAGER)
        self.assertEqual(str(doc.manager_date), today)

    def test_queue_completion_stamps_the_qa_supervisor_stage(self):
        doc = self._make_delegation_ready("GMP-Chain-QueueStamp")
        delegate_qa_review(doc.name, [QA_R1])
        complete_qa_review(doc.name, "Approve")
        doc.reload()
        self.assertEqual(doc.workflow_status, WF_PENDING_REGULATORY)
        # QA cleared via the delegated queue: the stage is credited to the
        # supervisor who owns the delegation, not to whichever reviewer
        # happened to close the last row.
        self.assertEqual(doc.qa_supervisor_approved_by, QA_SUP)
        self.assertEqual(str(doc.qa_supervisor_date), frappe.utils.today())

    def test_revision_starts_with_an_empty_timeline(self):
        doc = self._advance(
            self._make_draft("GMP-Chain-TimelineReset"), WF_PENDING_SUPERVISOR, PREPARER
        )
        doc = self._advance(doc, WF_UNDER_REVIEW, SUPERVISOR)
        self.assertTrue(doc.supervisor_date)

        # Stand the predecessor up as the effective version without a real
        # submit (which needs LibreOffice); the revise guard reads these three
        # fields straight from the database.
        doc.db_set("workflow_status", "Approved", update_modified=False)
        doc.db_set("docstatus", 1, update_modified=False)
        doc.db_set("is_active", 1, update_modified=False)
        doc.reload()

        successor = frappe.copy_doc(doc)
        successor.docstatus = 0  # copy_doc carries the source's submitted docstatus
        successor.revision_of = doc.name
        successor.reason_for_change = "timeline reset check"
        successor.document_name_en = "GMP-Chain-TimelineReset-r2"
        successor.attachment_file = self._dummy_attachment("GMP-Chain-TimelineReset-r2").file_url
        # Hand the successor stale stamps on purpose: before_insert must clear
        # them even when a caller carries them over, not merely rely on no_copy.
        successor.preparer_date = frappe.utils.today()
        successor.supervisor_date = frappe.utils.today()
        successor.supervisor_approved_by = SUPERVISOR
        successor.supervisor_signature = self._signature_of(SUPERVISOR)
        successor.qa_approver_on_behalf_of = QA_APPR
        successor.publish_date = frappe.utils.today()
        successor.ceo = QA_R1
        successor.ceo_date = frappe.utils.today()
        successor.insert(ignore_permissions=True)

        for field in (
            "preparer_date",
            "supervisor_date",
            "supervisor_approved_by",
            "supervisor_signature",
            "qa_approver_on_behalf_of",
            "publish_date",
            "ceo",
            "ceo_date",
        ):
            self.assertFalse(successor.get(field), f"{field} should start blank on a revision")

    # ------------------------------------------------------------------ #
    #  Delegated approval (DMS Proxy Approver)                           #
    # ------------------------------------------------------------------ #

    def test_proxy_approval_records_who_acted_for_whom(self):
        doc = self._advance(
            self._make_draft("GMP-Chain-Proxy"), WF_PENDING_SUPERVISOR, PREPARER
        )
        doc = self._advance(doc, WF_UNDER_REVIEW, PROXY)

        # The signer of record is the account that actually acted...
        self.assertEqual(doc.supervisor_approved_by, PROXY)
        # ...and the document states who they stood in for, with the date.
        self.assertEqual(doc.supervisor_on_behalf_of, SUPERVISOR)
        self.assertEqual(str(doc.supervisor_date), frappe.utils.today())

    def test_proxy_approval_applies_the_represented_signature(self):
        doc = self._advance(
            self._make_draft("GMP-Chain-ProxySig"), WF_PENDING_SUPERVISOR, PREPARER
        )
        doc = self._advance(doc, WF_UNDER_REVIEW, PROXY)

        # The signature block must read as the assigned supervisor's — that is
        # the whole point of approving on their behalf — never the proxy's own.
        self._assert_snapshot_of(doc.supervisor_signature, SUPERVISOR)
        self.assertNotEqual(
            self._file_bytes(doc.supervisor_signature),
            self._file_bytes(self._signature_of(PROXY)),
        )
        # ...and that is the image the renderer picks up.
        with open(doc._signature_paths()["supervisor_signature"], "rb") as fh:
            self.assertEqual(fh.read(), self._file_bytes(self._signature_of(SUPERVISOR)))

    def test_snapshot_never_claims_the_employees_signature_file(self):
        """Regression (2.6.1): the snapshot must COPY the image into a file this
        document owns.

        Frappe's global `attach_files_to_document` hook claims any *unattached*
        File named by an Attach Image field, re-parenting it to the document
        being saved. Storing the employee's own signature URL therefore stole
        their File on first approval — and deleting that document destroyed the
        signature for every future document, leaving the employee unusable as an
        approver with a dangling `custom_signature_image`."""
        sig_url = self._signature_of(SUPERVISOR)
        before = frappe.db.get_value(
            "File",
            {"file_url": sig_url},
            ["name", "attached_to_doctype", "attached_to_name"],
            as_dict=True,
        )

        doc = self._advance(
            self._make_draft("GMP-Chain-SigOwnership"), WF_PENDING_SUPERVISOR, PREPARER
        )
        doc = self._advance(doc, WF_UNDER_REVIEW, SUPERVISOR)

        # The snapshot is a distinct file stored for this document...
        self.assertNotEqual(doc.supervisor_signature, sig_url)
        snapshot = storage.parse_file_url(doc.supervisor_signature)
        self.assertEqual(snapshot[0], doc.name)
        self.assertTrue(storage.exists(*snapshot))
        self.assertFalse(frappe.db.exists("File", {"file_url": doc.supervisor_signature}))

        # ...and the employee's own File was not re-parented.
        after = frappe.db.get_value(
            "File", before.name, ["attached_to_doctype", "attached_to_name"], as_dict=True
        )
        self.assertEqual(after.attached_to_doctype, before.attached_to_doctype)
        self.assertEqual(after.attached_to_name, before.attached_to_name)

        # Deleting the document must not take the employee's signature with it
        # — only its own copy.
        frappe.delete_doc("GMP Document", doc.name, force=True, ignore_permissions=True)
        self.assertTrue(
            frappe.db.exists("File", {"file_url": sig_url}),
            "deleting the document destroyed the employee's signature file",
        )
        self.assertFalse(storage.exists(*snapshot))

    def test_proxy_reviewer_signature_beats_the_actual_actor(self):
        doc = self._advance(
            self._make_draft("GMP-Chain-ProxyReviewer"), WF_PENDING_SUPERVISOR, PREPARER
        )
        doc = self._advance(doc, WF_UNDER_REVIEW, SUPERVISOR)
        doc = self._advance(doc, WF_PENDING_QA_SUPERVISOR, PROXY)

        self.assertEqual(doc.reviewed_by, PROXY)
        self.assertEqual(doc.reviewer_on_behalf_of, MANAGER)
        # The reviewer signature resolves through on_behalf_of first, so the
        # proxy's own (perfectly valid) signature must NOT be the one applied.
        self.assertEqual(
            doc._signature_paths().get("reviewer_signature"),
            frappe.get_doc("File", {"file_url": self._signature_of(MANAGER)}).get_full_path(),
        )

    # ------------------------------------------------------------------ #
    #  CEO authorization                                                 #
    # ------------------------------------------------------------------ #

    def test_ceo_block_stamped_from_settings(self):
        settings = frappe.get_doc("DMS Settings")
        settings.ceo = QA_R1
        settings.save(ignore_permissions=True)
        try:
            doc = self._make_draft("GMP-Chain-CEO")
            doc._stamp_ceo_authorization()
            doc.reload()
            self.assertEqual(doc.ceo, QA_R1)
            self.assertEqual(doc.ceo_name, frappe.db.get_value("User", QA_R1, "full_name"))
            self.assertEqual(str(doc.ceo_date), frappe.utils.today())
            self._assert_snapshot_of(doc.ceo_signature, QA_R1)
        finally:
            _configure_settings()

    def test_ceo_department_override_beats_global(self):
        from dms.dms.doctype.dms_settings.dms_settings import resolve_department_actors

        settings = frappe.get_doc("DMS Settings")
        settings.ceo = QA_R1
        settings.append("department_actors", {"department": DEPT, "ceo": QA_R2})
        settings.save(ignore_permissions=True)
        try:
            self.assertEqual(resolve_department_actors(DEPT)["ceo"], QA_R2)
        finally:
            _configure_settings()

    def test_ceo_block_stays_empty_when_unconfigured(self):
        doc = self._make_draft("GMP-Chain-NoCEO")
        doc._stamp_ceo_authorization()
        doc.reload()
        self.assertFalse(doc.ceo)
        self.assertFalse(doc.ceo_date)
        self.assertFalse(doc.ceo_signature)

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

    def test_workflow_ships_a_proxy_twin_of_every_actor_gated_row(self):
        wf = frappe.get_doc("Workflow", "GMP Document Workflow")
        proxy_rows = {(t.state, t.action) for t in wf.transitions if t.allowed == PROXY_ROLE}
        for expected in (
            ("Draft", "Submit for Approval"),
            ("Pending Supervisor Approval", "Approve (Supervisor)"),
            ("Under Review", "Approve as Reviewer"),
            ("Pending QA Supervisor", "Approve (QA Supervisor)"),
            ("Pending Regulatory Validation", "Validate (Regulatory)"),
            ("Pending Manager Approval", "Approve (Manager)"),
            ("Pending Final QA Approval", "Publish"),
        ):
            self.assertIn(expected, proxy_rows)
        # Abandoning a draft revision is the author's and QA's call, never a
        # stand-in's — so it ships no twin.
        self.assertNotIn(("Draft", "Cancel Revision"), proxy_rows)
        # A twin only ever offers itself to someone who is NOT the assigned
        # actor, so a user holding both roles sees each action exactly once.
        twin = next(
            t
            for t in wf.transitions
            if (t.state, t.action) == ("Pending Supervisor Approval", "Approve (Supervisor)")
            and t.allowed == PROXY_ROLE
        )
        self.assertIn("doc.supervisor != frappe.session.user", twin.condition)

    def test_migrate_never_overwrites_an_edited_workflow(self):
        """The seed contract: once the workflow exists, the site owns it. A
        module upgrade must leave edited transitions exactly as the site left
        them — this is what makes the Workflow page safe to use."""
        from dms.install import after_migrate

        wf = frappe.get_doc("Workflow", "GMP Document Workflow")
        row = next(
            t
            for t in wf.transitions
            if (t.state, t.action) == ("Pending Supervisor Approval", "Approve (Supervisor)")
            and t.allowed == "DMS Approver"
        )
        before_rows = len(wf.transitions)
        original = {"allowed": row.allowed, "condition": row.condition}
        edited_condition = "doc.supervisor == frappe.session.user"

        try:
            # The kinds of edit a site makes from the Workflow page: a
            # different role, and a condition without our admin escape hatch.
            frappe.db.set_value(
                "Workflow Transition",
                row.name,
                {"allowed": "QA Manager", "condition": edited_condition},
            )
            frappe.db.commit()

            after_migrate()

            kept = frappe.get_doc("Workflow Transition", row.name)
            self.assertEqual(kept.allowed, "QA Manager")
            self.assertEqual(kept.condition, edited_condition)
            # ...and nothing was appended back either.
            self.assertEqual(
                len(frappe.get_doc("Workflow", "GMP Document Workflow").transitions),
                before_rows,
            )
        finally:
            # Put the row back by hand: restore_workflow_defaults() treats a row
            # whose role AND condition both differ as one the site added, so it
            # would leave this one behind and append a fresh copy beside it.
            frappe.db.set_value("Workflow Transition", row.name, original)
            frappe.db.commit()

    def test_restore_defaults_is_idempotent_with_twin_rows(self):
        """The twins make (state, action) non-unique; a restore that keyed on
        that pair alone would re-append them on every run."""
        before = len(frappe.get_doc("Workflow", "GMP Document Workflow").transitions)
        restore_workflow_defaults()
        restore_workflow_defaults()
        self.assertEqual(
            len(frappe.get_doc("Workflow", "GMP Document Workflow").transitions), before
        )

    def test_restore_defaults_repairs_a_renamed_role_on_a_twinned_row(self):
        """Role renames are the documented failure mode this repair exists for;
        the twin lookup must still fall back to `condition` so a row whose
        `allowed` no longer matches any shipped role is restored."""
        wf = frappe.get_doc("Workflow", "GMP Document Workflow")
        row = next(
            t
            for t in wf.transitions
            if (t.state, t.action) == ("Pending Supervisor Approval", "Approve (Supervisor)")
            and t.allowed == "DMS Approver"
        )
        frappe.db.set_value(
            "Workflow Transition", row.name, "allowed", "Some Renamed Role"
        )
        frappe.db.commit()

        restore_workflow_defaults()

        wf = frappe.get_doc("Workflow", "GMP Document Workflow")
        repaired = [
            t
            for t in wf.transitions
            if (t.state, t.action) == ("Pending Supervisor Approval", "Approve (Supervisor)")
        ]
        self.assertEqual(
            sorted(t.allowed for t in repaired), ["DMS Approver", PROXY_ROLE]
        )

    # ------------------------------------------------------------------ #
    #  Real permissions: actors holding only their workflow role         #
    # ------------------------------------------------------------------ #
    # Everything above saves with ignore_permissions. These go through the
    # same checks a browser session does: insert() and apply_workflow() as a
    # user who holds DMS Initiator or DMS Approver and nothing broader.

    def _act(self, doc, action, as_user):
        frappe.set_user(as_user)
        try:
            apply_workflow(frappe.get_doc("GMP Document", doc.name), action)
        finally:
            frappe.set_user("Administrator")
        doc.reload()
        return doc

    def _as(self, user, fn, *args, **kwargs):
        frappe.set_user(user)
        try:
            return fn(*args, **kwargs)
        finally:
            frappe.set_user("Administrator")

    def _force_effective(self, doc):
        doc.db_set(
            {"docstatus": 1, "workflow_status": "Approved", "is_active": 1}, update_modified=False
        )
        doc.reload()
        return doc

    def test_initiator_creates_a_draft_with_their_own_permissions(self):
        attachment = self._dummy_attachment("GMP-Chain-Perm-Create").file_url

        def create():
            doc = frappe.new_doc("GMP Document")
            doc.update(
                {
                    "document_name_fa": "تست",
                    "document_name_en": "GMP-Chain-Perm-Create",
                    "document_type": "SOP",
                    "department": DEPT,
                    "gmp_impact": "Major",
                    "validity_period": "3 Years",
                    "word_template": WORD_TEMPLATE,
                    "attachment_file": attachment,
                }
            )
            return doc.insert()

        doc = self._as(PREPARER, create)
        self.assertEqual(doc.prepared_by, PREPARER)

    def test_restricted_actors_walk_the_chain_with_apply_workflow(self):
        """Frappe sets the next state BEFORE it checks write permission, so a
        write gate keyed on the in-memory state asked about the next stage's
        actor and refused every one of these clicks."""
        doc = self._make_draft("GMP-Chain-Perm-Walk")
        for action, user, state in (
            ("Submit for Approval", PREPARER, WF_PENDING_SUPERVISOR),
            ("Approve (Supervisor)", SUPERVISOR, WF_UNDER_REVIEW),
            ("Approve as Reviewer", MANAGER, WF_PENDING_QA_SUPERVISOR),
            ("Approve (QA Supervisor)", QA_SUP, WF_PENDING_REGULATORY),
            ("Validate (Regulatory)", REG_MGR, WF_PENDING_MANAGER),
            ("Approve (Manager)", MANAGER, WF_PENDING_FINAL_QA),
        ):
            doc = self._act(doc, action, user)
            self.assertEqual(doc.workflow_status, state, action)

    def test_only_the_waiting_actor_may_edit(self):
        doc = self._act(self._make_draft("GMP-Chain-Perm-Edit"), "Submit for Approval", PREPARER)
        self.assertTrue(has_permission(doc, "write", SUPERVISOR))
        self.assertFalse(has_permission(doc, "write", PREPARER))
        self.assertFalse(has_permission(doc, "write", MANAGER))

        def edit():
            d = frappe.get_doc("GMP Document", doc.name)
            d.document_name_fa = "ویرایش"
            d.save()

        with self.assertRaises(frappe.PermissionError):
            self._as(PREPARER, edit)

    def test_actor_fields_sent_by_the_client_do_not_grant_write(self):
        doc = self._act(self._make_draft("GMP-Chain-Perm-Forge"), "Submit for Approval", PREPARER)
        doc.supervisor = MANAGER  # what a forged REST payload would carry
        self.assertFalse(has_permission(doc, "write", MANAGER))

    def test_preparer_cancels_a_revision_waiting_on_the_supervisor(self):
        source = self._force_effective(self._make_draft("GMP-Chain-Perm-Cancel"))
        rev = frappe.get_doc(
            "GMP Document", self._as(PREPARER, create_revision, source.name, "Update")
        )
        self.assertEqual(rev.prepared_by, PREPARER)

        def attach():
            d = frappe.get_doc("GMP Document", rev.name)
            d.attachment_file = self._dummy_attachment("GMP-Chain-Perm-Cancel-2").file_url
            d.save()

        self._as(PREPARER, attach)
        rev = self._act(rev, "Submit for Approval", PREPARER)
        rev = self._act(rev, "Cancel Revision", PREPARER)
        self.assertEqual(rev.workflow_status, "Revision Cancelled")

    def test_revision_needs_someone_who_can_submit_the_draft(self):
        source = self._force_effective(self._make_draft("GMP-Chain-Perm-Author"))
        with self.assertRaises(frappe.PermissionError):
            self._as(SUPERVISOR, create_revision, source.name, "Update")

    # ------------------------------------------------------------------ #
    #  Effective date                                                    #
    # ------------------------------------------------------------------ #

    def test_a_scheduled_date_does_not_block_later_approvers(self):
        """The privilege check used to fire on every save while the box was
        ticked, so the first approver after the manager who set the date was
        refused — and every one after them."""
        doc = self._make_draft("GMP-Chain-Eff-Scheduled")
        future = frappe.utils.add_days(frappe.utils.today(), 30)
        doc.set_effective_date = 1
        doc.effective_date = future
        doc.save(ignore_permissions=True)  # as Administrator, a privileged user

        doc = self._act(doc, "Submit for Approval", PREPARER)
        doc = self._act(doc, "Approve (Supervisor)", SUPERVISOR)
        self.assertEqual(doc.workflow_status, WF_UNDER_REVIEW)
        self.assertEqual(str(doc.effective_date), future)

    def test_setting_the_date_still_needs_the_privilege(self):
        doc = self._make_draft("GMP-Chain-Eff-Denied")

        def schedule():
            d = frappe.get_doc("GMP Document", doc.name)
            d.set_effective_date = 1
            d.effective_date = frappe.utils.add_days(frappe.utils.today(), 30)
            d.save()

        with self.assertRaises(frappe.PermissionError):
            self._as(PREPARER, schedule)

    def test_unticking_clears_a_manual_date(self):
        doc = self._make_draft("GMP-Chain-Eff-Untick")
        doc.set_effective_date = 1
        doc.effective_date = frappe.utils.add_days(frappe.utils.today(), 30)
        doc.save(ignore_permissions=True)
        self.assertTrue(doc.expiry_date)

        doc.set_effective_date = 0
        doc.save(ignore_permissions=True)
        doc.reload()
        self.assertFalse(doc.effective_date)
        self.assertFalse(doc.expiry_date)
        self.assertFalse(doc.next_revision_date)

    # ------------------------------------------------------------------ #
    #  Return reasons                                                    #
    # ------------------------------------------------------------------ #

    def _return_comments(self, doc):
        return frappe.get_all(
            "Comment",
            filters={
                "reference_doctype": "GMP Document",
                "reference_name": doc.name,
                "content": ["like", "Revision requested by%"],
            },
            pluck="content",
            order_by="creation asc",
        )

    def test_return_reason_is_recorded_then_cleared(self):
        doc = self._act(self._make_draft("GMP-Chain-Return"), "Submit for Approval", PREPARER)
        self._as(SUPERVISOR, set_return_reason, doc.name, "Fix section 3")
        doc = self._act(doc, "Return to Preparer", SUPERVISOR)

        self.assertEqual(doc.workflow_status, WF_REVISION)
        self.assertEqual(doc.last_revision_request, "Fix section 3")
        self.assertEqual(doc.last_revision_by, SUPERVISOR)
        self.assertIn("Fix section 3", self._return_comments(doc)[-1])

        # Moving forward again clears it, so a later return recorded without
        # a reason is never logged under the supervisor's words.
        doc = self._act(doc, "Submit for Approval", PREPARER)
        self.assertFalse(doc.last_revision_request)
        doc = self._act(doc, "Return to Preparer", SUPERVISOR)
        self.assertNotIn("Fix section 3", self._return_comments(doc)[-1])

    def test_only_the_waiting_actor_may_give_a_return_reason(self):
        doc = self._act(self._make_draft("GMP-Chain-Return-Denied"), "Submit for Approval", PREPARER)
        with self.assertRaises(frappe.PermissionError):
            self._as(MANAGER, set_return_reason, doc.name, "Not mine to return")

    # ------------------------------------------------------------------ #
    #  Fields only the controller writes                                 #
    # ------------------------------------------------------------------ #

    def test_a_draft_save_cannot_forge_the_approval_record(self):
        other = self._force_effective(self._make_draft("GMP-Chain-Forge-Target"))
        doc = self._make_draft("GMP-Chain-Forge")

        def forge():
            d = frappe.get_doc("GMP Document", doc.name)
            d.revision_of = other.name  # approval would retire `other`
            d.ceo = QA_R1
            d.ceo_name = "Forged"
            d.qa_approver_on_behalf_of = QA_R2
            d.save()

        self._as(PREPARER, forge)
        doc.reload()
        self.assertFalse(doc.revision_of)
        self.assertFalse(doc.ceo)
        self.assertFalse(doc.ceo_name)
        self.assertFalse(doc.qa_approver_on_behalf_of)

    def test_a_new_document_starts_without_an_approval_record(self):
        doc = frappe.new_doc("GMP Document")
        doc.update(
            {
                "document_name_fa": "تست",
                "document_name_en": "GMP-Chain-Forge-New",
                "document_type": "SOP",
                "department": DEPT,
                "gmp_impact": "Major",
                "validity_period": "3 Years",
                "prepared_by": PREPARER,
                "word_template": WORD_TEMPLATE,
                "attachment_file": self._dummy_attachment("GMP-Chain-Forge-New").file_url,
                "ceo": QA_R1,
                "approved_by": QA_R2,
                "supervisor_on_behalf_of": QA_R3,
            }
        )
        doc.insert(ignore_permissions=True)
        doc.reload()
        self.assertFalse(doc.ceo)
        self.assertFalse(doc.approved_by)
        self.assertFalse(doc.supervisor_on_behalf_of)

    # ------------------------------------------------------------------ #
    #  Visibility                                                        #
    # ------------------------------------------------------------------ #

    def test_a_left_employee_record_grants_no_department(self):
        old = frappe.new_doc("Employee")
        old.name = f"GMP-CHN-EMP-{frappe.generate_hash(length=8)}"
        old.first_name = "left"
        old.employee_name = "left"
        old.user_id = QA_R3
        old.department = "GMP-Chain Former Department"
        old.status = "Left"
        old.flags.ignore_mandatory = True
        old.db_insert()
        try:
            frappe.flags.pop("dms_user_departments", None)
            self.assertNotIn("GMP-Chain Former Department", _user_departments(QA_R3))
            self.assertIn(DEPT, _user_departments(QA_R3))
        finally:
            frappe.db.delete("Employee", {"name": old.name})
            frappe.flags.pop("dms_user_departments", None)

    def test_pending_counts_of_another_user_are_private(self):
        with self.assertRaises(frappe.PermissionError):
            self._as(PREPARER, get_my_pending_count, SUPERVISOR)
        self.assertIn("total", self._as(PREPARER, get_my_pending_count))

    # ------------------------------------------------------------------ #
    #  Who it waits on, and the progress bar                              #
    # ------------------------------------------------------------------ #

    def test_waiting_on_follows_the_chain(self):
        doc = self._make_draft("GMP-Chain-Waiting")
        self.assertEqual(doc.pending_with, PREPARER)
        self.assertEqual(doc.pending_with_name, frappe.db.get_value("User", PREPARER, "full_name"))
        self.assertTrue(doc.pending_since)

        since = {}
        for action, user, waiting_on in (
            ("Submit for Approval", PREPARER, SUPERVISOR),
            ("Approve (Supervisor)", SUPERVISOR, MANAGER),
            ("Approve as Reviewer", MANAGER, QA_SUP),
            ("Approve (QA Supervisor)", QA_SUP, REG_MGR),
            ("Validate (Regulatory)", REG_MGR, MANAGER),
            ("Approve (Manager)", MANAGER, QA_APPR),
        ):
            doc = self._act(doc, action, user)
            self.assertEqual(doc.pending_with, waiting_on, action)
            since[action] = doc.pending_since
        # The reviewer holds the Manager stage too, and its clock starts anew.
        self.assertGreater(since["Validate (Regulatory)"], since["Approve (Supervisor)"])

    def test_waiting_on_follows_the_qa_queue(self):
        doc = self._make_delegation_ready("GMP-Chain-Waiting-Queue")
        delegate_qa_review(doc.name, [QA_R1, QA_R2])
        doc.reload()
        self.assertEqual(doc.pending_with, QA_R1)

        complete_qa_review(doc.name, "Approve")
        doc.reload()
        self.assertEqual(doc.pending_with, QA_R2)

        complete_qa_review(doc.name, "Approve")
        doc.reload()
        self.assertEqual(doc.workflow_status, WF_PENDING_REGULATORY)
        self.assertEqual(doc.pending_with, REG_MGR)

    def test_a_return_hands_the_document_back(self):
        doc = self._act(self._make_draft("GMP-Chain-Waiting-Return"), "Submit for Approval", PREPARER)
        self._as(SUPERVISOR, set_return_reason, doc.name, "Fix section 3")
        doc = self._act(doc, "Return to Preparer", SUPERVISOR)
        self.assertEqual(doc.pending_with, PREPARER)

        progress = self._as(PREPARER, get_approval_progress, doc.name)
        self.assertEqual(progress["returned"]["user"], SUPERVISOR)
        self.assertEqual(progress["returned"]["reason"], "Fix section 3")
        self.assertEqual([s["status"] for s in progress["steps"][:2]], ["current", "upcoming"])

    def test_progress_marks_done_current_and_upcoming(self):
        doc = self._make_draft("GMP-Chain-Progress")
        doc = self._act(doc, "Submit for Approval", PREPARER)
        doc = self._act(doc, "Approve (Supervisor)", SUPERVISOR)

        progress = get_approval_progress(doc.name)
        self.assertEqual(
            [s["status"] for s in progress["steps"]],
            ["done", "done", "current", "upcoming", "upcoming", "upcoming", "upcoming"],
        )
        supervisor = progress["steps"][1]
        self.assertEqual(supervisor["user"], SUPERVISOR)
        self.assertTrue(supervisor["at"])
        self.assertEqual(progress["pending"]["user"], MANAGER)
        self.assertIsNone(progress["end"])

        progress = get_approval_progress(self._force_effective(doc).name)
        self.assertEqual({s["status"] for s in progress["steps"]}, {"done"})
        self.assertEqual(progress["end"], "approved")
        self.assertIsNone(progress["pending"])

    def test_progress_needs_read_access(self):
        doc = self._make_draft("GMP-Chain-Progress-Denied")
        with self.assertRaises(frappe.PermissionError):
            self._as(QA_R3, get_approval_progress, doc.name)

    def test_a_save_cannot_forge_who_it_waits_on(self):
        doc = self._make_draft("GMP-Chain-Waiting-Forge")
        doc.pending_with = QA_APPR
        doc.save(ignore_permissions=True)
        doc.reload()
        self.assertEqual(doc.pending_with, PREPARER)

    def test_upgrade_fills_waiting_on_for_documents_in_progress(self):
        from dms.patches.v2_9_0 import backfill_pending_with

        doc = self._act(self._make_draft("GMP-Chain-Waiting-Patch"), "Submit for Approval", PREPARER)
        doc.db_set({"pending_with": None, "pending_with_name": None, "pending_since": None}, update_modified=False)
        backfill_pending_with.execute()
        doc.reload()
        self.assertEqual(doc.pending_with, SUPERVISOR)
        self.assertTrue(doc.pending_since)


if __name__ == "__main__":
    unittest.main()
