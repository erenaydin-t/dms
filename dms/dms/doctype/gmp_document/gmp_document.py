# Copyright (c) 2026, ErenAydin - GMP DMS Module
# License: MIT
"""GMP Document controller.

Implements 21 CFR Part 11 / GMP-aware lifecycle for controlled documents:
deterministic naming, SHA-256 file integrity, Jinja-rendered .docx templates,
versioned amendments, and a persisted base-PDF that is dynamically watermarked
on download according to the document's current status.
"""

import hashlib
import os
import shutil
import subprocess
import tempfile
from io import BytesIO

import frappe
from frappe import _
from frappe.utils import add_months, add_years, cstr, getdate, now_datetime, today
from frappe.utils.file_manager import save_file
from frappe.utils.nestedset import NestedSet

from docx.shared import Mm
from docxtpl import DocxTemplate, InlineImage


VALIDITY_YEARS_MAP = {"2 Years": 2, "3 Years": 3, "5 Years": 5}
ALLOWED_EXTENSIONS = (".docx",)
SIGNATURE_WIDTH_MM = 40  # rendered signature width in PDF

# Workflow states ----------------------------------------------------------- #
WF_DRAFT = "Draft"
WF_UNDER_REVIEW = "Under Review"
WF_PENDING_QA = "Pending QA Approval"
WF_APPROVED = "Approved"
WF_REVISION = "Revision Requested"

WF_ALL = (WF_DRAFT, WF_UNDER_REVIEW, WF_PENDING_QA, WF_APPROVED, WF_REVISION)


class GMPDocument(NestedSet):
    nsm_parent_field = "parent_gmp_document"

    # ------------------------------------------------------------------ #
    #  Lifecycle hooks                                                   #
    # ------------------------------------------------------------------ #

    def autoname(self):
        if not self.document_type or not self.department:
            frappe.throw(_("Document Type and Department are required for naming."))

        if not frappe.db.has_column("Department", "custom_abbr"):
            frappe.throw(
                _(
                    "The 'custom_abbr' Custom Field is missing on the Department DocType. "
                    "Run 'bench --site <site> migrate' to apply the DMS bootstrap, then retry."
                )
            )

        dept_abbr = frappe.db.get_value("Department", self.department, "custom_abbr")
        if not dept_abbr:
            frappe.throw(
                _("Department {0} must have 'custom_abbr' set before naming a GMP Document.").format(
                    self.department
                )
            )

        version = self.version_number or 0

        # On amendment, retain the same logical ID; only bump the -vN suffix.
        if self.amended_from:
            base_name = self.amended_from.rsplit("-v", 1)[0]
            self.name = f"{base_name}-v{version}"
            return

        prefix = f"{self.document_type}-{dept_abbr}-"
        existing = frappe.get_all(
            "GMP Document",
            filters=[["name", "like", f"{prefix}%"]],
            pluck="name",
        )

        max_increment = 0
        for existing_name in existing:
            try:
                rest = existing_name[len(prefix):]
                inc_str = rest.split("-v")[0]
                inc = int(inc_str)
                if inc > max_increment:
                    max_increment = inc
            except (ValueError, IndexError):
                continue

        next_inc = str(max_increment + 1).zfill(2)
        self.name = f"{self.document_type}-{dept_abbr}-{next_inc}-v{version}"

    def before_insert(self):
        if not self.prepared_by:
            self.prepared_by = frappe.session.user
        if not self.workflow_status:
            self.workflow_status = WF_DRAFT

        if not self.amended_from:
            return

        old_version = frappe.db.get_value("GMP Document", self.amended_from, "version_number") or 0
        self.version_number = old_version + 1
        # Change control: a revised document must re-acquire its own
        # attachment, integrity hash, and effective date — never inherit.
        self.attachment_file = None
        self.file_integrity_hash = None
        self.effective_date = None
        self.expiry_date = None
        self.next_revision_date = None
        # The amended draft starts a fresh review cycle.
        self.workflow_status = WF_DRAFT
        self.reviewed_by = None
        self.reviewed_on = None
        self.approved_by = None
        self.approved_on = None
        self.last_revision_request = None
        self.last_revision_by = None
        self.last_revision_on = None

    def validate(self):
        if self.amended_from and not (self.reason_for_change and self.reason_for_change.strip()):
            frappe.throw(_("Reason for Change is mandatory when amending a GMP Document."))

    def before_save(self):
        self._calculate_lifecycle_dates()
        self._handle_attachment_changes()

    def before_submit(self):
        # Hard guard: submit must arrive via qa_approve(), which sets
        # workflow_status to Approved before calling doc.submit(). Without
        # this, any user with the DocType-level 'submit' permission
        # (QA Manager, System Manager) could click Frappe's standard Submit
        # button and bypass the Reviewer + QA Approver actors entirely.
        if self.workflow_status != WF_APPROVED:
            frappe.throw(
                _(
                    "This document cannot be submitted directly. It must pass "
                    "through the Reviewer and QA Approver workflow — use "
                    "'Submit for Review' under the Workflow menu."
                ),
                frappe.PermissionError,
            )

    def on_submit(self):
        if not self.attachment_file:
            frappe.throw(_("A .docx attachment is required before submitting."))

        if not self.effective_date:
            self.effective_date = today()
            self._calculate_lifecycle_dates()
            self.db_set("effective_date", self.effective_date, update_modified=False)
            self.db_set("expiry_date", self.expiry_date, update_modified=False)
            self.db_set("next_revision_date", self.next_revision_date, update_modified=False)

        self._render_and_generate_pdf()

    def on_cancel(self):
        # A cancelled controlled document is, by definition, obsolete.
        self.db_set("is_active", 0, update_modified=False)

    # ------------------------------------------------------------------ #
    #  Internal helpers                                                  #
    # ------------------------------------------------------------------ #

    def _calculate_lifecycle_dates(self):
        if not (self.effective_date and self.validity_period):
            return

        years = VALIDITY_YEARS_MAP.get(self.validity_period)
        if not years:
            return

        self.expiry_date = add_years(getdate(self.effective_date), years)
        self.next_revision_date = add_months(getdate(self.expiry_date), -1)

    def _handle_attachment_changes(self):
        if not self.attachment_file:
            return

        if not self.is_new():
            previous = self.get_doc_before_save()
            if previous and previous.attachment_file == self.attachment_file:
                return

        if not self.attachment_file.lower().endswith(ALLOWED_EXTENSIONS):
            frappe.throw(_("Only .docx files are allowed for GMP Documents."))

        file_doc = self._get_file_doc(self.attachment_file)
        physical_path = file_doc.get_full_path()

        if not os.path.exists(physical_path):
            frappe.throw(_("Attached file is missing on disk: {0}").format(self.attachment_file))

        self.file_integrity_hash = _compute_sha256(physical_path)

        # Rename the physical file to mirror the document ID for traceability.
        target_filename = f"{self.name}.docx"
        if file_doc.file_name == target_filename:
            return

        new_dir = os.path.dirname(physical_path)
        new_path = os.path.join(new_dir, target_filename)
        if os.path.exists(new_path) and os.path.abspath(new_path) != os.path.abspath(physical_path):
            os.remove(new_path)

        os.rename(physical_path, new_path)
        new_url = ("/private/files/" if file_doc.is_private else "/files/") + target_filename
        file_doc.file_name = target_filename
        file_doc.file_url = new_url
        file_doc.save(ignore_permissions=True)
        self.attachment_file = new_url

    def _render_and_generate_pdf(self):
        """Two-pass render from a single pristine source template:

        1. Clean render — every {{ field }} populated, every signature
           placeholder rendered as empty string. Becomes the canonical
           Word file users download (no signatures embedded).
        2. With-signatures render — same fields, plus inline PNG images at
           every signature placeholder. Used only as the source for the
           DOCX→PDF conversion; the intermediate file is then discarded.

        Result: the persisted base PDF carries the signatures, the saved
        .docx does not. Watermarking still happens on-demand at download
        time on top of the signed base PDF.
        """
        file_doc = self._get_file_doc(self.attachment_file)
        source_path = file_doc.get_full_path()

        if not source_path.lower().endswith(ALLOWED_EXTENSIONS):
            frappe.throw(_("Submitted attachment must be a .docx file."))

        soffice = shutil.which("soffice") or shutil.which("libreoffice")
        if not soffice:
            frappe.throw(_("LibreOffice (soffice) is not installed on the server. Cannot generate PDF."))

        with tempfile.TemporaryDirectory() as tmpdir:
            # 1. Clean render — deliverable Word file
            clean_path = os.path.join(tmpdir, f"{self.name}.docx")
            try:
                clean_template = DocxTemplate(source_path)
                clean_template.render(self._build_template_context())
                clean_template.save(clean_path)
            except Exception:
                frappe.log_error(
                    frappe.get_traceback(),
                    "GMP Document: Word template render (clean) failed",
                )
                frappe.throw(_("Failed to render Word template. Check Error Log for details."))

            # 2. With-signatures render — source for PDF
            sig_path = os.path.join(tmpdir, f"{self.name}-with-signatures.docx")
            try:
                sig_template = DocxTemplate(source_path)
                sig_context = self._build_template_context(template_for_images=sig_template)
                sig_template.render(sig_context)
                sig_template.save(sig_path)
            except Exception:
                frappe.log_error(
                    frappe.get_traceback(),
                    "GMP Document: Word template render (with signatures) failed",
                )
                frappe.throw(_("Failed to render Word template with signatures. Check Error Log."))

            # 3. DOCX → PDF (with signatures)
            try:
                subprocess.run(
                    [soffice, "--headless", "--convert-to", "pdf", "--outdir", tmpdir, sig_path],
                    capture_output=True,
                    timeout=180,
                    check=True,
                )
            except subprocess.TimeoutExpired:
                frappe.throw(_("PDF conversion timed out."))
            except subprocess.CalledProcessError as exc:
                frappe.log_error(
                    title="GMP Document: DOCX to PDF conversion failed",
                    message=(exc.stderr or b"").decode("utf-8", errors="ignore"),
                )
                frappe.throw(_("PDF conversion failed. Check Error Log for details."))

            generated_pdf = os.path.join(tmpdir, f"{self.name}-with-signatures.pdf")
            if not os.path.exists(generated_pdf):
                frappe.throw(_("PDF was not generated by LibreOffice."))

            with open(generated_pdf, "rb") as fh:
                pdf_bytes = fh.read()

            # 4. Replace the source .docx with the clean render. This is the
            # version users will download as the controlled Word file.
            shutil.copyfile(clean_path, source_path)

        # 5. SHA-256 reflects the deliverable (clean) bytes, not the signed PDF.
        self.db_set(
            "file_integrity_hash",
            _compute_sha256(source_path),
            update_modified=False,
        )

        # 6. Persist the signed PDF (replacing any prior copy).
        base_pdf_filename = f"{self.name}.pdf"
        for old in frappe.get_all(
            "File",
            filters={
                "attached_to_doctype": self.doctype,
                "attached_to_name": self.name,
                "file_name": base_pdf_filename,
            },
            pluck="name",
        ):
            frappe.delete_doc("File", old, ignore_permissions=True, force=True)

        save_file(
            fname=base_pdf_filename,
            content=pdf_bytes,
            dt=self.doctype,
            dn=self.name,
            is_private=1,
        )

    def _build_template_context(self, template_for_images=None):
        """Context dict consumed by docxtpl. Every editable GMP Document
        field is exposed as a Jinja variable so users can place any
        ``{{ field_name }}`` in the .docx template.

        Signature variables (preparer_signature, reviewer_signature,
        qa_signature) render as empty strings unless ``template_for_images``
        is supplied — in which case they become InlineImage objects sized
        to SIGNATURE_WIDTH_MM."""

        def user_full_name(user_id):
            if not user_id:
                return ""
            return frappe.db.get_value("User", user_id, "full_name") or user_id

        def employee_full_name(emp_id):
            if not emp_id:
                return ""
            return frappe.db.get_value("Employee", emp_id, "employee_name") or emp_id

        def department_full_name(dept):
            if not dept:
                return ""
            return frappe.db.get_value("Department", dept, "department_name") or dept

        context = {
            # ----- identifiers -----
            "docname": self.name or "",
            "name": self.name or "",
            # ----- names -----
            "document_name_fa": self.document_name_fa or "",
            "document_name_en": self.document_name_en or "",
            # ----- classification -----
            "document_type": self.document_type or "",
            "department": self.department or "",
            "department_name": department_full_name(self.department),
            "document_owner": self.document_owner or "",
            "document_owner_name": employee_full_name(self.document_owner),
            "gmp_impact": self.gmp_impact or "",
            "validity_period": self.validity_period or "",
            # ----- lifecycle -----
            "effective_date": cstr(self.effective_date) if self.effective_date else "",
            "expiry_date": cstr(self.expiry_date) if self.expiry_date else "",
            "next_revision_date": cstr(self.next_revision_date) if self.next_revision_date else "",
            # ----- versioning -----
            "version_number": self.version_number or 0,
            "is_active": int(bool(self.is_active)),
            "requires_training": int(bool(self.requires_training)),
            # ----- change control -----
            "reason_for_change": self.reason_for_change or "",
            # ----- workflow assignments -----
            "prepared_by": self.prepared_by or "",
            "prepared_by_name": user_full_name(self.prepared_by),
            "reviewer": self.reviewer or "",
            "reviewer_name": user_full_name(self.reviewer),
            "qa_approver": self.qa_approver or "",
            "qa_approver_name": user_full_name(self.qa_approver),
            # ----- workflow actuals -----
            "reviewed_by": self.reviewed_by or "",
            "reviewed_by_name": user_full_name(self.reviewed_by),
            "reviewed_on": cstr(self.reviewed_on) if self.reviewed_on else "",
            "approved_by": self.approved_by or "",
            "approved_by_name": user_full_name(self.approved_by),
            "approved_on": cstr(self.approved_on) if self.approved_on else "",
            "workflow_status": self.workflow_status or "",
            # ----- signatures (default: empty for clean DOCX) -----
            "preparer_signature": "",
            "reviewer_signature": "",
            "qa_signature": "",
        }

        if template_for_images is not None:
            preparer_path = _resolve_signature_path(self.prepared_by)
            reviewer_path = _resolve_signature_path(self.reviewed_by or self.reviewer)
            qa_path = _resolve_signature_path(self.approved_by or self.qa_approver)
            if preparer_path:
                context["preparer_signature"] = InlineImage(
                    template_for_images, preparer_path, width=Mm(SIGNATURE_WIDTH_MM)
                )
            if reviewer_path:
                context["reviewer_signature"] = InlineImage(
                    template_for_images, reviewer_path, width=Mm(SIGNATURE_WIDTH_MM)
                )
            if qa_path:
                context["qa_signature"] = InlineImage(
                    template_for_images, qa_path, width=Mm(SIGNATURE_WIDTH_MM)
                )

        return context

    @staticmethod
    def _get_file_doc(file_url):
        file_name = frappe.db.get_value("File", {"file_url": file_url}, "name")
        if not file_name:
            frappe.throw(_("File record not found for URL: {0}").format(file_url))
        return frappe.get_doc("File", file_name)


# ---------------------------------------------------------------------- #
#  Module-level helpers                                                  #
# ---------------------------------------------------------------------- #


def _compute_sha256(path):
    sha = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(8192), b""):
            sha.update(chunk)
    return sha.hexdigest()


def _resolve_signature_path(user_email):
    """Return the on-disk path of a user's signature image, or None.

    Resolution chain:
        User (user_email) -> Employee.user_id -> Employee.custom_signature_image
        -> File.file_url -> File.get_full_path()

    Each failure branch writes to the Error Log so a missing signature
    on a rendered PDF is diagnosable without silent data loss.
    """
    if not user_email:
        return None

    log_title = "GMP Document signature lookup"

    employee_name = frappe.db.get_value("Employee", {"user_id": user_email}, "name")
    if not employee_name:
        frappe.log_error(
            title=log_title,
            message=(
                f"No Employee record is linked to user '{user_email}'. "
                f"Set Employee.user_id to that user to enable a signature on rendered PDFs."
            ),
        )
        return None

    sig_url = frappe.db.get_value("Employee", employee_name, "custom_signature_image")
    if not sig_url:
        frappe.log_error(
            title=log_title,
            message=(
                f"Employee {employee_name} (user {user_email}) has no "
                f"'custom_signature_image' uploaded."
            ),
        )
        return None

    file_name = frappe.db.get_value("File", {"file_url": sig_url}, "name")
    if not file_name:
        frappe.log_error(
            title=log_title,
            message=(
                f"No File record matches Employee {employee_name}'s "
                f"signature URL '{sig_url}'."
            ),
        )
        return None

    physical_path = frappe.get_doc("File", file_name).get_full_path()
    if not os.path.exists(physical_path):
        frappe.log_error(
            title=log_title,
            message=(
                f"Employee {employee_name} signature missing on disk: {physical_path}"
            ),
        )
        return None

    if not physical_path.lower().endswith(".png"):
        # Spec: only PNG is accepted as a signature (transparency support).
        frappe.log_error(
            title=log_title,
            message=(
                f"Employee {employee_name} signature must be a PNG file — "
                f"got '{physical_path}'. Re-upload as .png."
            ),
        )
        return None

    return physical_path


# ---------------------------------------------------------------------- #
#  Whitelisted endpoint                                                  #
# ---------------------------------------------------------------------- #


@frappe.whitelist()
def download_watermarked_pdf(docname):
    """Stream the base PDF with a status-driven watermark.

    Watermark is resolved at call time so a status change (is_active,
    docstatus) is reflected immediately without re-rendering the PDF."""
    doc = frappe.get_doc("GMP Document", docname)
    doc.check_permission("read")

    base_pdf_filename = f"{docname}.pdf"
    base_pdf_name = frappe.db.get_value(
        "File",
        {
            "attached_to_doctype": "GMP Document",
            "attached_to_name": docname,
            "file_name": base_pdf_filename,
        },
        "name",
    )
    if not base_pdf_name:
        frappe.throw(
            _("Base PDF for {0} is not available. The document may not have been submitted yet.").format(
                docname
            )
        )

    base_pdf_path = frappe.get_doc("File", base_pdf_name).get_full_path()
    if not os.path.exists(base_pdf_path):
        frappe.throw(_("Base PDF file is missing on disk."))

    watermark_text = _resolve_watermark_text(doc)
    watermarked = _apply_watermark(base_pdf_path, watermark_text)

    safe_label = watermark_text.replace(" ", "_")
    frappe.local.response.filename = f"{docname}-{safe_label}.pdf"
    frappe.local.response.filecontent = watermarked
    frappe.local.response.type = "download"


# ---------------------------------------------------------------------- #
#  Workflow transitions                                                  #
# ---------------------------------------------------------------------- #
#
# The state machine:
#
#   Draft ── submit_for_review ──► Under Review
#   Under Review ── reviewer_approve ──► Pending QA Approval
#   Under Review ── reviewer_request_revision ──► Revision Requested
#   Pending QA Approval ── qa_approve ──► Approved (docstatus=1)
#   Pending QA Approval ── qa_request_revision ──► Under Review
#   Revision Requested ── submit_for_review ──► Under Review
#
# Each transition validates that the caller is the assigned actor
# (preparer / reviewer / qa_approver), creates a ToDo for the next
# actor, and closes the previous actor's ToDo.


@frappe.whitelist()
def submit_for_review(docname):
    doc = frappe.get_doc("GMP Document", docname)
    _ensure_actor(doc.prepared_by, "preparer")
    if doc.workflow_status not in (WF_DRAFT, WF_REVISION):
        frappe.throw(_("Document must be Draft or Revision Requested to submit for review."))
    if not doc.attachment_file:
        frappe.throw(_("Attach the .docx controlled file before submitting for review."))
    if not doc.reviewer:
        frappe.throw(_("Assign a Reviewer before submitting for review."))
    if not doc.qa_approver:
        frappe.throw(_("Assign a QA Approver before submitting for review."))

    doc.workflow_status = WF_UNDER_REVIEW
    doc.add_comment("Workflow", _("Submitted for review by {0}").format(frappe.session.user))
    doc.flags.ignore_permissions = True
    doc.save()
    _close_open_todos(doc, allocated_to=doc.prepared_by)
    _create_todo(doc, doc.reviewer, _("Review GMP Document {0}").format(doc.name))
    return doc.workflow_status


@frappe.whitelist()
def reviewer_approve(docname):
    doc = frappe.get_doc("GMP Document", docname)
    _ensure_actor(doc.reviewer, "reviewer")
    if doc.workflow_status != WF_UNDER_REVIEW:
        frappe.throw(_("Document must be Under Review."))
    if not doc.qa_approver:
        frappe.throw(_("Assign a QA Approver before approving."))

    doc.workflow_status = WF_PENDING_QA
    doc.reviewed_by = frappe.session.user
    doc.reviewed_on = now_datetime()
    doc.add_comment("Workflow", _("Reviewer approved — forwarded to QA"))
    doc.flags.ignore_permissions = True
    doc.save()
    _close_open_todos(doc, allocated_to=doc.reviewer)
    _create_todo(doc, doc.qa_approver, _("QA approval — GMP Document {0}").format(doc.name))
    return doc.workflow_status


@frappe.whitelist()
def reviewer_request_revision(docname, reason):
    doc = frappe.get_doc("GMP Document", docname)
    _ensure_actor(doc.reviewer, "reviewer")
    if doc.workflow_status != WF_UNDER_REVIEW:
        frappe.throw(_("Document must be Under Review."))
    reason = (reason or "").strip()
    if not reason:
        frappe.throw(_("Please provide a reason for the revision request."))

    doc.workflow_status = WF_REVISION
    doc.last_revision_request = reason
    doc.last_revision_by = frappe.session.user
    doc.last_revision_on = now_datetime()
    doc.add_comment("Workflow", _("Reviewer requested revision: {0}").format(reason))
    doc.flags.ignore_permissions = True
    doc.save()
    _close_open_todos(doc, allocated_to=doc.reviewer)
    _create_todo(doc, doc.prepared_by, _("Address revision request — {0}").format(doc.name))
    return doc.workflow_status


@frappe.whitelist()
def qa_approve(docname):
    doc = frappe.get_doc("GMP Document", docname)
    _ensure_actor(doc.qa_approver, "qa_approver")
    if doc.workflow_status != WF_PENDING_QA:
        frappe.throw(_("Document must be Pending QA Approval."))

    doc.workflow_status = WF_APPROVED
    doc.approved_by = frappe.session.user
    doc.approved_on = now_datetime()
    doc.add_comment("Workflow", _("QA approval granted — finalizing"))
    doc.flags.ignore_permissions = True
    doc.save()
    _close_open_todos(doc, allocated_to=doc.qa_approver)
    # Final Frappe submit (docstatus=1) — this is what triggers the
    # Word template render and base PDF generation in on_submit.
    doc.submit()
    return doc.workflow_status


@frappe.whitelist()
def qa_request_revision(docname, reason):
    doc = frappe.get_doc("GMP Document", docname)
    _ensure_actor(doc.qa_approver, "qa_approver")
    if doc.workflow_status != WF_PENDING_QA:
        frappe.throw(_("Document must be Pending QA Approval."))
    reason = (reason or "").strip()
    if not reason:
        frappe.throw(_("Please provide a reason for the revision request."))

    # QA bounces back to the reviewer (not all the way to preparer) — they
    # may re-approve after re-checking, or request revision themselves.
    doc.workflow_status = WF_UNDER_REVIEW
    doc.last_revision_request = reason
    doc.last_revision_by = frappe.session.user
    doc.last_revision_on = now_datetime()
    doc.add_comment("Workflow", _("QA requested revision: {0}").format(reason))
    doc.flags.ignore_permissions = True
    doc.save()
    _close_open_todos(doc, allocated_to=doc.qa_approver)
    _create_todo(doc, doc.reviewer, _("Re-review — QA requested revision on {0}").format(doc.name))
    return doc.workflow_status


@frappe.whitelist()
def get_my_pending_count(user=None):
    """Used by the workspace pending-count badge."""
    user = user or frappe.session.user
    counts = {
        "to_review": frappe.db.count("GMP Document", filters={
            "docstatus": 0, "workflow_status": WF_UNDER_REVIEW, "reviewer": user,
        }),
        "to_approve": frappe.db.count("GMP Document", filters={
            "docstatus": 0, "workflow_status": WF_PENDING_QA, "qa_approver": user,
        }),
        "to_revise": frappe.db.count("GMP Document", filters={
            "docstatus": 0, "workflow_status": WF_REVISION, "prepared_by": user,
        }),
    }
    counts["total"] = counts["to_review"] + counts["to_approve"] + counts["to_revise"]
    return counts


def _ensure_actor(expected_user, role_label):
    """Raise PermissionError unless the caller is the assigned actor or a System Manager."""
    if not expected_user:
        frappe.throw(_("No {0} is assigned to this document.").format(role_label))
    me = frappe.session.user
    if me == expected_user:
        return
    if "System Manager" in frappe.get_roles(me):
        return
    frappe.throw(
        _("Only the assigned {0} ({1}) can perform this action.").format(role_label, expected_user),
        frappe.PermissionError,
    )


def _create_todo(doc, allocated_to, description):
    if not allocated_to:
        return
    frappe.get_doc({
        "doctype": "ToDo",
        "allocated_to": allocated_to,
        "reference_type": doc.doctype,
        "reference_name": doc.name,
        "description": description,
        "status": "Open",
        "priority": "Medium",
    }).insert(ignore_permissions=True)


def _close_open_todos(doc, allocated_to):
    if not allocated_to:
        return
    open_todos = frappe.get_all(
        "ToDo",
        filters={
            "reference_type": doc.doctype,
            "reference_name": doc.name,
            "allocated_to": allocated_to,
            "status": "Open",
        },
        pluck="name",
    )
    for t in open_todos:
        frappe.db.set_value("ToDo", t, "status", "Closed")


# ---------------------------------------------------------------------- #
#  Tree page data source                                                 #
# ---------------------------------------------------------------------- #


@frappe.whitelist()
def get_dms_tree_children(parent=None):
    """Hierarchical data for the GMP Document Tree page.

    Levels:
        Root       -> Department (with submitted document count)
        Department -> Document Type (with submitted document count)
        Doc Type   -> latest submitted version per document family
    """
    parent = (parent or "").strip()

    if not parent:
        rows = frappe.get_all(
            "GMP Document",
            filters={"docstatus": 1},
            fields=["department"],
            distinct=True,
        )
        depts = sorted({r.department for r in rows if r.department})
        nodes = []
        for dept in depts:
            cnt = frappe.db.count(
                "GMP Document",
                filters={"docstatus": 1, "department": dept},
            )
            nodes.append({
                "value": f"Dept::{dept}",
                "title": f"{dept} ({cnt})",
                "expandable": 1,
            })
        return nodes

    if parent.startswith("Dept::"):
        dept = parent[len("Dept::"):]
        rows = frappe.get_all(
            "GMP Document",
            filters={"docstatus": 1, "department": dept},
            fields=["document_type"],
            distinct=True,
        )
        types = sorted({r.document_type for r in rows if r.document_type})
        nodes = []
        for dtype in types:
            cnt = frappe.db.count(
                "GMP Document",
                filters={"docstatus": 1, "department": dept, "document_type": dtype},
            )
            nodes.append({
                "value": f"Type::{dept}::{dtype}",
                "title": f"{dtype} ({cnt})",
                "expandable": 1,
            })
        return nodes

    if parent.startswith("Type::"):
        rest = parent[len("Type::"):]
        if "::" not in rest:
            return []
        dept, dtype = rest.split("::", 1)

        docs = frappe.get_all(
            "GMP Document",
            filters={"docstatus": 1, "department": dept, "document_type": dtype},
            fields=["name", "version_number", "document_name_en", "is_active"],
        )

        latest = {}
        for d in docs:
            base = d.name.rsplit("-v", 1)[0]
            v = d.version_number or 0
            if base not in latest or v > (latest[base].version_number or 0):
                latest[base] = d

        nodes = []
        for d in sorted(latest.values(), key=lambda x: x.name):
            title = d.name
            if d.document_name_en:
                title += f"  —  {d.document_name_en}"
            nodes.append({
                "value": d.name,
                "title": title,
                "expandable": 0,
                "indicator": "ACTIVE" if d.is_active else "OBSOLETE",
                "indicator_color": "green" if d.is_active else "red",
            })
        return nodes

    return []


@frappe.whitelist()
def download_word_document(docname):
    """Stream the clean .docx (text fields rendered, no signatures).

    The signed PDF lives at /api/method/...download_watermarked_pdf;
    this endpoint serves the Word counterpart that GMP audits can store
    without exposing actor signatures."""
    doc = frappe.get_doc("GMP Document", docname)
    doc.check_permission("read")

    if not doc.attachment_file:
        frappe.throw(_("No Word file is attached to {0}.").format(docname))

    file_doc = doc._get_file_doc(doc.attachment_file)
    physical_path = file_doc.get_full_path()
    if not os.path.exists(physical_path):
        frappe.throw(_("Word file is missing on disk."))

    with open(physical_path, "rb") as fh:
        content = fh.read()

    frappe.local.response.filename = f"{docname}.docx"
    frappe.local.response.filecontent = content
    frappe.local.response.type = "download"


def _resolve_watermark_text(doc):
    if doc.docstatus == 1 and doc.is_active:
        return "CONTROLLED COPY"
    if not doc.is_active:
        return "OBSOLETE"
    return "DRAFT - NOT FOR USE"


def _apply_watermark(pdf_path, watermark_text):
    from pypdf import PdfReader, PdfWriter
    from reportlab.lib.colors import Color
    from reportlab.pdfgen import canvas

    reader = PdfReader(pdf_path)
    writer = PdfWriter()

    for page in reader.pages:
        width = float(page.mediabox.width)
        height = float(page.mediabox.height)

        overlay_buffer = BytesIO()
        c = canvas.Canvas(overlay_buffer, pagesize=(width, height))
        c.saveState()
        c.translate(width / 2, height / 2)
        c.rotate(45)
        c.setFillColor(Color(0.85, 0.10, 0.10, alpha=0.30))
        c.setFont("Helvetica-Bold", 80)
        c.drawCentredString(0, 0, watermark_text)
        c.restoreState()
        c.save()
        overlay_buffer.seek(0)

        overlay_page = PdfReader(overlay_buffer).pages[0]
        page.merge_page(overlay_page)
        writer.add_page(page)

    output = BytesIO()
    writer.write(output)
    return output.getvalue()
