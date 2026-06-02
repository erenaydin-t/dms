# Copyright (c) 2026, ErenAydin - GMP DMS Module
# License: MIT
"""Unit tests for the GMP Document controller.

Coverage:
    - Autoname rules (per-pair increment, amendment suffix, missing abbr)
    - Amendment lifecycle (version bump + field reset)
    - Lifecycle date calculation (expiry, next revision)
    - SHA-256 integrity helper
    - Watermark resolver state machine
    - Server-side mandatory enforcement of `reason_for_change`

Integration tests that require LibreOffice and real .docx fixtures
(template rendering, base-PDF generation, watermark overlay) are marked
as `skipUnless` so the unit suite remains hermetic.
"""

import os
import shutil
import tempfile
import unittest
from unittest.mock import MagicMock

import frappe
from frappe.tests.utils import FrappeTestCase

from dms.dms.doctype.gmp_document.gmp_document import (
    VALIDITY_YEARS_MAP,
    _apply_watermark,
    _compute_sha256,
    _resolve_watermark_text,
)


TEST_DEPT = "GMP-Test-QA Department"
TEST_DEPT_ABBR = "QA"
SOFFICE_AVAILABLE = bool(shutil.which("soffice") or shutil.which("libreoffice"))


def _ensure_test_department():
    """Create a Department fixture with a known custom_abbr."""
    if not frappe.db.exists("Department", TEST_DEPT):
        dept = frappe.new_doc("Department")
        dept.department_name = TEST_DEPT
        dept.is_group = 0
        dept.flags.ignore_mandatory = True
        dept.flags.ignore_permissions = True
        dept.insert(ignore_permissions=True)
    frappe.db.set_value("Department", TEST_DEPT, "custom_abbr", TEST_DEPT_ABBR)
    frappe.db.commit()


def _purge_test_documents():
    for name in frappe.get_all(
        "GMP Document",
        filters=[["document_name_en", "like", "GMP-Test-%"]],
        pluck="name",
    ):
        try:
            doc = frappe.get_doc("GMP Document", name)
            if doc.docstatus == 1:
                doc.flags.ignore_permissions = True
                doc.cancel()
            frappe.delete_doc("GMP Document", name, ignore_permissions=True, force=True)
        except Exception:
            pass
    frappe.db.commit()


class TestGMPDocument(FrappeTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        _ensure_test_department()
        _purge_test_documents()

    @classmethod
    def tearDownClass(cls):
        _purge_test_documents()
        super().tearDownClass()

    def tearDown(self):
        _purge_test_documents()

    # ------------------------------------------------------------------ #
    #  Helpers                                                           #
    # ------------------------------------------------------------------ #

    def _build_doc(self, **overrides):
        defaults = {
            "doctype": "GMP Document",
            "document_name_fa": "تست",
            "document_name_en": "GMP-Test-Default",
            "document_type": "SOP",
            "department": TEST_DEPT,
            "gmp_impact": "Major",
            "validity_period": "3 Years",
            "version_number": 0,
        }
        defaults.update(overrides)
        doc = frappe.new_doc("GMP Document")
        doc.update(defaults)
        return doc

    def _approve_via_workflow(self, doc):
        """Drive a draft through the native Workflow to Approved (submitted).

        Transitions go through Frappe's apply_workflow (the same path the
        "Actions" menu uses). Tests run as Administrator, which satisfies every
        transition role and the per-actor conditions' Administrator escape."""
        from frappe.model.workflow import apply_workflow

        apply_workflow(doc, "Submit for Review")
        apply_workflow(doc, "Approve as Reviewer")
        apply_workflow(doc, "Approve as QA")
        doc.reload()
        return doc

    # ------------------------------------------------------------------ #
    #  Autoname                                                          #
    # ------------------------------------------------------------------ #

    def test_autoname_first_doc_in_pair(self):
        doc = self._build_doc(document_name_en="GMP-Test-First")
        doc.insert(ignore_permissions=True)
        self.assertEqual(doc.name, f"SOP-{TEST_DEPT_ABBR}-01-v0")

    def test_autoname_increments_per_type_dept_pair(self):
        d1 = self._build_doc(document_name_en="GMP-Test-Inc-1")
        d1.insert(ignore_permissions=True)
        d2 = self._build_doc(document_name_en="GMP-Test-Inc-2")
        d2.insert(ignore_permissions=True)

        self.assertEqual(d1.name, f"SOP-{TEST_DEPT_ABBR}-01-v0")
        self.assertEqual(d2.name, f"SOP-{TEST_DEPT_ABBR}-02-v0")

    def test_autoname_separate_increments_for_different_types(self):
        sop = self._build_doc(document_type="SOP", document_name_en="GMP-Test-SOP")
        sop.insert(ignore_permissions=True)
        wi = self._build_doc(document_type="WI", document_name_en="GMP-Test-WI")
        wi.insert(ignore_permissions=True)

        self.assertEqual(sop.name, f"SOP-{TEST_DEPT_ABBR}-01-v0")
        self.assertEqual(wi.name, f"WI-{TEST_DEPT_ABBR}-01-v0")

    def test_autoname_amended_doc_keeps_base_id(self):
        original = self._build_doc(document_name_en="GMP-Test-Amend-Origin")
        original.insert(ignore_permissions=True)

        amended = self._build_doc(document_name_en="GMP-Test-Amend-V1")
        amended.amended_from = original.name
        amended.reason_for_change = "Procedural correction per CAPA-2026-001"
        amended.before_insert()
        amended.autoname()

        base = original.name.rsplit("-v", 1)[0]
        self.assertEqual(amended.name, f"{base}-v1")
        self.assertEqual(amended.version_number, 1)

    def test_autoname_throws_when_dept_missing_abbr(self):
        no_abbr_dept = "GMP-Test-NoAbbr Department"
        if not frappe.db.exists("Department", no_abbr_dept):
            d = frappe.new_doc("Department")
            d.department_name = no_abbr_dept
            d.flags.ignore_mandatory = True
            d.insert(ignore_permissions=True)
        frappe.db.set_value("Department", no_abbr_dept, "custom_abbr", "")
        frappe.db.commit()

        doc = self._build_doc(department=no_abbr_dept, document_name_en="GMP-Test-NoAbbr")
        with self.assertRaises(frappe.ValidationError):
            doc.insert(ignore_permissions=True)

    # ------------------------------------------------------------------ #
    #  Amendment lifecycle                                               #
    # ------------------------------------------------------------------ #

    def test_amendment_resets_fields_and_bumps_version(self):
        original = self._build_doc(document_name_en="GMP-Test-Reset-Origin")
        original.insert(ignore_permissions=True)
        # Simulate a submitted-but-not-rendered original
        frappe.db.set_value("GMP Document", original.name, "version_number", 0)

        amended = self._build_doc(document_name_en="GMP-Test-Reset-V1")
        amended.amended_from = original.name
        amended.attachment_file = "/private/files/should-be-cleared.docx"
        amended.file_integrity_hash = "deadbeef" * 8
        amended.effective_date = "2026-04-01"
        amended.expiry_date = "2029-04-01"
        amended.next_revision_date = "2029-03-01"

        amended.before_insert()

        self.assertEqual(amended.version_number, 1)
        self.assertIsNone(amended.attachment_file)
        self.assertIsNone(amended.file_integrity_hash)
        self.assertIsNone(amended.effective_date)
        self.assertIsNone(amended.expiry_date)
        self.assertIsNone(amended.next_revision_date)

    def test_amendment_requires_reason_for_change(self):
        original = self._build_doc(document_name_en="GMP-Test-ReasonReq-Origin")
        original.insert(ignore_permissions=True)

        amended = self._build_doc(document_name_en="GMP-Test-ReasonReq-V1")
        amended.amended_from = original.name
        amended.reason_for_change = ""  # missing

        with self.assertRaises(frappe.ValidationError):
            amended.insert(ignore_permissions=True)

    # ------------------------------------------------------------------ #
    #  Lifecycle date calculation                                        #
    # ------------------------------------------------------------------ #

    def test_expiry_and_revision_date_calculation(self):
        doc = self._build_doc(validity_period="3 Years")
        doc.effective_date = "2026-01-15"
        doc._calculate_lifecycle_dates()

        self.assertEqual(str(doc.expiry_date), "2029-01-15")
        self.assertEqual(str(doc.next_revision_date), "2028-12-15")

    def test_lifecycle_dates_skipped_without_effective_date(self):
        doc = self._build_doc()
        doc.effective_date = None
        doc.expiry_date = None
        doc.next_revision_date = None
        doc._calculate_lifecycle_dates()

        self.assertIsNone(doc.expiry_date)
        self.assertIsNone(doc.next_revision_date)

    def test_validity_period_map_covers_all_options(self):
        # Guards against future Select-option drift.
        self.assertEqual(VALIDITY_YEARS_MAP, {"2 Years": 2, "3 Years": 3, "5 Years": 5})

    # ------------------------------------------------------------------ #
    #  SHA-256 integrity                                                 #
    # ------------------------------------------------------------------ #

    def test_compute_sha256_is_deterministic_and_correct_length(self):
        with tempfile.NamedTemporaryFile(delete=False, suffix=".bin") as fh:
            fh.write(b"GMP integrity test content")
            path = fh.name
        try:
            digest = _compute_sha256(path)
            self.assertEqual(len(digest), 64)
            self.assertEqual(digest, _compute_sha256(path))
        finally:
            os.unlink(path)

    def test_compute_sha256_changes_with_content(self):
        with tempfile.NamedTemporaryFile(delete=False) as fh:
            fh.write(b"alpha")
            path_a = fh.name
        with tempfile.NamedTemporaryFile(delete=False) as fh:
            fh.write(b"beta")
            path_b = fh.name
        try:
            self.assertNotEqual(_compute_sha256(path_a), _compute_sha256(path_b))
        finally:
            os.unlink(path_a)
            os.unlink(path_b)

    # ------------------------------------------------------------------ #
    #  Watermark state machine                                           #
    # ------------------------------------------------------------------ #

    def test_watermark_controlled_copy(self):
        doc = MagicMock(docstatus=1, is_active=1)
        self.assertEqual(_resolve_watermark_text(doc), "CONTROLLED COPY")

    def test_watermark_obsolete_when_inactive(self):
        doc = MagicMock(docstatus=1, is_active=0)
        self.assertEqual(_resolve_watermark_text(doc), "OBSOLETE")

    def test_watermark_obsolete_overrides_draft_state(self):
        # Cancelled draft (docstatus=2) is also inactive -> OBSOLETE wins.
        doc = MagicMock(docstatus=2, is_active=0)
        self.assertEqual(_resolve_watermark_text(doc), "OBSOLETE")

    def test_watermark_draft_for_unsubmitted_active(self):
        doc = MagicMock(docstatus=0, is_active=1)
        self.assertEqual(_resolve_watermark_text(doc), "DRAFT - NOT FOR USE")

    # ------------------------------------------------------------------ #
    #  Watermark overlay (integration - requires reportlab + pypdf)      #
    # ------------------------------------------------------------------ #

    def test_apply_watermark_returns_pdf_bytes(self):
        from reportlab.lib.pagesizes import A4
        from reportlab.pdfgen import canvas

        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as fh:
            c = canvas.Canvas(fh.name, pagesize=A4)
            c.drawString(100, 750, "Base content")
            c.save()
            base_path = fh.name

        try:
            output = _apply_watermark(base_path, "CONTROLLED COPY")
            self.assertTrue(output.startswith(b"%PDF"))
            self.assertGreater(len(output), 0)
        finally:
            os.unlink(base_path)

    # ------------------------------------------------------------------ #
    #  End-to-end submit (skipped unless LibreOffice is on PATH)         #
    # ------------------------------------------------------------------ #

    def _make_docx_file(self, fname="test_template.docx"):
        """Persist a minimal valid .docx as a private File, or skip if
        python-docx is unavailable."""
        try:
            from docx import Document as DocxDocument
        except ImportError:
            self.skipTest("python-docx not installed; skipping end-to-end test")

        with tempfile.TemporaryDirectory() as tmpdir:
            docx_path = os.path.join(tmpdir, fname)
            d = DocxDocument()
            d.add_paragraph("Document: {{ docname }}")
            d.add_paragraph("Version: {{ version_number }}")
            d.save(docx_path)
            with open(docx_path, "rb") as fh:
                return frappe.get_doc({
                    "doctype": "File",
                    "file_name": fname,
                    "is_private": 1,
                    "content": fh.read(),
                }).insert(ignore_permissions=True)

    def _make_submitted_doc(self, en):
        """Create a GMP Document, attach a real .docx, and drive it through the
        native workflow to Approved/submitted. Returns the reloaded doc."""
        file_doc = self._make_docx_file(fname=f"{en}.docx")
        doc = self._build_doc(document_name_en=en)
        doc.attachment_file = file_doc.file_url
        doc.reviewer = "Administrator"
        doc.qa_approver = "Administrator"
        doc.insert(ignore_permissions=True)
        return self._approve_via_workflow(doc)

    @unittest.skipUnless(SOFFICE_AVAILABLE, "LibreOffice (soffice) not found on PATH")
    def test_submit_persists_base_pdf(self):
        doc = self._make_submitted_doc("GMP-Test-E2E")

        self.assertEqual(doc.docstatus, 1)
        self.assertIsNotNone(doc.effective_date)
        self.assertIsNotNone(doc.file_integrity_hash)
        self.assertEqual(len(doc.file_integrity_hash), 64)

        base_pdf = frappe.db.exists(
            "File",
            {
                "attached_to_doctype": "GMP Document",
                "attached_to_name": doc.name,
                "file_name": f"{doc.name}.pdf",
            },
        )
        self.assertTrue(base_pdf, "Base PDF should be persisted on submit")

    # ------------------------------------------------------------------ #
    #  Issue #3 — amendment naming via the real insert path              #
    # ------------------------------------------------------------------ #

    def test_amend_insert_produces_versioned_name(self):
        # The 'Default Naming' amend rule must let our autoname() run via the
        # real insert path so the name becomes …-v1, not the framework's
        # …-v0-1 counter. Hermetic: no submit/LibreOffice needed — the
        # predecessor is forced to a cancelled docstatus so it is amendable.
        from dms.install import _ensure_amend_naming_rule

        _ensure_amend_naming_rule()

        original = self._build_doc(document_name_en="GMP-Test-AmendName")
        original.insert(ignore_permissions=True)
        frappe.db.set_value("GMP Document", original.name, "docstatus", 2)

        amended = frappe.copy_doc(original)
        amended.amended_from = original.name
        amended.reason_for_change = "CAPA-2026-002 procedural fix"
        amended.insert(ignore_permissions=True)

        base = original.name.rsplit("-v", 1)[0]
        self.assertEqual(amended.name, f"{base}-v1")
        self.assertNotIn("-v0-1", amended.name)
        self.assertEqual(amended.version_number, 1)

    # ------------------------------------------------------------------ #
    #  Issue #2 — cancel transitions status to Obsolete                  #
    # ------------------------------------------------------------------ #

    @unittest.skipUnless(SOFFICE_AVAILABLE, "LibreOffice (soffice) not found on PATH")
    def test_cancel_sets_obsolete_status(self):
        doc = self._make_submitted_doc("GMP-Test-Cancel")
        doc.cancel()
        doc.reload()

        self.assertEqual(doc.docstatus, 2)
        self.assertEqual(doc.is_active, 0)
        self.assertEqual(doc.workflow_status, "Obsolete")

    # ------------------------------------------------------------------ #
    #  Issue #4 — references repoint to the new version on amend         #
    # ------------------------------------------------------------------ #

    @unittest.skipUnless(SOFFICE_AVAILABLE, "LibreOffice (soffice) not found on PATH")
    def test_amend_repoints_dependent_references(self):
        from dms.install import _ensure_amend_naming_rule
        from frappe.model.workflow import apply_workflow

        _ensure_amend_naming_rule()

        target = self._make_submitted_doc("GMP-Test-RefTarget")

        # A dependent document referencing the target version.
        dependent = self._make_docx_file(fname="GMP-Test-RefDependent.docx")
        dep = self._build_doc(document_name_en="GMP-Test-RefDependent")
        dep.attachment_file = dependent.file_url
        dep.reviewer = "Administrator"
        dep.qa_approver = "Administrator"
        dep.append("references", {"referenced_document": target.name, "reference_type": "References"})
        dep.insert(ignore_permissions=True)
        dep = self._approve_via_workflow(dep)

        # Cancel + amend the target — link validation must not block this.
        target.cancel()
        amended = frappe.copy_doc(target)
        amended.amended_from = target.name
        amended.reason_for_change = "Revised per change control"
        amended.attachment_file = self._make_docx_file(fname="GMP-Test-RefTarget-v1.docx").file_url
        amended.insert(ignore_permissions=True)
        amended = self._approve_via_workflow(amended)

        # The dependent's reference row should now point at the new version.
        repointed = frappe.get_all(
            "GMP Document Reference",
            filters={"parent": dep.name},
            pluck="referenced_document",
        )
        self.assertIn(amended.name, repointed)
        self.assertNotIn(target.name, repointed)

    # ------------------------------------------------------------------ #
    #  Issue #1 — base PDF regenerates when the File record is missing   #
    # ------------------------------------------------------------------ #

    @unittest.skipUnless(SOFFICE_AVAILABLE, "LibreOffice (soffice) not found on PATH")
    def test_download_regenerates_missing_base_pdf(self):
        from dms.dms.doctype.gmp_document.gmp_document import download_watermarked_pdf

        doc = self._make_submitted_doc("GMP-Test-Regen")

        # Delete the persisted base PDF to simulate the Issue #1 failure mode.
        for pdf in frappe.get_all(
            "File",
            filters={
                "attached_to_doctype": "GMP Document",
                "attached_to_name": doc.name,
                "file_name": ["like", "%.pdf"],
            },
            pluck="name",
        ):
            frappe.delete_doc("File", pdf, ignore_permissions=True, force=True)

        # Should regenerate transparently instead of throwing.
        download_watermarked_pdf(doc.name)
        self.assertEqual(frappe.local.response.type, "download")
        self.assertTrue(frappe.local.response.filecontent.startswith(b"%PDF"))
