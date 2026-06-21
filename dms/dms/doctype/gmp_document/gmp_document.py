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
from frappe.utils import add_months, add_years, cint, cstr, get_files_path, getdate, now_datetime, today
from frappe.utils.file_manager import save_file
from frappe.utils.nestedset import NestedSet
from frappe.core.doctype.file.utils import get_content_hash

from docx.shared import Mm
from docxtpl import DocxTemplate, InlineImage


# Roles that read and operate on every GMP Document regardless of department or
# creator. "DMS Manager" is the module-owner/admin role (full CRUD + cancel);
# "QA Manager" drives the review/approval workflow; "System Manager" is the
# Frappe super-admin. Everyone else (a plain "Employee") is a read-only
# consumer scoped to their own department's approved, active documents — see
# get_permission_query_conditions() / has_permission().
UNRESTRICTED_ROLES = frozenset({"System Manager", "QA Manager", "DMS Manager"})

# Upper bound on get_document_reference_tree recursion, so a hand-crafted depth
# argument over the whitelisted endpoint can't drive runaway traversal of a
# large/dense reference graph. Per-path cycle detection prevents loops; this
# caps how deep any single path is expanded.
MAX_REFERENCE_TREE_DEPTH = 10

VALIDITY_YEARS_MAP = {"2 Years": 2, "3 Years": 3, "5 Years": 5}
ALLOWED_EXTENSIONS = (".docx",)
SIGNATURE_WIDTH_MM = 40  # rendered signature width in PDF
# python-docx/InlineImage embeds these raster formats reliably. PNG is
# preferred (transparency); JPG/JPEG accepted because HR uploads vary.
ALLOWED_SIGNATURE_EXTENSIONS = (".png", ".jpg", ".jpeg")


def document_type_label(code):
    """Human label for a GMP Document Type code (the link stores the code, which
    is the master's name). Falls back to the code itself if unresolved."""
    if not code:
        return ""
    return frappe.db.get_value("GMP Document Type", code, "type_name") or code


# Single source of truth for the values a Word template may pull in. Each entry
# is (context_key, human_label). The context_key must exist in
# _build_template_context(); the label drives the mapping UI dropdown. The three
# *_signature keys resolve to an inline image in the signed-PDF render pass and
# to an empty string in the clean .docx (mirroring the native signature tags).
TEMPLATE_FIELDS = [
    ("docname", "Document ID"),
    ("document_name_fa", "Document Name (FA)"),
    ("document_name_en", "Document Name (EN)"),
    ("document_type", "Document Type (label)"),
    ("document_type_code", "Document Type (code)"),
    ("department", "Department (ID)"),
    ("department_name", "Department Name"),
    ("document_owner", "Document Owner (ID)"),
    ("document_owner_name", "Document Owner Name"),
    ("gmp_impact", "GMP Impact"),
    ("validity_period", "Validity Period"),
    ("effective_date", "Effective Date"),
    ("expiry_date", "Expiry Date"),
    ("next_revision_date", "Next Revision Date"),
    ("version_number", "Version Number"),
    ("is_active", "Is Active"),
    ("requires_training", "Requires Training"),
    ("reason_for_change", "Reason for Change"),
    ("prepared_by", "Prepared By (user)"),
    ("prepared_by_name", "Prepared By (name)"),
    ("reviewer", "Reviewer (user)"),
    ("reviewer_name", "Reviewer (name)"),
    ("qa_approver", "QA Approver (user)"),
    ("qa_approver_name", "QA Approver (name)"),
    ("reviewed_by", "Reviewed By (user)"),
    ("reviewed_by_name", "Reviewed By (name)"),
    ("reviewed_on", "Reviewed On"),
    ("approved_by", "Approved By (user)"),
    ("approved_by_name", "Approved By (name)"),
    ("approved_on", "Approved On"),
    ("workflow_status", "Workflow Status"),
    ("preparer_signature", "Preparer Signature (image)"),
    ("reviewer_signature", "Reviewer Signature (image)"),
    ("qa_signature", "QA Approver Signature (image)"),
]

TEMPLATE_FIELD_KEYS = frozenset(key for key, _label in TEMPLATE_FIELDS)


@frappe.whitelist()
def get_template_field_catalog():
    """Mappable system fields for the GMP Word Template mapping UI.

    Returns the catalog as [{"value": key, "label": label}] so the client can
    populate the `system_field` Select from a single Python source of truth,
    keeping it in lockstep with _build_template_context()."""
    return [{"value": key, "label": label} for key, label in TEMPLATE_FIELDS]


@frappe.whitelist()
def check_signature(user):
    """Client-side pre-check for the Reviewer / QA Approver fields: report
    whether ``user`` has a usable signature image. Returns
    {"ok": bool, "message": str}. The authoritative enforcement is
    GMPDocument._validate_signatures() on save/submit."""
    if not user:
        return {"ok": True, "message": ""}
    issue = _signature_issue(user)
    if not issue:
        return {"ok": True, "message": ""}
    full_name = frappe.db.get_value("User", user, "full_name") or user
    return {"ok": False, "message": _("{0} has no usable signature: {1}.").format(full_name, issue)}


# Workflow states ----------------------------------------------------------- #
WF_DRAFT = "Draft"
WF_UNDER_REVIEW = "Under Review"
WF_PENDING_QA = "Pending QA Approval"
WF_APPROVED = "Approved"
WF_REVISION = "Revision Requested"
WF_OBSOLETE = "Obsolete"

WF_ALL = (WF_DRAFT, WF_UNDER_REVIEW, WF_PENDING_QA, WF_APPROVED, WF_REVISION, WF_OBSOLETE)


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

        # document_type links to GMP Document Type, whose record name *is* the
        # short code (e.g. "BMR"), so it is already filesystem/name safe.
        type_code = self.document_type
        prefix = f"{type_code}-{dept_abbr}-"
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
        self.name = f"{type_code}-{dept_abbr}-{next_inc}-v{version}"

    def before_insert(self):
        if not self.prepared_by:
            self.prepared_by = frappe.session.user
        if not self.workflow_status:
            self.workflow_status = WF_DRAFT

        if not self.amended_from:
            return

        predecessor = frappe.db.get_value(
            "GMP Document",
            self.amended_from,
            ["version_number"],
            as_dict=True,
        ) or frappe._dict()
        self.version_number = (predecessor.version_number or 0) + 1
        # Change control: a revised document must re-acquire its own controlled
        # file, so clear an attachment that was carried over from the
        # predecessor by the amend — but never one the user uploaded for this
        # revision. The distinction is made by File *ownership*, not URL string:
        # Frappe deduplicates uploads by content hash, so a freshly attached
        # .docx can be handed the predecessor's file_url while being a distinct
        # File attached to this new document. A string compare wrongly treated
        # that as inherited and nulled it, so the mandatory check then failed
        # with "Value missing for Attachment (.docx)". An inherited file is one
        # whose File record is still attached to the predecessor.
        inherited_attachment = bool(
            self.attachment_file
            and frappe.db.exists(
                "File",
                {
                    "file_url": self.attachment_file,
                    "attached_to_doctype": self.doctype,
                    "attached_to_name": self.amended_from,
                },
            )
        )
        if not self.attachment_file or inherited_attachment:
            self.attachment_file = None
            self.file_integrity_hash = None
        self.effective_date = None
        self.expiry_date = None
        self.next_revision_date = None
        # The amended draft starts a fresh review cycle. It is also the new
        # active version — the predecessor was amended from a cancelled
        # (is_active=0) doc, so re-assert active here rather than inherit 0.
        self.is_active = 1
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
        self._check_circular_references()
        self._validate_signatures()

    def _validate_signatures(self):
        """Block save/submit unless the assigned Reviewer and QA Approver each
        have a usable signature image, so an approved document can never be
        rendered with a missing reviewer/QA signature. Resolution mirrors
        _resolve_signature_path (Employee.user_id -> custom_signature_image ->
        a PNG/JPG/JPEG File on disk)."""
        for fieldname, label in (("reviewer", _("Reviewer")), ("qa_approver", _("QA Approver"))):
            user = self.get(fieldname)
            if not user:
                continue
            issue = _signature_issue(user)
            if issue:
                full_name = frappe.db.get_value("User", user, "full_name") or user
                frappe.throw(
                    _(
                        "{0} {1} cannot be used until a signature is configured: {2}. "
                        "Upload a PNG/JPG/JPEG signature image on their Employee record "
                        "(Employee → Signature) before saving."
                    ).format(label, frappe.bold(full_name), issue),
                    title=_("Missing Signature"),
                )

    def _check_circular_references(self):
        """Prevent circular reference chains (A → B → A)."""
        if not self.references:
            return

        def _dfs(doc_name, visited):
            if doc_name in visited:
                frappe.throw(
                    _("Circular reference detected: document {0} is already in the reference chain.").format(doc_name)
                )
            visited.add(doc_name)
            children = frappe.get_all(
                "GMP Document Reference",
                filters={"parent": doc_name},
                pluck="referenced_document",
            )
            for child in children:
                _dfs(child, visited.copy())

        for row in self.references:
            if row.referenced_document == self.name:
                frappe.throw(_("A document cannot reference itself."))
            _dfs(row.referenced_document, {self.name})

    def before_save(self):
        self._calculate_lifecycle_dates()
        self._handle_attachment_changes()

    def before_submit(self):
        # Hard guard: submit must arrive via the native "Approve as QA"
        # workflow transition, whose target state (Approved) has doc_status=1
        # and therefore auto-submits — by which point workflow_status is
        # already Approved. Without this, any user with the DocType-level
        # 'submit' permission (QA Manager, System Manager) could click
        # Frappe's standard Submit button and bypass the Reviewer + QA
        # Approver actors entirely.
        if self.workflow_status != WF_APPROVED:
            frappe.throw(
                _(
                    "This document cannot be submitted directly. It must pass "
                    "through the Reviewer and QA Approver workflow — use the "
                    "actions in the workflow 'Actions' menu."
                ),
                frappe.PermissionError,
            )

    def on_update(self):
        # Workflow transitions arrive via Frappe's native apply_workflow(),
        # which save()s the doc — so audit stamping and ToDo housekeeping that
        # used to live in the (now removed) custom transition endpoints is
        # driven here by detecting a change in workflow_status.
        self._apply_workflow_side_effects()

    def on_submit(self):
        if not self.word_template:
            frappe.throw(_("A Word Template must be selected before submitting."))
        if not self.attachment_file:
            frappe.throw(_("A .docx attachment is required before submitting."))

        # Stamp the approver *before* the PDF render so the approver's signature
        # is resolved and embedded. on_submit fires only on the QA-approval
        # transition (before_submit enforces workflow_status == Approved), so the
        # submitting session user is the QA approver. Doing it here — rather than
        # relying solely on the on_update workflow side-effect, which runs in the
        # same save and can be skipped — guarantees approved_by is populated when
        # _render_and_generate_pdf() builds the signature context.
        self._stamp_approver()

        if not self.effective_date:
            self.effective_date = today()
            self._calculate_lifecycle_dates()
            self.db_set("effective_date", self.effective_date, update_modified=False)
            self.db_set("expiry_date", self.expiry_date, update_modified=False)
            self.db_set("next_revision_date", self.next_revision_date, update_modified=False)

        self._render_and_generate_pdf()
        # Now that this revision is officially approved, swing any dependent
        # references off the superseded version and onto this one.
        self._repoint_references_to_self()

    def _stamp_approver(self):
        """Record the QA approver (and timestamp) if not already set.

        Idempotent: when the on_update workflow side-effect already stamped
        approved_by, this is a no-op; otherwise it fills it from the submitting
        session user. Either way approved_by is guaranteed populated before the
        signature render so the approver's signature is embedded in the PDF."""
        if not self.approved_by:
            self.approved_by = frappe.session.user
            if not self.is_new():
                self.db_set("approved_by", self.approved_by, update_modified=False)
        if not self.approved_on:
            self.approved_on = now_datetime()
            if not self.is_new():
                self.db_set("approved_on", self.approved_on, update_modified=False)

    def before_cancel(self):
        # A controlled document is routinely listed in the `references` child
        # table of other GMP Documents. Without this exemption Frappe's
        # check_no_back_links_exist() blocks cancellation (and therefore the
        # cancel-and-amend revision flow) whenever the document is referenced
        # by a submitted peer. Revision is a first-class GMP lifecycle event,
        # so we opt GMP Document references out of the cancel-time link guard;
        # the new version then repoints dependents in
        # _repoint_references_to_self(). It must be an *instance* attribute —
        # the guard reads it via doc.get(), which ignores class attributes —
        # and set here, before check_no_back_links_exist() runs post-cancel.
        self.ignore_linked_doctypes = ("GMP Document",)

    def on_cancel(self):
        # A cancelled controlled document is, by definition, obsolete. Reflect
        # that in both the active flag and the lifecycle status field so the
        # native workflow badge stops reading "Approved".
        self.db_set("is_active", 0, update_modified=False)
        self.db_set("workflow_status", WF_OBSOLETE, update_modified=False)

    def copy_attachments_from_amended_from(self):
        # GMP change control: a revised document must re-acquire its own
        # controlled file — before_insert() deliberately clears attachment_file
        # so nothing is inherited. We therefore override Frappe's default amend
        # behaviour, which would (a) carry the predecessor's File forward
        # against policy, and (b) raise FileNotFoundError when the predecessor's
        # physical .docx is no longer on disk, crashing the whole amendment.
        return

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

        previous_url = None
        if not self.is_new():
            previous = self.get_doc_before_save()
            if previous:
                if previous.attachment_file == self.attachment_file:
                    return
                previous_url = previous.attachment_file

        if not self.attachment_file.lower().endswith(ALLOWED_EXTENSIONS):
            frappe.throw(_("Only .docx files are allowed for GMP Documents."))

        controlled_url = f"/private/files/{self.name}.docx"
        if self.attachment_file == controlled_url:
            # Already this document's own controlled file (e.g. a re-save with no
            # new upload) — just keep the integrity hash current.
            controlled = self._get_file_doc(self.attachment_file)
            path = controlled.get_full_path()
            if not os.path.exists(path):
                frappe.throw(_("Attached file is missing on disk: {0}").format(self.attachment_file))
            self.file_integrity_hash = _compute_sha256(path)
            return

        src = self._get_file_doc(self.attachment_file)
        src_path = src.get_full_path()
        if not os.path.exists(src_path):
            frappe.throw(_("Attached file is missing on disk: {0}").format(self.attachment_file))
        with open(src_path, "rb") as fh:
            content = fh.read()

        # Promote the upload to this document's OWN controlled .docx. We copy the
        # bytes into a per-document physical file and own its File record rather
        # than renaming the uploaded file in place, because Frappe deduplicates
        # uploads by content hash: identical or derived uploads (e.g. each
        # version started from the same base file) are pointed at a single shared
        # physical file, and renaming / overwriting it then bleeds one document's
        # content into another document's render. Owning an independent file
        # decouples this document from that sharing.
        controlled = self._set_controlled_file(content)
        self.attachment_file = controlled.file_url
        self.file_integrity_hash = _compute_sha256(controlled.get_full_path())

        self._purge_superseded_attachments(
            keep=controlled.name, previous_url=previous_url, also_remove=[src.name]
        )
        frappe.clear_document_cache(self.doctype, self.name)

    def _set_controlled_file(self, content):
        """Persist ``content`` as this document's controlled .docx in a physical
        file owned solely by this document, with a File record whose
        ``content_hash`` is kept in sync. Returns the File doc.

        Deliberately bypasses Frappe's content-hash deduplication (it writes the
        file directly and inserts/updates the File row without going through
        File.save): dedup can otherwise point several documents at one physical
        file which — combined with the in-place clean-render overwrite that left
        File.content_hash stale — caused one document's PDF to contain another
        document's content."""
        fname = f"{self.name}.docx"
        url = f"/private/files/{fname}"
        path = get_files_path(fname, is_private=1)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(content)
        chash = get_content_hash(content)

        rows = frappe.get_all(
            "File",
            filters={
                "attached_to_doctype": self.doctype,
                "attached_to_name": self.name,
                "file_name": fname,
            },
            pluck="name",
        )
        if rows:
            keep = rows[0]
            for extra in rows[1:]:
                frappe.delete_doc("File", extra, force=True, ignore_permissions=True)
            frappe.db.set_value(
                "File",
                keep,
                {
                    "file_url": url,
                    "is_private": 1,
                    "file_size": len(content),
                    "content_hash": chash,
                },
                update_modified=False,
            )
            return frappe.get_doc("File", keep)

        f = frappe.new_doc("File")
        f.name = frappe.generate_hash(length=10)
        f.update(
            {
                "file_name": fname,
                "file_url": url,
                "is_private": 1,
                "attached_to_doctype": self.doctype,
                "attached_to_name": self.name,
                "file_size": len(content),
                "content_hash": chash,
            }
        )
        # db_insert bypasses File.save -> no dedup, no filesystem rewrite; we own
        # both the physical file (written above) and the row.
        f.db_insert()
        return f

    def _purge_superseded_attachments(self, keep, previous_url, also_remove=None):
        """Delete File rows left behind by an attachment change — the original
        upload, the previous controlled file, and any other .docx attached to
        this document — keeping only the File we just promoted. Frappe guards the
        physical delete when another File row references the same path, so a
        shared (deduplicated) upload never removes another document's bytes."""
        stale = set(also_remove or [])
        if previous_url:
            stale.update(frappe.get_all("File", filters={"file_url": previous_url}, pluck="name"))
        stale.update(
            frappe.get_all(
                "File",
                filters={
                    "attached_to_doctype": self.doctype,
                    "attached_to_name": self.name,
                    "file_name": ["like", "%.docx"],
                },
                pluck="name",
            )
        )
        stale.discard(keep)
        for name in stale:
            # force bypasses link checks; ignore_permissions because this runs
            # inside the controlled save, not as the end user.
            frappe.delete_doc("File", name, force=True, ignore_permissions=True)

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

        The render source is the user's uploaded attachment; the linked Word
        Template supplies only the custom-tag -> system-field mappings applied
        during the render.
        """
        source_path, field_mappings = self._resolve_render_source()

        if not source_path.lower().endswith(ALLOWED_EXTENSIONS):
            frappe.throw(_("Render source must be a .docx file."))

        soffice = shutil.which("soffice") or shutil.which("libreoffice")
        if not soffice:
            frappe.throw(_("LibreOffice (soffice) is not installed on the server. Cannot generate PDF."))

        with tempfile.TemporaryDirectory() as tmpdir:
            # 1. Clean render — deliverable Word file
            clean_path = os.path.join(tmpdir, f"{self.name}.docx")
            try:
                clean_template = DocxTemplate(source_path)
                clean_template.render(self._build_template_context(field_mappings=field_mappings))
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
                sig_context = self._build_template_context(
                    template_for_images=sig_template, field_mappings=field_mappings
                )
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
            with open(clean_path, "rb") as fh:
                clean_bytes = fh.read()

        # 4. Replace the controlled .docx with the clean render (the deliverable
        # Word file). Routed through _set_controlled_file so the File's
        # content_hash is updated too — an in-place overwrite would leave it
        # stale and poison Frappe's dedup for later uploads.
        self._set_controlled_file(clean_bytes)

        # 5. SHA-256 reflects the deliverable (clean) bytes, not the signed PDF.
        self.db_set(
            "file_integrity_hash",
            hashlib.sha256(clean_bytes).hexdigest(),
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

    def _resolve_render_source(self):
        """Return (source_docx_path, field_mappings) for the render.

        The render source is always the user's uploaded .docx; the linked Word
        Template contributes only its tag mappings (custom_tag -> system_field).
        word_template is mandatory, so mappings are always read from it."""
        template = frappe.get_doc("GMP Word Template", self.word_template)
        mappings = list(template.field_mappings or [])
        file_doc = self._get_file_doc(self.attachment_file)
        return file_doc.get_full_path(), mappings

    def _build_template_context(self, template_for_images=None, field_mappings=None):
        """Context dict consumed by docxtpl. Every editable GMP Document
        field is exposed as a Jinja variable so users can place any
        ``{{ field_name }}`` in the .docx template.

        Signature variables (preparer_signature, reviewer_signature,
        qa_signature) render as empty strings unless ``template_for_images``
        is supplied — in which case they become InlineImage objects sized
        to SIGNATURE_WIDTH_MM.

        ``field_mappings`` (GMP Template Field Mapping rows) add user-defined
        aliases: each row copies the value of its ``system_field`` to a
        ``custom_tag`` key, so a template authored with ``{{ my_title }}`` can
        be fed by the system ``document_name_en`` field. Aliases are additive —
        native ``{{ field_name }}`` tags keep working."""

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
            "document_type": document_type_label(self.document_type),
            "document_type_code": self.document_type or "",
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

        # Apply user-defined aliases last so a custom tag can mirror any
        # resolved system value — text fields, the *_name lookups above, or a
        # *_signature InlineImage (so {{ my_sig }} can map to a signature).
        for row in (field_mappings or []):
            tag = (row.custom_tag or "").strip()
            if tag and row.system_field in context:
                context[tag] = context[row.system_field]

        return context

    # ------------------------------------------------------------------ #
    #  Workflow side effects (driven by native apply_workflow)           #
    # ------------------------------------------------------------------ #

    def _apply_workflow_side_effects(self):
        """Stamp audit fields and shuffle ToDos when workflow_status changes.

        Transitions are performed by Frappe's native Workflow engine (the
        "Actions" menu); this hook supplies the GMP bookkeeping the engine
        does not: who reviewed/approved/requested-revision and when, plus the
        task hand-off between the assigned preparer, reviewer and QA approver.
        Per-actor authorisation is enforced separately by the transition
        `condition` expressions defined in install.py.
        """
        before = self.get_doc_before_save()
        if not before:
            return  # initial insert — no transition to react to

        prev = before.workflow_status
        curr = self.workflow_status
        if prev == curr:
            return

        actor = frappe.session.user

        if curr == WF_UNDER_REVIEW and prev in (WF_DRAFT, WF_REVISION):
            # Preparer submitted for review.
            self.add_comment("Workflow", _("Submitted for review by {0}").format(actor))
            _close_open_todos(self, allocated_to=self.prepared_by)
            _create_todo(self, self.reviewer, _("Review GMP Document {0}").format(self.name))

        elif curr == WF_PENDING_QA:
            # Reviewer approved — forward to QA.
            self.db_set("reviewed_by", actor, update_modified=False)
            self.db_set("reviewed_on", now_datetime(), update_modified=False)
            self.add_comment("Workflow", _("Reviewer approved — forwarded to QA"))
            _close_open_todos(self, allocated_to=self.reviewer)
            _create_todo(self, self.qa_approver, _("QA approval — GMP Document {0}").format(self.name))

        elif curr == WF_APPROVED:
            # QA approved (this save also auto-submits the document).
            self.db_set("approved_by", actor, update_modified=False)
            self.db_set("approved_on", now_datetime(), update_modified=False)
            self.add_comment("Workflow", _("QA approval granted — document submitted"))
            _close_open_todos(self, allocated_to=self.qa_approver)

        elif curr == WF_REVISION and prev == WF_UNDER_REVIEW:
            # Reviewer bounced the document back to the preparer.
            self._stamp_revision_request(actor)
            _close_open_todos(self, allocated_to=self.reviewer)
            _create_todo(self, self.prepared_by, _("Address revision request — {0}").format(self.name))

        elif curr == WF_UNDER_REVIEW and prev == WF_PENDING_QA:
            # QA bounced the document back to the reviewer.
            self._stamp_revision_request(actor)
            _close_open_todos(self, allocated_to=self.qa_approver)
            _create_todo(self, self.reviewer, _("Re-review — QA requested revision on {0}").format(self.name))

    def _stamp_revision_request(self, actor):
        """Record who/when on a revision request. The reason text is captured
        in the writable `last_revision_request` field on the form before the
        actor selects the native Request Revision action."""
        self.db_set("last_revision_by", actor, update_modified=False)
        self.db_set("last_revision_on", now_datetime(), update_modified=False)
        reason = (self.last_revision_request or "").strip()
        self.add_comment(
            "Workflow",
            _("Revision requested by {0}: {1}").format(actor, reason or _("(no reason given)")),
        )

    def _repoint_references_to_self(self):
        """Swing dependents' reference rows from the superseded version to this
        newly-approved one, so cross-references always resolve to the current
        controlled version (Issue #4)."""
        if not self.amended_from:
            return

        rows = frappe.get_all(
            "GMP Document Reference",
            filters={"referenced_document": self.amended_from},
            fields=["name", "parent", "parenttype"],
        )
        repointed_parents = set()
        for row in rows:
            if row.parent == self.name:
                continue  # never repoint this revision's own reference rows
            frappe.db.set_value(
                "GMP Document Reference",
                row.name,
                "referenced_document",
                self.name,
                update_modified=False,
            )
            if row.parenttype == "GMP Document":
                repointed_parents.add(row.parent)

        for parent_name in repointed_parents:
            try:
                frappe.get_doc("GMP Document", parent_name).add_comment(
                    "Info",
                    _("Reference {0} automatically superseded by {1}.").format(
                        self.amended_from, self.name
                    ),
                )
            except Exception:
                frappe.log_error(
                    frappe.get_traceback(),
                    "GMP Document: reference repoint comment failed",
                )

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


def _signature_issue(user_email):
    """Return a short, human-readable reason ``user_email`` has no usable
    signature image, or None if the user has a valid one.

    Mirrors _resolve_signature_path's resolution (Employee.user_id ->
    custom_signature_image -> a PNG/JPG/JPEG File on disk) but returns a message
    instead of logging, so it can drive save-time validation and the client-side
    pre-check. Used by GMPDocument._validate_signatures and check_signature."""
    if not user_email:
        return _("no user is selected")

    employees = frappe.get_all(
        "Employee",
        filters={"user_id": user_email},
        fields=["name", "status", "custom_signature_image"],
        order_by="modified desc",
    )
    if not employees:
        return _("no Employee record is linked to this user")

    def _rank(emp):
        has_sig = 1 if (emp.custom_signature_image or "").strip() else 0
        is_active = 1 if (emp.status or "") == "Active" else 0
        return (has_sig, is_active)

    employees.sort(key=_rank, reverse=True)
    sig_url = (employees[0].custom_signature_image or "").strip()
    if not sig_url:
        return _("the linked Employee has no signature image uploaded")

    file_name = frappe.db.get_value("File", {"file_url": sig_url}, "name")
    if not file_name:
        return _("the signature file record is missing")

    physical_path = frappe.get_doc("File", file_name).get_full_path()
    if not os.path.exists(physical_path):
        return _("the signature image is missing on disk")
    if not physical_path.lower().endswith(ALLOWED_SIGNATURE_EXTENSIONS):
        return _("the signature must be a PNG, JPG or JPEG image")
    return None


def _resolve_signature_path(user_email):
    """Return the on-disk path of a user's signature image, or None.

    Resolution chain:
        User (user_email) -> Employee.user_id -> Employee.custom_signature_image
        -> File.file_url -> File.get_full_path()

    A user can be linked to more than one Employee (rehire, a prior Left/
    Inactive record, or a duplicate). A bare get_value() returns an arbitrary
    row with no ordering guarantee, which is why a signature can appear in one
    render and vanish in the next. We resolve deterministically instead:
    prefer an Employee that actually has a signature, then an Active one, with
    a stable most-recently-modified tiebreak.

    Each failure branch writes to the Error Log so a missing signature on a
    rendered PDF is diagnosable without silent data loss.
    """
    if not user_email:
        return None

    log_title = "GMP Document signature lookup"

    employees = frappe.get_all(
        "Employee",
        filters={"user_id": user_email},
        fields=["name", "status", "custom_signature_image"],
        order_by="modified desc",
    )
    if not employees:
        frappe.log_error(
            title=log_title,
            message=(
                f"No Employee record is linked to user '{user_email}'. "
                f"Set Employee.user_id to that user to enable a signature on rendered PDFs."
            ),
        )
        return None

    # Stable sort keeps the modified-desc order within ties; key ranks an
    # Employee with a signature above one without, and Active above the rest.
    def _rank(emp):
        has_sig = 1 if (emp.custom_signature_image or "").strip() else 0
        is_active = 1 if (emp.status or "") == "Active" else 0
        return (has_sig, is_active)

    employees.sort(key=_rank, reverse=True)
    emp = employees[0]
    employee_name = emp.name
    sig_url = (emp.custom_signature_image or "").strip()
    if not sig_url:
        frappe.log_error(
            title=log_title,
            message=(
                f"User '{user_email}' has {len(employees)} linked Employee record(s) "
                f"but none has a 'custom_signature_image' uploaded."
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

    if not physical_path.lower().endswith(ALLOWED_SIGNATURE_EXTENSIONS):
        frappe.log_error(
            title=log_title,
            message=(
                f"Employee {employee_name} signature must be one of "
                f"{', '.join(ALLOWED_SIGNATURE_EXTENSIONS)} — got '{physical_path}'."
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
    docstatus) is reflected immediately without re-rendering the PDF. The
    base PDF path is resolved dynamically and regenerated on demand if the
    File record or the on-disk file has gone missing (Issue #1)."""
    doc = frappe.get_doc("GMP Document", docname)
    doc.check_permission("read")

    if doc.docstatus != 1:
        frappe.throw(
            _("A controlled PDF is only available after {0} has been QA-approved (submitted).").format(
                docname
            )
        )

    base_pdf_path = _resolve_base_pdf_path(doc)

    watermark_text = _resolve_watermark_text(doc)
    watermarked = _apply_watermark(base_pdf_path, watermark_text)

    safe_label = watermark_text.replace(" ", "_")
    frappe.local.response.filename = f"{docname}-{safe_label}.pdf"
    frappe.local.response.filecontent = watermarked
    frappe.local.response.type = "download"


def _resolve_base_pdf_path(doc):
    """Return the on-disk path of the document's base PDF, regenerating it if
    the File record or the physical file is missing.

    Lookup is by attachment + ``.pdf`` extension (not an exact filename
    match) so a renamed/re-versioned document still resolves. Regeneration
    requires the source ``.docx`` to still be present."""

    def _find_pdf_on_disk():
        for f in frappe.get_all(
            "File",
            filters={
                "attached_to_doctype": doc.doctype,
                "attached_to_name": doc.name,
                "file_name": ["like", "%.pdf"],
            },
            fields=["name"],
        ):
            path = frappe.get_doc("File", f.name).get_full_path()
            if os.path.exists(path):
                return path
        return None

    path = _find_pdf_on_disk()
    if path:
        return path

    # Missing base PDF — regenerate from the controlled .docx attachment.
    # Regeneration mutates the document (db_set of the integrity hash) and
    # rewrites File records, so it must not be reachable by a read-only member
    # downloading a controlled copy. Restrict it to the manager/admin roles.
    if not _is_unrestricted(frappe.session.user):
        frappe.throw(
            _(
                "The controlled PDF for {0} is temporarily unavailable. "
                "Please ask a DMS/QA manager to regenerate it."
            ).format(doc.name)
        )
    if not doc.attachment_file:
        frappe.throw(
            _(
                "Base PDF for {0} is unavailable and cannot be regenerated: "
                "the controlled .docx attachment is missing."
            ).format(doc.name)
        )
    doc._render_and_generate_pdf()

    path = _find_pdf_on_disk()
    if not path:
        frappe.throw(_("Base PDF for {0} could not be generated.").format(doc.name))
    return path


# ---------------------------------------------------------------------- #
#  Workflow                                                              #
# ---------------------------------------------------------------------- #
#
# Transitions are driven entirely by Frappe's native Workflow engine (the
# form "Actions" menu). The state machine, per-actor authorisation
# `condition`s, and roles are declared in install.py:
#
#   Draft / Revision Requested ── Submit for Review ──► Under Review
#   Under Review ── Approve as Reviewer ──► Pending QA Approval
#   Under Review ── Request Revision (Reviewer) ──► Revision Requested
#   Pending QA Approval ── Approve as QA ──► Approved (docstatus=1)
#   Pending QA Approval ── Request Revision (QA) ──► Under Review
#
# Audit stamping and ToDo hand-off run in GMPDocument._apply_workflow_side_effects
# (invoked from on_update / on_submit). There are deliberately no custom
# transition endpoints here — the controller reacts to workflow_status
# changes instead of owning the transitions.


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
#  Permissions: department-scoped read for members, full access for      #
#  module owners                                                         #
# ---------------------------------------------------------------------- #
#
# Visibility model:
#   - DMS Manager / QA Manager / System Manager (+ Administrator): every
#     document, any department, any state.
#   - Everyone else (a plain Employee): read-only access to the *approved,
#     active* controlled copies of the department(s) they belong to, plus any
#     document on which they are personally named (preparer / reviewer / QA
#     approver) so workflow participants are never locked out.
#
# Enforcement is in two cooperating hooks (wired in hooks.py):
#   - get_permission_query_conditions -> list / report / search visibility
#   - has_permission                  -> opening a single document & the
#                                        PDF-download whitelisted methods
# The GMP Document Tree page bypasses both (it queries with frappe.get_all),
# so get_dms_tree_children() applies the same scope explicitly.


def _user_departments(user):
    """Departments the user belongs to, resolved via their Employee record(s)
    (Employee.user_id == user). A user with no Employee link has none.

    Memoised per request (frappe.flags is request-local) because has_permission
    can fire several times per document per request — without this each call
    would issue a fresh Employee query."""
    if not user or user == "Guest":
        return []
    memo = frappe.flags.setdefault("dms_user_departments", {})
    if user not in memo:
        memo[user] = [
            d
            for d in frappe.get_all(
                "Employee", filters={"user_id": user}, pluck="department"
            )
            if d
        ]
    return memo[user]


def _is_unrestricted(user):
    """True for the module-owner / workflow / super-admin roles that see and
    operate on every document regardless of department or creator."""
    return user == "Administrator" or bool(
        UNRESTRICTED_ROLES & set(frappe.get_roles(user))
    )


def _visibility_scope(user=None):
    """Return (allowed_departments, active_only) for the calling user.

    allowed_departments is None for unrestricted users (no department filter);
    otherwise the set of departments they may see. active_only is True for
    restricted users, who only ever see approved, active documents."""
    user = user or frappe.session.user
    if _is_unrestricted(user):
        return None, False
    return set(_user_departments(user)), True


def get_permission_query_conditions(user=None):
    """SQL visibility filter for GMP Document lists, reports and search.

    Empty string = no restriction (unrestricted roles). Restricted users get
    their department's approved/active documents OR any document naming them."""
    user = user or frappe.session.user
    if _is_unrestricted(user):
        return ""

    conditions = []
    depts = _user_departments(user)
    if depts:
        dept_in = ", ".join(frappe.db.escape(d) for d in depts)
        conditions.append(
            "(`tabGMP Document`.department in ({0}) "
            "and `tabGMP Document`.is_active = 1 "
            "and `tabGMP Document`.docstatus = 1)".format(dept_in)
        )

    u = frappe.db.escape(user)
    conditions.append("`tabGMP Document`.prepared_by = {0}".format(u))
    conditions.append("`tabGMP Document`.reviewer = {0}".format(u))
    conditions.append("`tabGMP Document`.qa_approver = {0}".format(u))

    return "(" + " or ".join(conditions) + ")"


def has_permission(doc, ptype="read", user=None):
    """Per-document gate mirroring get_permission_query_conditions().

    Returning False denies; True/None defers to the role permission. Unrestricted
    roles always pass. A plain member may read (and print) an approved, active
    document of their department, or any document naming them; everything else
    is denied for them here (role perms already withhold write/create/etc. — this
    is defence in depth)."""
    if doc is None:
        return None  # doctype-level check; leave to role perms + query conditions

    user = user or frappe.session.user
    if _is_unrestricted(user):
        return True

    read_like = ptype in ("read", "print")
    named = user in (doc.get("prepared_by"), doc.get("reviewer"), doc.get("qa_approver"))
    if named:
        return read_like

    in_scope = (
        cint(doc.get("is_active"))
        and cint(doc.get("docstatus")) == 1
        and doc.get("department") in _user_departments(user)
    )
    return bool(read_like and in_scope)


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

    Honours the same department scope as the list view (see _visibility_scope):
    restricted members only see their department's approved, active documents;
    unrestricted roles see everything submitted."""
    # The endpoint is whitelisted; gate it on the GMP Document read role so a
    # user who happens to be linked to an Employee/department but has no GMP
    # read permission can't enumerate document names/counts via the tree.
    frappe.has_permission("GMP Document", "read", throw=True)

    parent = (parent or "").strip()
    allowed_depts, active_only = _visibility_scope()

    def base_filters(extra=None):
        f = {"docstatus": 1}
        if active_only:
            f["is_active"] = 1
        if extra:
            f.update(extra)
        return f

    def dept_allowed(dept):
        return allowed_depts is None or dept in allowed_depts

    if not parent:
        rows = frappe.get_all(
            "GMP Document",
            filters=base_filters(),
            fields=["department"],
            distinct=True,
        )
        depts = sorted({r.department for r in rows if r.department and dept_allowed(r.department)})
        nodes = []
        for dept in depts:
            cnt = frappe.db.count("GMP Document", filters=base_filters({"department": dept}))
            nodes.append({
                "value": f"Dept::{dept}",
                "title": f"{dept} ({cnt})",
                "expandable": 1,
            })
        return nodes

    if parent.startswith("Dept::"):
        dept = parent[len("Dept::"):]
        if not dept_allowed(dept):
            return []
        rows = frappe.get_all(
            "GMP Document",
            filters=base_filters({"department": dept}),
            fields=["document_type"],
            distinct=True,
        )
        types = sorted({r.document_type for r in rows if r.document_type})
        nodes = []
        for dtype in types:
            cnt = frappe.db.count(
                "GMP Document",
                filters=base_filters({"department": dept, "document_type": dtype}),
            )
            nodes.append({
                "value": f"Type::{dept}::{dtype}",
                "title": f"{document_type_label(dtype)} ({cnt})",
                "expandable": 1,
            })
        return nodes

    if parent.startswith("Type::"):
        rest = parent[len("Type::"):]
        if "::" not in rest:
            return []
        dept, dtype = rest.split("::", 1)
        if not dept_allowed(dept):
            return []

        docs = frappe.get_all(
            "GMP Document",
            filters=base_filters({"department": dept, "document_type": dtype}),
            fields=["name", "version_number", "document_name_en", "is_active"],
        )

        latest = {}
        for d in docs:
            base = d.name.rsplit("-v", 1)[0]
            v = int(d.version_number or 0)
            if base not in latest or v > int(latest[base].version_number or 0):
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

    # The clean Word file is a manager-only control-distribution artifact;
    # department members are limited to the watermarked controlled-copy PDF.
    if not _is_unrestricted(frappe.session.user):
        frappe.throw(
            _("Only DMS/QA managers may download the Word file. Use 'Download PDF (Controlled Copy)' instead."),
            frappe.PermissionError,
        )

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


@frappe.whitelist()
def get_document_reference_tree(docname, depth=3):
    """Return a nested dict representing the reference tree for a GMP Document.

    Each node: {"name": str, "label": str, "reference_type": str, "children": [...]}
    depth limits recursion depth to avoid runaway queries on large graphs.

    Department-scoped: the root must be readable by the caller, and any
    referenced document the caller cannot read (e.g. another department's, for a
    plain member) is omitted from the tree so names/status don't leak across the
    permission boundary.
    """
    # depth arrives from a whitelisted call (string over HTTP). Coerce safely
    # and clamp so a bad or huge value can't crash or drive runaway recursion.
    try:
        depth = int(depth)
    except (TypeError, ValueError):
        depth = 3
    depth = max(0, min(depth, MAX_REFERENCE_TREE_DEPTH))

    if not frappe.db.exists("GMP Document", docname):
        frappe.throw(_("GMP Document {0} not found.").format(docname), frappe.DoesNotExistError)

    root = frappe.get_doc("GMP Document", docname)
    if not frappe.has_permission("GMP Document", "read", doc=root):
        frappe.throw(
            _("You do not have permission to read {0}.").format(docname),
            frappe.PermissionError,
        )

    def _label(doc):
        label = doc.name
        if doc.document_name_en:
            label += f" — {doc.document_name_en}"
        return label

    def _build(doc, current_depth, visited):
        node = {
            "name": doc.name,
            "label": _label(doc),
            "reference_type": "",
            "children": [],
        }
        if current_depth <= 0 or doc.name in visited:
            return node
        visited = visited | {doc.name}
        # doc.references is the already-loaded child table, so no extra query.
        for r in (doc.references or []):
            target = r.referenced_document
            # Skip dangling references (target deleted) so a missing document
            # degrades gracefully instead of raising DoesNotExistError, and skip
            # any the caller cannot read so the tree never discloses documents
            # outside their permission scope (e.g. another department's). The
            # exists check guards against loading a non-existent doc; the target
            # is then loaded once and reused for both the permission check and
            # the recursion, so there is no redundant document load.
            if not target or not frappe.db.exists("GMP Document", target):
                continue
            child_doc = frappe.get_doc("GMP Document", target)
            if not frappe.has_permission("GMP Document", "read", doc=child_doc):
                continue
            child = _build(child_doc, current_depth - 1, visited)
            child["reference_type"] = r.reference_type or ""
            node["children"].append(child)
        return node

    return _build(root, depth, set())


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
