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
ALLOWED_EXTENSIONS = (".docx", ".xlsx", ".vsdx")
SIGNATURE_WIDTH_MM = 40  # rendered signature width in PDF

# Visio cannot embed the QA stamp in-package reliably (libvisio does not
# rasterise ForeignData bitmaps on PDF export), so the stamp is overlaid on the
# converted PDF at these coordinates. Points, bottom-left origin, Letter page
# (612x792). Tune on the server once the real template layout is fixed.
VSDX_STAMP_PLACEMENT = {"page": 0, "x": 430, "y": 70, "width": 130}
# python-docx/InlineImage embeds these raster formats reliably. PNG is
# preferred (transparency); JPG/JPEG accepted because HR uploads vary.
ALLOWED_SIGNATURE_EXTENSIONS = (".png", ".jpg", ".jpeg")

# QA status stamps baked into the signed base PDF at the {{ qa_stamp }}
# placeholder (same InlineImage mechanism as signatures). The approved stamp is
# embedded when the document is active & QA-approved; the rejected stamp when it
# becomes obsolete (cancelled or auto-expired). Assets ship inside the app
# package so they resolve on any host.
STAMP_WIDTH_MM = 45  # rendered QA-stamp width in PDF
STAMP_APPROVED = "qaapproved.png"
STAMP_REJECTED = "qarejected.png"


def _stamp_asset_path(filename):
    """Absolute path to a packaged QA stamp image (dms/stamps/<filename>)."""
    return frappe.get_app_path("dms", "stamps", filename)


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
    ("qa_stamp", "QA Status Stamp (image)"),
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
WF_PENDING_SUPERVISOR = "Pending Supervisor Approval"
WF_UNDER_REVIEW = "Under Review"
WF_PENDING_QA_SUPERVISOR = "Pending QA Supervisor"
WF_QA_IN_PROGRESS = "QA Review In Progress"
WF_PENDING_MANAGER = "Pending Manager Approval"
WF_PENDING_REGULATORY = "Pending Regulatory Validation"
WF_PENDING_FINAL_QA = "Pending Final QA Approval"
WF_APPROVED = "Approved"
WF_REVISION = "Revision Requested"
WF_REVISION_CANCELLED = "Revision Cancelled"
WF_OBSOLETE = "Obsolete"

WF_ALL = (
    WF_DRAFT,
    WF_PENDING_SUPERVISOR,
    WF_UNDER_REVIEW,
    WF_PENDING_QA_SUPERVISOR,
    WF_QA_IN_PROGRESS,
    WF_PENDING_MANAGER,
    WF_PENDING_REGULATORY,
    WF_PENDING_FINAL_QA,
    WF_APPROVED,
    WF_REVISION,
    WF_REVISION_CANCELLED,
    WF_OBSOLETE,
)

# Sequential QA-queue row statuses (GMP QA Review child table).
QA_QUEUED = "Queued"
QA_AWAITING = "Awaiting Review"
QA_APPROVED = "Approved"
QA_RETURNED = "Return Requested"
QA_SKIPPED = "Skipped"
QA_SUPERSEDED = "Superseded"
QA_OPEN_STATUSES = (QA_QUEUED, QA_AWAITING)

# Reverse-path edges: (from_state, to_state) → (actor field whose ToDo closes,
# actor field who receives the task, untranslated ToDo message with {0} = doc
# name). Every return stamps last_revision_by/on + the reason comment via
# _stamp_revision_request, so the whole reverse path is logged and visible.
_RETURN_EDGES = {
    (WF_PENDING_SUPERVISOR, WF_REVISION):          ("supervisor",         "prepared_by",        "Address revision request — {0}"),
    (WF_UNDER_REVIEW, WF_PENDING_SUPERVISOR):      ("reviewer",           "supervisor",         "Re-approve — reviewer returned {0}"),
    (WF_UNDER_REVIEW, WF_REVISION):                ("reviewer",           "prepared_by",        "Address revision request — {0}"),
    (WF_PENDING_QA_SUPERVISOR, WF_UNDER_REVIEW):   ("qa_supervisor",      "reviewer",           "Re-review — QA Supervisor returned {0}"),
    (WF_PENDING_QA_SUPERVISOR, WF_REVISION):       ("qa_supervisor",      "prepared_by",        "Address revision request — {0}"),
    (WF_PENDING_MANAGER, WF_PENDING_QA_SUPERVISOR): ("reviewer",          "qa_supervisor",      "Re-assess — Manager returned {0} to QA"),
    (WF_PENDING_REGULATORY, WF_PENDING_MANAGER):   ("regulatory_manager", "reviewer",           "Re-approve — Regulatory returned {0}"),
    (WF_PENDING_FINAL_QA, WF_PENDING_REGULATORY):  ("qa_approver",        "regulatory_manager", "Re-validate — Final QA returned {0}"),
}


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

        # Strict ID structure: [Dept Abbr]-[Form Type]-[Number(4)]-[Version]
        #   e.g. PR-FRM-0001-1
        # No prefixes, suffixes, spaces or extra characters — exactly four
        # dash-separated segments. The trailing version segment reflects the
        # document's POSITION in its revision chain (first issue == 1), NOT the
        # version_number field — which is human revision content and may be
        # 0-based. Deriving it from the chain keeps names collision-free and
        # always starting at 1 regardless of how version_number is numbered.
        #
        # Both successor mechanisms share the same chain: `revision_of` (the
        # non-destructive revise flow) and `amended_from` (legacy cancel+amend).
        # The next segment is MAX(existing segments in the chain) + 1 — not
        # predecessor's segment + 1 — because a cancelled revision is retained
        # in the database and keeps its name, leaving an occupied segment that
        # a naive +1 would collide with.
        predecessor_name = self.revision_of or self.amended_from
        if predecessor_name:
            self.name = self._next_chain_name(predecessor_name)
            return

        # document_type links to GMP Document Type, whose record name *is* the
        # short form-type code (e.g. "FRM"), so it is already name-safe.
        type_code = self.document_type
        prefix = f"{dept_abbr}-{type_code}-"
        existing = frappe.get_all(
            "GMP Document",
            filters=[["name", "like", f"{prefix}%"]],
            pluck="name",
        )

        # The document number is the segment immediately after the prefix.
        # Amended versions share their predecessor's number (only the version
        # segment differs), so they never inflate the counter.
        max_increment = 0
        for existing_name in existing:
            try:
                rest = existing_name[len(prefix):]
                inc = int(rest.split("-")[0])
                if inc > max_increment:
                    max_increment = inc
            except (ValueError, IndexError):
                continue

        next_inc = str(max_increment + 1).zfill(4)
        # A brand-new document is the first version of its chain: segment 1.
        self.name = f"{dept_abbr}-{type_code}-{next_inc}-1"

    @staticmethod
    def _next_chain_name(predecessor_name):
        """Next collision-free name in a revision chain.

        Chain members share the logical base ``[dept]-[type]-[number]`` and
        differ only in the trailing integer segment. Scans every existing
        member (including retained cancelled revisions) and returns
        ``base-(max_segment + 1)``."""
        base_name, _sep, prev_seg = predecessor_name.rpartition("-")
        try:
            max_seg = int(prev_seg)
        except ValueError:
            # Malformed predecessor name: no chain to scan — degrade to
            # suffixing it, which at least stays unique per rpartition base.
            base_name = predecessor_name
            max_seg = 0

        for existing_name in frappe.get_all(
            "GMP Document",
            filters=[["name", "like", f"{base_name}-%"]],
            pluck="name",
        ):
            rest = existing_name[len(base_name) + 1:]
            try:
                seg = int(rest)
            except ValueError:
                continue  # different chain sharing the prefix (never 4-segment)
            if seg > max_seg:
                max_seg = seg

        return f"{base_name}-{max_seg + 1}"

    def before_insert(self):
        if not self.prepared_by:
            self.prepared_by = frappe.session.user
        if not self.workflow_status:
            self.workflow_status = WF_DRAFT

        predecessor_name = self.revision_of or self.amended_from
        if not predecessor_name:
            # version_number is the human *revision* number rendered into the
            # document body — it is content, not identity. The name's trailing
            # segment (see autoname) is what always starts at 1; version_number
            # merely defaults to 1 for a brand-new document, while a 0-based
            # revision scheme may set it explicitly (e.g. first issue == rev 0).
            if self.version_number is None:
                self.version_number = 1
            return

        if self.revision_of and self.amended_from:
            frappe.throw(
                _("A GMP Document cannot be both a revision (revision_of) and an amendment (amended_from) at once.")
            )

        self._guard_successor_creation(predecessor_name)

        predecessor = frappe.db.get_value(
            "GMP Document",
            predecessor_name,
            ["version_number"],
            as_dict=True,
        ) or frappe._dict()
        # Successors increment from the predecessor; since the chain starts at 1,
        # the first revision is version 2, the next 3, and so on. For the
        # non-destructive revise flow this is the *candidate* number only: the
        # predecessor keeps its version_number and stays the effective version
        # until this draft is QA-approved, so the official version increases
        # only at approval — and not at all if the revision is cancelled.
        self.version_number = (cint(predecessor.version_number) or 0) + 1
        # Change control: a revised document must re-acquire its own controlled
        # file, so clear an attachment that was carried over from the
        # predecessor by the amend/copy — but never one the user uploaded for
        # this revision. The distinction is made by File *ownership*, not URL
        # string: Frappe deduplicates uploads by content hash, so a freshly
        # uploaded .docx can be handed the predecessor's file_url while being a
        # distinct File row (still unattached at before_insert). A plain
        # url/exists check wrongly treated that as inherited and nulled it, so
        # the mandatory check then failed with "Value missing for Attachment
        # (.docx)".
        #
        # A genuine re-upload always creates its OWN File row (unattached here,
        # or attached to this new doc), whereas amend/copy creates none — the
        # only File(s) carrying the url stay attached to the predecessor. So the
        # attachment is inherited iff a File with this url exists AND every File
        # with it is attached to the predecessor. The predecessor's own file is
        # therefore never reused, let alone replaced, by a revision.
        inherited_attachment = False
        if self.attachment_file:
            files = frappe.get_all(
                "File",
                filters={"file_url": self.attachment_file},
                fields=["attached_to_doctype", "attached_to_name"],
            )
            inherited_attachment = bool(files) and all(
                f.attached_to_doctype == self.doctype
                and f.attached_to_name == predecessor_name
                for f in files
            )
        if not self.attachment_file or inherited_attachment:
            self.attachment_file = None
            self.file_integrity_hash = None
        self.effective_date = None
        self.set_effective_date = 0  # each revision decides its own scheduling
        self.expiry_date = None
        self.next_revision_date = None
        # The successor draft starts a fresh review cycle. is_active=1 marks it
        # as the live candidate of its chain (for an amend, the predecessor was
        # cancelled, so re-assert active rather than inherit 0); the *effective*
        # version is still whichever chain member has docstatus == 1.
        self.is_active = 1
        self.workflow_status = WF_DRAFT
        # Routing actors are re-resolved when this successor is submitted for
        # approval — the org chart or DMS Settings may have changed since the
        # predecessor's cycle — and every stage stamp starts blank.
        self.supervisor = None
        self.reviewer = None
        self.qa_supervisor = None
        self.regulatory_manager = None
        self.qa_approver = None
        self.qa_reviews = []
        self.qa_review_complete = 0
        self.supervisor_approved_by = None
        self.supervisor_approved_on = None
        self.reviewed_by = None
        self.reviewed_on = None
        self.manager_approved_by = None
        self.manager_approved_on = None
        self.regulatory_validated_by = None
        self.regulatory_validated_on = None
        self.approved_by = None
        self.approved_on = None
        self.last_revision_request = None
        self.last_revision_by = None
        self.last_revision_on = None

    def _guard_successor_creation(self, predecessor_name):
        """Integrity guards for creating a successor (revision or amendment).

        - The revise flow requires an Approved, active, submitted predecessor —
          the record that stays effective while the revision is drafted.
        - A chain may have at most ONE open successor at a time; retained
          cancelled revisions don't count.
        - A predecessor that was already superseded by an approved successor
          can never grow a second, parallel successor."""
        if self.revision_of:
            pred = frappe.db.get_value(
                "GMP Document",
                self.revision_of,
                ["docstatus", "workflow_status", "is_active"],
                as_dict=True,
            )
            if not pred:
                frappe.throw(_("Document {0} does not exist.").format(self.revision_of))
            if pred.docstatus != 1 or pred.workflow_status != WF_APPROVED or not cint(pred.is_active):
                frappe.throw(
                    _(
                        "Only the current effective version can be revised: {0} must be "
                        "Approved and active (it is {1})."
                    ).format(self.revision_of, _(pred.workflow_status or "Draft"))
                )

        # Superseded already? (An approved successor exists.)
        for link_field in ("revision_of", "amended_from"):
            approved_successor = frappe.db.get_value(
                "GMP Document", {link_field: predecessor_name, "docstatus": 1}, "name"
            )
            if approved_successor:
                frappe.throw(
                    _(
                        "{0} has already been superseded by {1}. Revise the current "
                        "effective version instead."
                    ).format(predecessor_name, approved_successor)
                )

        # One open successor at a time. Cancelled revisions are retained
        # records (workflow_status == Revision Cancelled) and don't block.
        for link_field in ("revision_of", "amended_from"):
            open_successor = frappe.db.get_value(
                "GMP Document",
                {
                    link_field: predecessor_name,
                    "docstatus": 0,
                    "workflow_status": ["!=", WF_REVISION_CANCELLED],
                    "name": ["!=", self.name or ""],
                },
                "name",
            )
            if open_successor:
                frappe.throw(
                    _(
                        "A revision of {0} is already in progress: {1}. Complete or "
                        "cancel it before starting another."
                    ).format(predecessor_name, frappe.bold(open_successor))
                )

    def validate(self):
        # Cancelling a revision must succeed from ANY pre-approval state —
        # including a bare draft that has no attachment yet (create_revision
        # deliberately strips the predecessor's file). The workflow save into
        # the terminal state must not trip the mandatory attachment check.
        if self.docstatus == 0 and self.workflow_status == WF_REVISION_CANCELLED and self.revision_of:
            self.flags.ignore_mandatory = True

        if (self.amended_from or self.revision_of) and not (
            self.reason_for_change and self.reason_for_change.strip()
        ):
            frappe.throw(_("Reason for Change is mandatory when revising a GMP Document."))
        self._enforce_effective_date_policy()
        self._check_circular_references()
        self._resolve_workflow_actors_on_submit_for_approval()
        self._resolve_stage_actor_if_missing()
        self._validate_signatures()

    def _resolve_stage_actor_if_missing(self):
        """Safety net for documents that entered the pipeline before v1.3 (or
        whose settings changed mid-flight): when a transition lands on a stage
        whose settings-sourced actor was never resolved, resolve it now rather
        than stranding the document in a state nobody is allowed to act on."""
        before = self.get_doc_before_save()
        if not before or before.workflow_status == self.workflow_status:
            return
        needed = {
            WF_PENDING_QA_SUPERVISOR: ("qa_supervisor", _("QA Supervisor")),
            WF_PENDING_REGULATORY: ("regulatory_manager", _("Regulatory Manager")),
            WF_PENDING_FINAL_QA: ("qa_approver", _("QA Approver")),
        }.get(self.workflow_status)
        if not needed or self.get(needed[0]):
            return

        from dms.dms.doctype.dms_settings.dms_settings import resolve_department_actors

        fieldname, label = needed
        actor = resolve_department_actors(self.department).get(fieldname)
        if not actor:
            frappe.throw(
                _("No {0} is configured for department {1}. Set it in DMS Settings.").format(
                    label, frappe.bold(self.department or "?")
                ),
                title=_("Routing Failed"),
            )
        self.set(fieldname, actor)

    def _resolve_workflow_actors_on_submit_for_approval(self):
        """Dynamic routing: when the preparer submits the draft for approval
        (Draft/Revision Requested → Pending Supervisor Approval), resolve and
        freeze every workflow actor onto the document:

          supervisor  — the preparer's direct supervisor (Employee.reports_to)
          reviewer    — the supervisor's own manager; also acts later as the
                        Manager in the post-QA approval step
          qa_supervisor / regulatory_manager / qa_approver — from DMS Settings
                        (per-department override, else global default)

        Resolution runs inside validate() so the actors land in the same save
        that performs the transition, and a failure aborts it with a precise
        message. Re-submitting after a revision re-resolves, picking up org
        chart or settings changes."""
        before = self.get_doc_before_save()
        entering = (
            self.workflow_status == WF_PENDING_SUPERVISOR
            and before is not None
            and before.workflow_status in (WF_DRAFT, WF_REVISION)
        )
        if not entering:
            return

        def _employee_of(user, label):
            emp = frappe.db.get_value(
                "Employee",
                {"user_id": user, "status": "Active"},
                ["name", "employee_name", "reports_to"],
                as_dict=True,
            )
            if not emp:
                frappe.throw(
                    _("{0} {1} has no active Employee record, so the approval chain cannot be resolved.").format(
                        label, frappe.bold(user)
                    ),
                    title=_("Routing Failed"),
                )
            return emp

        def _user_of_employee(employee_id, label):
            user_id = frappe.db.get_value("Employee", employee_id, "user_id")
            if not user_id:
                frappe.throw(
                    _("Employee {0} ({1}) is not linked to a User, so they cannot act in the workflow.").format(
                        frappe.bold(employee_id), label
                    ),
                    title=_("Routing Failed"),
                )
            return user_id

        preparer_emp = _employee_of(self.prepared_by, _("Preparer"))
        if not preparer_emp.reports_to:
            frappe.throw(
                _("Employee {0} has no supervisor (Reports To is empty). Set it on the Employee record.").format(
                    frappe.bold(preparer_emp.employee_name or preparer_emp.name)
                ),
                title=_("Routing Failed"),
            )
        self.supervisor = _user_of_employee(preparer_emp.reports_to, _("Supervisor"))

        supervisor_reports_to = frappe.db.get_value("Employee", preparer_emp.reports_to, "reports_to")
        if not supervisor_reports_to:
            frappe.throw(
                _("Supervisor {0} has no manager (Reports To is empty on their Employee record), so no Reviewer can be resolved.").format(
                    frappe.bold(self.supervisor)
                ),
                title=_("Routing Failed"),
            )
        self.reviewer = _user_of_employee(supervisor_reports_to, _("Reviewer / Manager"))

        from dms.dms.doctype.dms_settings.dms_settings import resolve_department_actors

        actors = resolve_department_actors(self.department)
        for fieldname, label in (
            ("qa_supervisor", _("QA Supervisor")),
            ("regulatory_manager", _("Regulatory Manager")),
            ("qa_approver", _("QA Approver")),
        ):
            if not actors.get(fieldname):
                frappe.throw(
                    _("No {0} is configured for department {1}. Set it in DMS Settings.").format(
                        label, frappe.bold(self.department or "?")
                    ),
                    title=_("Routing Failed"),
                )
            self.set(fieldname, actors[fieldname])

    def _enforce_effective_date_policy(self):
        """ERPNext posting-date semantics for ``effective_date``.

        Mirrors the `set_posting_time` pattern: with the 'Edit Effective Date'
        checkbox OFF the field is system-controlled — any manual value on a
        draft is silently normalized away (exactly like ERPNext overwriting
        posting_date when set_posting_time is unchecked) and the system stamps
        the QA-approval date at submit. With the checkbox ON, a QA/DMS manager
        may schedule a future date or backdate; a future date keeps the
        document pending (not effective) until the daily sweep activates it."""
        if self.docstatus != 0:
            return  # post-submit edits are governed by Frappe's submitted-doc rules

        if not cint(self.set_effective_date):
            previous = self.get_doc_before_save()
            system_value = previous.effective_date if previous else None
            if cstr(self.effective_date) != cstr(system_value):
                self.effective_date = system_value
                self._calculate_lifecycle_dates()
            return

        allowed = {"QA Manager", "DMS Manager", "System Manager"}
        if frappe.session.user != "Administrator" and not (allowed & set(frappe.get_roles())):
            frappe.throw(
                _("Only a QA Manager or DMS Manager may edit the Effective Date."),
                frappe.PermissionError,
            )

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

        # Manual effective-date mode must resolve to an actual date by
        # approval time: either the scheduler date was entered, or the box is
        # unticked and the system stamps today in on_submit.
        if cint(self.set_effective_date) and not self.effective_date:
            frappe.throw(
                _(
                    "'Edit Effective Date' is enabled but no Effective Date is set. "
                    "Enter the date or untick the option to let the system stamp "
                    "the approval date."
                )
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
            frappe.throw(_("A .docx, .xlsx, or .vsdx attachment is required before submitting."))

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

        # A future-dated document is QA-approved but PENDING: it must not
        # become the effective version — nor displace its predecessor — until
        # the effective date arrives (activate_effective_documents sweep).
        # The render above ran while is_active was still 1, so the base PDF
        # carries the approved stamp, as it should for an approved document.
        if _is_pending_effective_date(self.effective_date):
            self.db_set("is_active", 0, update_modified=False)
            self.add_comment(
                "Info",
                _(
                    "QA-approved with a future Effective Date ({0}); the document "
                    "becomes the effective version on that date."
                ).format(self.effective_date),
            )
            return

        # Effective immediately: only after the render has succeeded (a throw
        # above aborts the submit and leaves the predecessor untouched) does
        # the predecessor stop being the effective version.
        self._obsolete_superseded_version()
        # Now that this revision is officially approved, swing any dependent
        # references off the superseded version and onto this one.
        self._repoint_references_to_self()

    def _obsolete_superseded_version(self):
        """Retire the document this approved revision supersedes.

        The non-destructive revise flow (revision_of) keeps the predecessor
        Approved and effective throughout drafting and review; the moment the
        revision is QA-approved, the predecessor transitions to Obsolete —
        automatically, not via cancel. It keeps docstatus 1 (same pattern as
        the expiry sweep): the record stays a submitted, immutable audit
        artifact, and crucially Frappe never offers 'Amend' on it, which a
        cancel would — inviting a parallel chain."""
        if not self.revision_of:
            return

        pred = frappe.get_doc("GMP Document", self.revision_of)
        if pred.docstatus != 1 or not cint(pred.is_active):
            return  # already retired (e.g. expired or manually cancelled meanwhile)

        pred.db_set("is_active", 0, update_modified=False)
        pred.db_set("workflow_status", WF_OBSOLETE, update_modified=False)
        pred.add_comment(
            "Info",
            _("Superseded by approved revision {0} — status set to Obsolete.").format(self.name),
        )
        self.add_comment(
            "Info",
            _("Approval of this revision obsoleted predecessor {0}.").format(pred.name),
        )

        # Re-stamp the predecessor's persisted base PDF with the rejected/
        # obsolete stamp (is_active is now 0, so _resolve_stamp_path() selects
        # it). Guarded: approval of the new revision must complete even if
        # LibreOffice is unavailable — downloads are protected regardless,
        # because the dynamic watermark already resolves to OBSOLETE.
        if pred.attachment_file:
            try:
                pred._render_and_generate_pdf()
            except Exception:
                frappe.log_error(
                    frappe.get_traceback(),
                    "GMP Document: obsolete re-stamp of superseded version failed",
                )

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

        # Re-stamp the persisted base PDF with the QA-rejected stamp. is_active is
        # now 0, so _resolve_stamp_path() selects the rejected asset (the stamp
        # itself is embedded only in the PDF pass, never the clean .docx). The
        # re-render also refreshes the deliverable .docx to reflect the now-
        # obsolete status text, updating file_integrity_hash accordingly.
        # Guarded + logged rather than throwing: a cancellation must complete
        # even if LibreOffice is unavailable — the stamp can be regenerated
        # later, but the document must not stay stuck.
        if self.attachment_file:
            try:
                self._render_and_generate_pdf()
            except Exception:
                frappe.log_error(
                    frappe.get_traceback(),
                    "GMP Document: QA-rejected re-stamp on cancel failed",
                )

    def on_trash(self, *args, **kwargs):
        # 21 CFR Part 11 retention: a cancelled revision is a controlled audit
        # record — it documents that a change was attempted and abandoned — and
        # must stay in the database with its Revision Cancelled status.
        if self.workflow_status == WF_REVISION_CANCELLED:
            frappe.throw(
                _(
                    "Cancelled revisions are retained for audit traceability and "
                    "cannot be deleted."
                )
            )
        super().on_trash(*args, **kwargs)

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
            frappe.throw(_("Only .docx, .xlsx, or .vsdx files are allowed for GMP Documents."))

        controlled_url = f"/private/files/{self._source_filename()}"
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

        # Promote the upload to this document's OWN pristine template. We copy the
        # bytes into a per-document physical file and own its File record rather
        # than renaming the uploaded file in place, because Frappe deduplicates
        # uploads by content hash: identical or derived uploads (e.g. each
        # version started from the same base file) are pointed at a single shared
        # physical file, and renaming / overwriting it then bleeds one document's
        # content into another document's render. Owning an independent file
        # decouples this document from that sharing. This file is IMMUTABLE — the
        # render writes its clean deliverable to a separate file so the pristine
        # tagged template always remains available as the render source.
        controlled = self._own_private_file(content, self._source_filename())
        self.attachment_file = controlled.file_url
        self.file_integrity_hash = _compute_sha256(controlled.get_full_path())

        self._purge_superseded_attachments(
            keep=controlled.name, previous_url=previous_url, also_remove=[src.name]
        )
        frappe.clear_document_cache(self.doctype, self.name)

    def _controlled_ext(self):
        """Extension of this document's source template, derived from the current
        attachment. One of ALLOWED_EXTENSIONS; defaults to .docx. Stable across
        saves because attachment_file stays pointed at the immutable pristine
        template (which carries the same extension)."""
        src = (self.attachment_file or "").lower()
        for ext in ALLOWED_EXTENSIONS:
            if src.endswith(ext):
                return ext
        return ".docx"

    def _source_filename(self):
        """Filename of the immutable pristine template (the render source). This
        is what ``attachment_file`` points to; it is never overwritten by a
        render, so re-renders (cancel, expiry, regenerate) stay idempotent."""
        return f"{self.name}{self._controlled_ext()}"

    def _deliverable_filename(self):
        """Filename of the CLEAN rendered deliverable (tags populated, no images)
        stored as a separate file so the pristine source survives. Downloaded by
        managers via download_word_document; regenerated on every render."""
        return f"{self.name}-controlled{self._controlled_ext()}"

    def _own_private_file(self, content, filename):
        """Persist ``content`` to /private/files/<filename> in a physical file
        owned solely by this document, with a File record whose ``content_hash``
        is kept in sync. Returns the File doc.

        Deliberately bypasses Frappe's content-hash deduplication (it writes the
        file directly and inserts/updates the File row without going through
        File.save): dedup can otherwise point several documents at one physical
        file which — combined with an in-place overwrite that left
        File.content_hash stale — caused one document's PDF to contain another
        document's content."""
        fname = filename
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
        upload, the previous controlled file, and any other source-format file
        (.docx/.xlsx/.vsdx) attached to this document — keeping only the File we
        just promoted. The generated base PDF is deliberately excluded (only
        ALLOWED_EXTENSIONS match), so a re-upload never deletes the signed PDF.
        Frappe guards the physical delete when another File row references the
        same path, so a shared (deduplicated) upload never removes another
        document's bytes."""
        stale = set(also_remove or [])
        if previous_url:
            stale.update(frappe.get_all("File", filters={"file_url": previous_url}, pluck="name"))
        attached = frappe.get_all(
            "File",
            filters={"attached_to_doctype": self.doctype, "attached_to_name": self.name},
            fields=["name", "file_name"],
        )
        stale.update(
            row.name
            for row in attached
            if (row.file_name or "").lower().endswith(ALLOWED_EXTENSIONS)
        )
        stale.discard(keep)
        for name in stale:
            # force bypasses link checks; ignore_permissions because this runs
            # inside the controlled save, not as the end user.
            frappe.delete_doc("File", name, force=True, ignore_permissions=True)

    def _render_and_generate_pdf(self):
        """Render the controlled source file and the signed base PDF from the
        uploaded template, dispatching by file format.

        Every format produces the same two artefacts:

        1. A CLEAN deliverable in the native format (.docx/.xlsx/.vsdx) with all
           text tags populated but NO images embedded — what managers download.
        2. A signed base PDF that additionally carries the signature images and
           the QA status stamp.

        The render source is the user's uploaded attachment; the linked Word
        Template supplies only the custom-tag -> system-field mappings applied
        during the render.
        """
        source_path, field_mappings = self._resolve_render_source()

        ext = os.path.splitext(source_path)[1].lower()
        if ext not in ALLOWED_EXTENSIONS:
            frappe.throw(_("Render source must be a .docx, .xlsx, or .vsdx file."))

        soffice = self._soffice_binary()

        with tempfile.TemporaryDirectory() as tmpdir:
            if ext == ".docx":
                clean_bytes, pdf_bytes = self._render_docx(source_path, field_mappings, soffice, tmpdir)
            elif ext == ".xlsx":
                clean_bytes, pdf_bytes = self._render_xlsx(source_path, field_mappings, soffice, tmpdir)
            else:  # .vsdx
                clean_bytes, pdf_bytes = self._render_vsdx(source_path, field_mappings, soffice, tmpdir)

        self._persist_render_artifacts(clean_bytes, pdf_bytes)

    def _persist_render_artifacts(self, clean_bytes, pdf_bytes):
        """Common tail for every format: store the clean deliverable as its OWN
        file (leaving the pristine source template untouched) and replace the
        signed base PDF.

        Critically, this does NOT overwrite ``attachment_file`` / the source
        template — that immutability is what lets cancel, expiry and download-time
        regeneration re-render from a still-tagged template. The clean deliverable
        lives at ``_deliverable_filename()`` and is served by download_word_document."""
        self._own_private_file(clean_bytes, self._deliverable_filename())

        # SHA-256 reflects the deliverable (clean) bytes, not the signed PDF.
        self.db_set(
            "file_integrity_hash",
            hashlib.sha256(clean_bytes).hexdigest(),
            update_modified=False,
        )

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

    def _soffice_binary(self):
        soffice = shutil.which("soffice") or shutil.which("libreoffice")
        if not soffice:
            frappe.throw(_("LibreOffice (soffice) is not installed on the server. Cannot generate PDF."))
        return soffice

    def _convert_to_pdf(self, soffice, src_path, tmpdir):
        """Convert ``src_path`` to PDF via LibreOffice into ``tmpdir`` and return
        the PDF bytes. Shared by every format's render pipeline.

        Each run gets a private ``UserInstallation`` profile: concurrent soffice
        processes sharing the default profile exit 0 without producing output."""
        profile_dir = os.path.join(tmpdir, ".lo-profile")
        os.makedirs(profile_dir, exist_ok=True)
        try:
            proc = subprocess.run(
                [
                    soffice,
                    "--headless",
                    f"-env:UserInstallation=file://{profile_dir}",
                    "--convert-to",
                    "pdf",
                    "--outdir",
                    tmpdir,
                    src_path,
                ],
                capture_output=True,
                timeout=180,
                check=True,
            )
        except subprocess.TimeoutExpired:
            frappe.throw(_("PDF conversion timed out."))
        except subprocess.CalledProcessError as exc:
            frappe.log_error(
                title="GMP Document: PDF conversion failed",
                message=(exc.stderr or b"").decode("utf-8", errors="ignore"),
            )
            frappe.throw(_("PDF conversion failed. Check Error Log for details."))

        stem = os.path.splitext(os.path.basename(src_path))[0]
        generated_pdf = os.path.join(tmpdir, f"{stem}.pdf")
        if not os.path.exists(generated_pdf):
            # soffice also exits 0 when it has no import/export filter for the
            # source format (e.g. libreoffice-calc/-draw not installed), so
            # capture its output — the real reason is only ever printed there.
            frappe.log_error(
                title="GMP Document: PDF conversion produced no output",
                message="\n".join(
                    part
                    for part in (
                        f"binary: {soffice}",
                        f"source: {os.path.basename(src_path)}",
                        (proc.stdout or b"").decode("utf-8", errors="ignore"),
                        (proc.stderr or b"").decode("utf-8", errors="ignore"),
                    )
                    if part
                ),
            )
            frappe.throw(_("PDF was not generated by LibreOffice. Check Error Log for details."))
        with open(generated_pdf, "rb") as fh:
            return fh.read()

    def _resolve_render_images(self, field_mappings=None):
        """Map of ``{tag: absolute_png_path}`` for the image placeholders, from
        current state — the QA status stamp plus the three signatures. Mirrors
        the actor-then-assigned signature resolution used by the .docx image
        pass. User-defined aliases (custom_tag -> an image system_field) are
        propagated so an aliased image tag resolves in xlsx/vsdx too."""
        images = {}
        stamp = self._resolve_stamp_path()
        if stamp:
            images["qa_stamp"] = stamp
        for tag, actor, assigned in (
            ("preparer_signature", self.prepared_by, self.prepared_by),
            ("reviewer_signature", self.reviewed_by, self.reviewer),
            ("qa_signature", self.approved_by, self.qa_approver),
        ):
            path = _resolve_signature_path(actor) or _resolve_signature_path(assigned)
            if path:
                images[tag] = path
        for row in (field_mappings or []):
            alias = (row.custom_tag or "").strip()
            if alias and row.system_field in images:
                images[alias] = images[row.system_field]
        return images

    def _render_docx(self, source_path, field_mappings, soffice, tmpdir):
        """DOCX: two docxtpl passes (clean deliverable + with-images source)."""
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

        pdf_bytes = self._convert_to_pdf(soffice, sig_path, tmpdir)
        with open(clean_path, "rb") as fh:
            return fh.read(), pdf_bytes

    def _render_xlsx(self, source_path, field_mappings, soffice, tmpdir):
        """XLSX: openpyxl renders text tags per cell; images are anchored as real
        pictures in the PDF-source pass. Calc rasterises anchored images on PDF
        export, so the stamp/signatures need no post-overlay."""
        from .format_renderers import render_xlsx

        text_context = self._build_template_context(field_mappings=field_mappings)
        clean_path = os.path.join(tmpdir, f"{self.name}.xlsx")
        img_path = os.path.join(tmpdir, f"{self.name}-with-images.xlsx")
        try:
            render_xlsx(source_path, clean_path, text_context, images={})
            render_xlsx(
                source_path, img_path, text_context,
                images=self._resolve_render_images(field_mappings),
            )
        except Exception:
            frappe.log_error(frappe.get_traceback(), "GMP Document: Excel render failed")
            frappe.throw(_("Failed to render Excel template. Check Error Log for details."))

        pdf_bytes = self._convert_to_pdf(soffice, img_path, tmpdir)
        with open(clean_path, "rb") as fh:
            return fh.read(), pdf_bytes

    def _render_vsdx(self, source_path, field_mappings, soffice, tmpdir):
        """VSDX: render text tags in the page XML (no in-package image embedding);
        the QA stamp is overlaid onto the converted PDF. The single text render
        is both the deliverable and the PDF source since no images are baked in."""
        from .format_renderers import render_vsdx, stamp_pdf

        text_context = self._build_template_context(field_mappings=field_mappings)
        clean_path = os.path.join(tmpdir, f"{self.name}.vsdx")
        try:
            render_vsdx(source_path, clean_path, text_context)
        except Exception:
            frappe.log_error(frappe.get_traceback(), "GMP Document: Visio render failed")
            frappe.throw(_("Failed to render Visio template. Check Error Log for details."))

        pdf_bytes = self._convert_to_pdf(soffice, clean_path, tmpdir)

        stamp = self._resolve_stamp_path()
        if stamp:
            # Overlay failure must not sink the whole submit — log and ship the
            # unstamped PDF; the stamp can be regenerated on the server.
            try:
                pdf_bytes = stamp_pdf(pdf_bytes, [{**VSDX_STAMP_PLACEMENT, "image": stamp}])
            except Exception:
                frappe.log_error(
                    frappe.get_traceback(),
                    "GMP Document: Visio PDF stamp overlay failed",
                )

        with open(clean_path, "rb") as fh:
            return fh.read(), pdf_bytes

    def _resolve_render_source(self):
        """Return (source_template_path, field_mappings) for the render.

        The render source is the immutable pristine template that
        ``attachment_file`` points to (never overwritten by a render), so every
        re-render still sees the ``{{ tags }}`` needed to place text, signatures
        and the QA stamp. The linked Word Template contributes only its tag
        mappings (custom_tag -> system_field); it is mandatory, so mappings are
        always read from it."""
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
            "version_number": cint(self.version_number),
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
            # ----- QA status stamp (default: empty for clean DOCX) -----
            "qa_stamp": "",
        }

        if template_for_images is not None:
            # Resolve the actual signer's signature first, then fall back to the
            # assigned reviewer / QA approver. The two are usually the same, but
            # when a workflow step is performed by someone else (e.g. an admin
            # via the escape-hatch) the actor (reviewed_by/approved_by) may have
            # no signature — so without the fallback no signature would render at
            # all. _validate_signatures guarantees the assigned reviewer/
            # qa_approver have a signature, so this fallback always resolves.
            preparer_path = _resolve_signature_path(self.prepared_by)
            reviewer_path = _resolve_signature_path(self.reviewed_by) or _resolve_signature_path(self.reviewer)
            qa_path = _resolve_signature_path(self.approved_by) or _resolve_signature_path(self.qa_approver)
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

            # QA status stamp — approved while active, rejected once obsolete.
            stamp_path = self._resolve_stamp_path()
            if stamp_path:
                context["qa_stamp"] = InlineImage(
                    template_for_images, stamp_path, width=Mm(STAMP_WIDTH_MM)
                )

        # Apply user-defined aliases last so a custom tag can mirror any
        # resolved system value — text fields, the *_name lookups above, or a
        # *_signature InlineImage (so {{ my_sig }} can map to a signature).
        for row in (field_mappings or []):
            tag = (row.custom_tag or "").strip()
            if tag and row.system_field in context:
                context[tag] = context[row.system_field]

        return context

    def _resolve_stamp_path(self):
        """Absolute path to the QA status stamp for the current state, or None.

        - Obsolete (cancelled or auto-expired -> is_active == 0): rejected stamp.
        - Active & QA-approved (approved_by set): approved stamp.
        - Otherwise (draft, not yet approved): no stamp.

        Only ever consulted in the with-images render pass, so the clean
        deliverable .docx stays stamp-free — the stamp lives only in the signed
        base PDF, mirroring how signatures are handled."""
        if _is_pending_effective(self):
            # Future-dated but QA-approved: a re-render while pending must keep
            # the approved stamp, not the rejected one is_active=0 would imply.
            path = _stamp_asset_path(STAMP_APPROVED)
        elif not self.is_active:
            path = _stamp_asset_path(STAMP_REJECTED)
        elif self.approved_by:
            path = _stamp_asset_path(STAMP_APPROVED)
        else:
            return None
        if not os.path.exists(path):
            frappe.log_error(
                f"QA stamp asset missing: {path}",
                "GMP Document: stamp asset not found",
            )
            return None
        return path

    # ------------------------------------------------------------------ #
    #  Workflow side effects (driven by native apply_workflow)           #
    # ------------------------------------------------------------------ #

    def _apply_workflow_side_effects(self):
        """Stamp audit fields and shuffle ToDos when workflow_status changes.

        Human transitions are performed by Frappe's native Workflow engine
        (the "Actions" menu); this hook supplies the GMP bookkeeping the
        engine does not: per-stage who/when stamps, timeline comments for the
        forward AND reverse paths, and the task hand-off along the chain
        (preparer → supervisor → reviewer → QA supervisor → manager →
        regulatory → QA approver). Per-actor authorisation is enforced
        separately by the transition `condition` expressions in install.py.
        The sequential QA queue's own state changes bypass save (db_set in
        the queue engine), so they do their bookkeeping inline, not here.
        """
        before = self.get_doc_before_save()
        if not before:
            return  # initial insert — no transition to react to

        prev = before.workflow_status
        curr = self.workflow_status
        if prev == curr:
            return

        actor = frappe.session.user

        # ---------------- forward path ---------------- #
        if curr == WF_PENDING_SUPERVISOR and prev in (WF_DRAFT, WF_REVISION):
            # Preparer submitted for approval; actors were resolved in
            # validate() during this same save.
            self.add_comment(
                "Workflow",
                _("Submitted for approval by {0} — routed to supervisor {1}").format(actor, self.supervisor),
            )
            _close_open_todos(self, allocated_to=self.prepared_by)
            _create_todo(self, self.supervisor, _("Supervisor approval — GMP Document {0}").format(self.name))

        elif curr == WF_UNDER_REVIEW and prev == WF_PENDING_SUPERVISOR:
            self.db_set("supervisor_approved_by", actor, update_modified=False)
            self.db_set("supervisor_approved_on", now_datetime(), update_modified=False)
            self.add_comment("Workflow", _("Supervisor approved — forwarded to reviewer {0}").format(self.reviewer))
            _close_open_todos(self, allocated_to=self.supervisor)
            _create_todo(self, self.reviewer, _("Review GMP Document {0}").format(self.name))

        elif curr == WF_PENDING_QA_SUPERVISOR and prev == WF_UNDER_REVIEW:
            self.db_set("reviewed_by", actor, update_modified=False)
            self.db_set("reviewed_on", now_datetime(), update_modified=False)
            self.add_comment("Workflow", _("Reviewer approved — forwarded to QA Supervisor {0}").format(self.qa_supervisor))
            _close_open_todos(self, allocated_to=self.reviewer)
            _create_todo(self, self.qa_supervisor, _("QA Supervisor — approve or delegate review of {0}").format(self.name))

        elif curr == WF_PENDING_MANAGER and prev == WF_PENDING_QA_SUPERVISOR:
            # QA Supervisor approved directly, without delegating a queue (or
            # past a halted one — retire any rows still queued from it).
            self._supersede_open_qa_rows()
            self.add_comment("Workflow", _("QA Supervisor approved — forwarded to Manager {0}").format(self.reviewer))
            _close_open_todos(self, allocated_to=self.qa_supervisor)
            _create_todo(self, self.reviewer, _("Manager approval — GMP Document {0}").format(self.name))

        elif curr == WF_PENDING_REGULATORY and prev == WF_PENDING_MANAGER:
            self.db_set("manager_approved_by", actor, update_modified=False)
            self.db_set("manager_approved_on", now_datetime(), update_modified=False)
            self.add_comment("Workflow", _("Manager approved — forwarded to Regulatory {0}").format(self.regulatory_manager))
            _close_open_todos(self, allocated_to=self.reviewer)
            _create_todo(self, self.regulatory_manager, _("Regulatory validation — GMP Document {0}").format(self.name))

        elif curr == WF_PENDING_FINAL_QA and prev == WF_PENDING_REGULATORY:
            self.db_set("regulatory_validated_by", actor, update_modified=False)
            self.db_set("regulatory_validated_on", now_datetime(), update_modified=False)
            self.add_comment("Workflow", _("Regulatory validated — forwarded to QA Approver {0}").format(self.qa_approver))
            _close_open_todos(self, allocated_to=self.regulatory_manager)
            _create_todo(self, self.qa_approver, _("Final QA authorization — publish GMP Document {0}").format(self.name))

        elif curr == WF_APPROVED:
            # Final QA published (this save also auto-submits the document).
            self.db_set("approved_by", actor, update_modified=False)
            self.db_set("approved_on", now_datetime(), update_modified=False)
            self.add_comment("Workflow", _("Final QA authorization granted — document published"))
            _close_open_todos(self, allocated_to=self.qa_approver)

        # ------------- QA delegation recall ------------- #
        elif prev == WF_QA_IN_PROGRESS and curr == WF_PENDING_QA_SUPERVISOR:
            # Manual "Recall Delegation" by the QA Supervisor. (The queue
            # engine's own returns bypass save, so only the human action
            # arrives here.)
            self._supersede_open_qa_rows()
            self.add_comment("Workflow", _("QA delegation recalled by {0}").format(actor))
            _create_todo(self, self.qa_supervisor, _("QA Supervisor — approve or delegate review of {0}").format(self.name))

        # ---------------- reverse path ---------------- #
        elif (prev, curr) in _RETURN_EDGES:
            from_field, to_field, todo_msg = _RETURN_EDGES[(prev, curr)]
            self._stamp_revision_request(actor)
            _close_open_todos(self, allocated_to=self.get(from_field))
            _create_todo(self, self.get(to_field), _(todo_msg).format(self.name))

        elif curr == WF_REVISION_CANCELLED:
            # Draft revision abandoned. Terminal: the record is retained for
            # audit (on_trash blocks deletion) and the revised document remains
            # the effective version — it was never touched by this draft.
            self.db_set("is_active", 0, update_modified=False)
            self.add_comment(
                "Workflow",
                _("Revision cancelled by {0} — {1} remains the effective version.").format(
                    actor, self.revision_of or _("the approved predecessor")
                ),
            )
            self._supersede_open_qa_rows()
            for assignee in (
                self.prepared_by,
                self.supervisor,
                self.reviewer,
                self.qa_supervisor,
                self.regulatory_manager,
                self.qa_approver,
            ):
                _close_open_todos(self, allocated_to=assignee)
            if self.revision_of:
                try:
                    frappe.get_doc("GMP Document", self.revision_of).add_comment(
                        "Info",
                        _("Draft revision {0} was cancelled; this version remains effective.").format(
                            self.name
                        ),
                    )
                except Exception:
                    frappe.log_error(
                        frappe.get_traceback(),
                        "GMP Document: revision-cancelled comment on predecessor failed",
                    )

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

    # ------------------------------------------------------------------ #
    #  Sequential QA review queue engine                                 #
    # ------------------------------------------------------------------ #
    # The queue lives in the qa_reviews child table; row order within the
    # highest `round` IS the review order. The engine (not manual workflow
    # actions) moves the document out of 'QA Review In Progress': forward to
    # 'Pending Manager Approval' once every row of the round is closed with
    # at least one actual approval, or back to 'Pending QA Supervisor' when
    # a reviewer requests a return (or a round ends all-skipped). Server-
    # driven state changes go through _server_transition (db_set + comment)
    # — deliberately outside apply_workflow, which models single-actor
    # role-gated actions and cannot express "advance when the queue drains".

    def _server_transition(self, next_state, comment):
        """Engine-driven state change + audit comment. db_set keeps it out of
        the Workflow engine's transition validation (there is intentionally no
        manual transition for these edges)."""
        self.db_set("workflow_status", next_state, update_modified=False)
        self.add_comment("Workflow", comment)

    def _current_qa_round(self):
        return max((cint(r.round) for r in self.qa_reviews), default=0)

    def _current_round_rows(self):
        rnd = self._current_qa_round()
        return [r for r in self.qa_reviews if cint(r.round) == rnd]

    def _qa_queue_head(self):
        """The single row currently awaiting review, or None."""
        for row in self._current_round_rows():
            if row.status == QA_AWAITING:
                return row
        return None

    def _supersede_open_qa_rows(self):
        """Close every open queue row (any round) and its reviewer's task.
        Rows are retained with status Superseded — the audit record of a
        recalled or cancelled delegation."""
        for row in self.qa_reviews:
            if row.status in QA_OPEN_STATUSES:
                _unassign_queue_reviewer(self, row.reviewer)
                frappe.db.set_value(
                    "GMP QA Review", row.name,
                    {"status": QA_SUPERSEDED, "completed_on": now_datetime()},
                    update_modified=False,
                )
                row.status = QA_SUPERSEDED

    def _activate_next_qa_reviewer(self):
        """Hand the task to the next Queued row of the current round. Returns
        the activated row, or None when the queue is drained."""
        for row in self._current_round_rows():
            if row.status == QA_QUEUED:
                frappe.db.set_value(
                    "GMP QA Review", row.name,
                    {"status": QA_AWAITING, "assigned_on": now_datetime()},
                    update_modified=False,
                )
                row.status = QA_AWAITING
                _assign_queue_reviewer(
                    self, row.reviewer,
                    _("QA review (sequential queue) — GMP Document {0}").format(self.name),
                )
                self.add_comment(
                    "Workflow",
                    _("QA review task handed to {0}").format(row.reviewer),
                )
                return row
        return None

    def _finish_qa_queue(self):
        """The current round has no Queued/Awaiting rows left. Advance to the
        Manager if at least one reviewer actually approved; a round that ended
        with zero approvals (everyone skipped) goes back to the QA Supervisor
        — a skipped-out queue must never count as a completed review."""
        rows = self._current_round_rows()
        approvals = [r for r in rows if r.status == QA_APPROVED]
        if approvals:
            self.db_set("qa_review_complete", 1, update_modified=False)
            self._server_transition(
                WF_PENDING_MANAGER,
                _("All delegated QA reviews completed ({0} approved, {1} skipped) — forwarded to Manager {2}").format(
                    len(approvals),
                    len([r for r in rows if r.status == QA_SKIPPED]),
                    self.reviewer,
                ),
            )
            _create_todo(self, self.reviewer, _("Manager approval — GMP Document {0}").format(self.name))
        else:
            self._server_transition(
                WF_PENDING_QA_SUPERVISOR,
                _("QA review round ended without any completed review — returned to QA Supervisor {0}").format(
                    self.qa_supervisor
                ),
            )
            _create_todo(self, self.qa_supervisor, _("QA Supervisor — approve or delegate review of {0}").format(self.name))

    def _repoint_references_to_self(self):
        """Swing dependents' reference rows from the superseded version to this
        newly-approved one, so cross-references always resolve to the current
        controlled version (Issue #4)."""
        predecessor_name = self.revision_of or self.amended_from
        if not predecessor_name:
            return

        rows = frappe.get_all(
            "GMP Document Reference",
            filters={"referenced_document": predecessor_name},
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
                        predecessor_name, self.name
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
#  Scheduled task                                                        #
# ---------------------------------------------------------------------- #


def activate_effective_documents():
    """Daily sweep: make every QA-approved, future-dated GMP Document whose
    ``effective_date`` has arrived the effective version of its chain.

    Pending documents are the only rows carrying (docstatus 1, workflow
    Approved, is_active 0) — every other inactive submitted document has
    workflow_status Obsolete — so the filter cannot touch retired records.
    Activation performs the hand-over on_submit deferred: flip is_active,
    retire the superseded predecessor, repoint dependents' references. Each
    document commits independently so one failure cannot block the sweep.

    Runs before expire_gmp_documents (hooks order) so a document that becomes
    effective today is active before the expiry filter evaluates."""
    due = frappe.get_all(
        "GMP Document",
        filters={
            "docstatus": 1,
            "is_active": 0,
            "workflow_status": WF_APPROVED,
            "effective_date": ["<=", today()],
        },
        pluck="name",
    )
    for name in due:
        try:
            doc = frappe.get_doc("GMP Document", name)
            doc.db_set("is_active", 1, update_modified=False)
            doc.add_comment(
                "Info",
                _("Effective Date {0} reached — this document is now the effective version.").format(
                    doc.effective_date
                ),
            )
            doc._obsolete_superseded_version()
            doc._repoint_references_to_self()
            frappe.db.commit()
        except Exception:
            frappe.db.rollback()
            frappe.log_error(
                frappe.get_traceback(),
                f"GMP Document: effective-date activation failed for {name}",
            )


def expire_gmp_documents():
    """Daily sweep: obsolete every active, QA-approved GMP Document whose
    ``expiry_date`` has passed, and re-stamp its base PDF with the QA-rejected
    stamp.

    Expiry is NOT cancellation — docstatus stays 1 (the record remains an
    official, submitted revision); only ``is_active`` flips to 0 and the
    lifecycle status becomes Obsolete, which makes _resolve_stamp_path() select
    the rejected asset on re-render. Each document is committed independently so
    one failure (e.g. a stray missing attachment) cannot block the rest of the
    sweep. Rows with a NULL expiry_date never match the ``<=`` filter, so
    documents without a set expiry are left untouched."""
    due = frappe.get_all(
        "GMP Document",
        filters={
            "docstatus": 1,
            "is_active": 1,
            "expiry_date": ["<=", today()],
        },
        pluck="name",
    )
    for name in due:
        try:
            doc = frappe.get_doc("GMP Document", name)
            doc.db_set("is_active", 0, update_modified=False)
            doc.db_set("workflow_status", WF_OBSOLETE, update_modified=False)
            if doc.attachment_file:
                doc._render_and_generate_pdf()
            frappe.db.commit()
        except Exception:
            frappe.db.rollback()
            frappe.log_error(
                frappe.get_traceback(),
                f"GMP Document: auto-expire failed for {name}",
            )


# ---------------------------------------------------------------------- #
#  Whitelisted endpoint                                                  #
# ---------------------------------------------------------------------- #


@frappe.whitelist()
def create_revision(docname, reason_for_change):
    """Start a non-destructive revision of an approved document.

    Unlike Frappe's cancel+amend, the source document is NOT cancelled: it
    stays Approved, active and effective while the new draft revision moves
    through the review workflow. The draft is a separate record linked back
    via ``revision_of``; only when it is QA-approved does on_submit retire the
    predecessor to Obsolete (and only then does the official version number
    advance). Abandoning the draft (workflow action 'Cancel Revision') leaves
    the predecessor untouched and retains the draft as a Revision Cancelled
    audit record.

    Guards (enforced server-side in before_insert):
    - source must be Approved, submitted and active;
    - at most one open revision per document."""
    source = frappe.get_doc("GMP Document", docname)
    source.check_permission("read")

    reason = (reason_for_change or "").strip()
    if not reason:
        frappe.throw(_("Reason for Change is required to start a revision."))

    revision = frappe.copy_doc(source)  # no_copy fields (revision_of, amended_from) reset by copy
    revision.docstatus = 0  # copy_doc may carry the source's submitted docstatus
    revision.revision_of = source.name
    revision.amended_from = None
    revision.reason_for_change = reason
    revision.prepared_by = frappe.session.user
    # before_insert() derives the rest — chain-based name segment, candidate
    # version_number, cleared attachment/dates/audit stamps, workflow Draft —
    # and enforces the Approved+active predecessor and single-open-revision
    # guards. insert() runs with the caller's permissions, so the DocType
    # create permission (QA Manager) is enforced by Frappe itself.
    #
    # ignore_mandatory: change control deliberately strips the predecessor's
    # controlled file, so the freshly created draft starts WITHOUT the
    # mandatory attachment_file — the preparer uploads the revised file next.
    # The gap is closed by the first workflow save (mandatory validation runs
    # on Submit for Review) and again by on_submit.
    revision.flags.ignore_mandatory = True
    revision.insert()

    source.add_comment(
        "Info",
        _("Revision {0} started by {1}; this version remains effective until the revision is approved.").format(
            revision.name, frappe.session.user
        ),
    )
    return revision.name


PDF_VARIANTS = ("controlled", "uncontrolled", "plain")


@frappe.whitelist()
def download_watermarked_pdf(docname, variant=None):
    """Stream the base PDF with a page-spanning watermark.

    ``variant`` selects the copy type (unknown values are rejected):

    - ``None`` / ``"controlled"`` (default): the watermark is resolved from the
      live document status (CONTROLLED COPY / OBSOLETE / DRAFT), so a status
      change is reflected immediately without re-rendering the PDF.
    - ``"uncontrolled"``: every page carries an ``UNCONTROLLED COPY`` watermark,
      plus a footer stamped with the Persian (Jalali) date and time of
      generation. No extra page is added — the existing pages are reused
      verbatim (Issue: uncontrolled reference prints).
    - ``"plain"``: no watermark; only the Jalali print-timestamp footer.

    An obsolete document (is_active == 0) is ALWAYS stamped OBSOLETE, whatever
    variant was requested — a retired document must never leave the system
    looking like a normal approved copy (GMP/data-integrity rule).

    The base PDF path is resolved dynamically and regenerated on demand if the
    File record or the on-disk file has gone missing (Issue #1)."""
    doc = frappe.get_doc("GMP Document", docname)
    doc.check_permission("read")

    variant = (cstr(variant).strip().lower()) or "controlled"
    if variant not in PDF_VARIANTS:
        frappe.throw(
            _("Unknown PDF variant {0}. Valid variants: {1}.").format(
                frappe.bold(variant), ", ".join(PDF_VARIANTS)
            )
        )

    if doc.docstatus != 1:
        frappe.throw(
            _("A PDF is only available after {0} has been QA-approved (submitted).").format(
                docname
            )
        )

    base_pdf_path = _resolve_base_pdf_path(doc)

    if variant == "uncontrolled":
        watermark_text = "UNCONTROLLED COPY"
    elif variant == "plain":
        watermark_text = None
    else:
        watermark_text = _resolve_watermark_text(doc)

    # Status always wins: no variant may hide that a document is retired —
    # or not yet effective. A pending (future-dated, QA-approved) document is
    # not obsolete, but it must not circulate looking like a normal copy.
    if _is_pending_effective(doc):
        watermark_text = "NOT YET EFFECTIVE"
    elif not doc.is_active:
        watermark_text = "OBSOLETE"

    # 21 CFR Part 11: a reference print outside change control is only
    # meaningful with a print timestamp. GMP sites in Iran expect the Jalali
    # calendar. Deliberately NOT translated: the footer is drawn with
    # reportlab's built-in Helvetica (WinAnsi-only), which cannot encode
    # Persian script — a translated string would crash or render as boxes.
    footer_text = None
    if variant == "uncontrolled":
        footer_text = f"Uncontrolled copy - printed {_jalali_now_stamp()} (Jalali) - not subject to change control"
    elif variant == "plain":
        footer_text = f"Printed {_jalali_now_stamp()} (Jalali)"

    watermarked = _apply_watermark(base_pdf_path, watermark_text, footer_text=footer_text)

    safe_label = (watermark_text or "PLAIN").replace(" ", "_")
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


def _resolve_deliverable_path(doc):
    """Return the on-disk path of the clean rendered deliverable (the downloadable
    Word/Excel/Visio file), regenerating from the pristine template if the File
    record or the physical file is missing. Lookup is by the deterministic
    deliverable filename attached to the document."""
    fname = doc._deliverable_filename()

    def _find_on_disk():
        for f in frappe.get_all(
            "File",
            filters={
                "attached_to_doctype": doc.doctype,
                "attached_to_name": doc.name,
                "file_name": fname,
            },
            fields=["name"],
        ):
            path = frappe.get_doc("File", f.name).get_full_path()
            if os.path.exists(path):
                return path
        return None

    path = _find_on_disk()
    if path:
        return path

    # Missing deliverable — regenerate from the pristine template. Regeneration
    # mutates the document and rewrites File records, so restrict it to managers.
    if not _is_unrestricted(frappe.session.user):
        frappe.throw(
            _(
                "The controlled document file for {0} is temporarily unavailable. "
                "Please ask a DMS/QA manager to regenerate it."
            ).format(doc.name)
        )
    if not doc.attachment_file:
        frappe.throw(
            _(
                "The deliverable for {0} is unavailable and cannot be regenerated: "
                "the source template is missing."
            ).format(doc.name)
        )
    doc._render_and_generate_pdf()

    path = _find_on_disk()
    if not path:
        frappe.throw(_("The deliverable for {0} could not be generated.").format(doc.name))
    return path


# ---------------------------------------------------------------------- #
#  Workflow                                                              #
# ---------------------------------------------------------------------- #
#
# Human transitions are driven by Frappe's native Workflow engine (the form
# "Actions" menu). The state machine, per-actor authorisation `condition`s,
# and roles are declared in install.py. Forward chain:
#
#   Draft / Revision Requested ── Submit for Approval ──► Pending Supervisor Approval
#   Pending Supervisor Approval ── Approve (Supervisor) ──► Under Review
#   Under Review ── Approve as Reviewer ──► Pending QA Supervisor
#   Pending QA Supervisor ── Approve (QA Supervisor) ──► Pending Manager Approval
#   Pending QA Supervisor ── delegate_qa_review() ──► QA Review In Progress
#   QA Review In Progress ── queue engine (all reviews done) ──► Pending Manager Approval
#   Pending Manager Approval ── Approve (Manager) ──► Pending Regulatory Validation
#   Pending Regulatory Validation ── Validate (Regulatory) ──► Pending Final QA Approval
#   Pending Final QA Approval ── Publish ──► Approved (docstatus=1)
#
# Every stage also has a one-level "Return …" transition (see _RETURN_EDGES
# for the audit bookkeeping on the reverse path). The sequential QA queue is
# driven by the whitelisted endpoints below, not by manual transitions.
#
# Audit stamping and ToDo hand-off run in GMPDocument._apply_workflow_side_effects
# (invoked from on_update / on_submit). The controller reacts to
# workflow_status changes instead of owning the human transitions.


@frappe.whitelist()
def get_my_pending_count(user=None):
    """Used by the workspace pending-count badge: how many documents are
    waiting on this user at each stage of the chain, including the QA queue
    (counted only when the user is the row currently awaiting review)."""
    user = user or frappe.session.user
    stage_actor = (
        ("supervisor_approval", WF_PENDING_SUPERVISOR, "supervisor"),
        ("to_review", WF_UNDER_REVIEW, "reviewer"),
        ("qa_supervisor", WF_PENDING_QA_SUPERVISOR, "qa_supervisor"),
        ("manager_approval", WF_PENDING_MANAGER, "reviewer"),
        ("regulatory", WF_PENDING_REGULATORY, "regulatory_manager"),
        ("to_approve", WF_PENDING_FINAL_QA, "qa_approver"),
        ("to_revise", WF_REVISION, "prepared_by"),
    )
    counts = {}
    for key, state, actor_field in stage_actor:
        counts[key] = frappe.db.count("GMP Document", filters={
            "docstatus": 0, "workflow_status": state, actor_field: user,
        })
    counts["qa_queue"] = frappe.db.count("GMP QA Review", filters={
        "parenttype": "GMP Document", "reviewer": user, "status": QA_AWAITING,
    })
    counts["total"] = sum(counts.values())
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


def _assign_queue_reviewer(doc, user, description):
    """Native assignment (updates `_assign` + creates the ToDo) so the queued
    reviewer sees the document under 'Assigned to me'. Falls back to a plain
    ToDo if the assignment API refuses (e.g. the user already holds one)."""
    from frappe.desk.form.assign_to import add as assign_add

    try:
        assign_add(
            {
                "assign_to": [user],
                "doctype": doc.doctype,
                "name": doc.name,
                "description": description,
            },
            ignore_permissions=True,
        )
    except Exception:
        _create_todo(doc, user, description)


def _unassign_queue_reviewer(doc, user):
    """Mirror of _assign_queue_reviewer: clears `_assign` and the ToDo."""
    from frappe.desk.form.assign_to import remove as assign_remove

    if not user:
        return
    try:
        assign_remove(doc.doctype, doc.name, user, ignore_permissions=True)
    except Exception:
        pass
    _close_open_todos(doc, allocated_to=user)


# ---------------------------------------------------------------------- #
#  Sequential QA review queue: whitelisted endpoints                     #
# ---------------------------------------------------------------------- #


def _get_doc_for_qa_action(docname, expected_state):
    doc = frappe.get_doc("GMP Document", docname)
    if doc.workflow_status != expected_state:
        frappe.throw(
            _("This action requires the document to be in state {0} (it is {1}).").format(
                frappe.bold(_(expected_state)), frappe.bold(_(doc.workflow_status))
            )
        )
    return doc


def _require_qa_supervisor(doc):
    user = frappe.session.user
    if (
        user == doc.qa_supervisor
        or user == "Administrator"
        or {"DMS Manager", "System Manager"} & set(frappe.get_roles(user))
    ):
        return
    frappe.throw(
        _("Only the QA Supervisor assigned to this document may perform this action."),
        frappe.PermissionError,
    )


@frappe.whitelist()
def delegate_qa_review(docname, reviewers):
    """QA Supervisor hands the document to a SEQUENTIAL queue of reviewers.

    ``reviewers`` is an ordered list of User ids (JSON string or list); order
    is the review order. Only the first reviewer receives the task; each
    completion hands it to the next. The document leaves 'Pending QA
    Supervisor' for 'QA Review In Progress' and only proceeds to the Manager
    once every queued reviewer has completed (or been skipped with reason —
    at least one real approval is required)."""
    doc = _get_doc_for_qa_action(docname, WF_PENDING_QA_SUPERVISOR)
    _require_qa_supervisor(doc)

    reviewers = frappe.parse_json(reviewers) if isinstance(reviewers, str) else reviewers
    reviewers = [r for r in (reviewers or []) if r]
    if not reviewers:
        frappe.throw(_("Select at least one reviewer to delegate to."))
    if len(reviewers) != len(set(reviewers)):
        frappe.throw(_("The same reviewer appears more than once in the queue."))

    for user in reviewers:
        if not frappe.db.get_value("User", user, "enabled"):
            frappe.throw(_("User {0} does not exist or is disabled.").format(frappe.bold(user)))
        # Separation of duties: the author must never QA-review their own
        # document.
        if user == doc.prepared_by:
            frappe.throw(_("The preparer ({0}) cannot be a QA reviewer of their own document.").format(frappe.bold(user)))

    # Retire any leftover open rows of earlier rounds, then append the new
    # round. Old rows are kept — the delegation history is audit data.
    doc._supersede_open_qa_rows()
    rnd = doc._current_qa_round() + 1
    for user in reviewers:
        doc.append("qa_reviews", {"reviewer": user, "status": QA_QUEUED, "round": rnd})
    doc.db_set("qa_review_complete", 0, update_modified=False)
    doc.save(ignore_permissions=True)

    _close_open_todos(doc, allocated_to=doc.qa_supervisor)
    doc._server_transition(
        WF_QA_IN_PROGRESS,
        _("QA Supervisor {0} delegated sequential review (round {1}) to: {2}").format(
            frappe.session.user, rnd, " → ".join(reviewers)
        ),
    )
    doc._activate_next_qa_reviewer()
    return {"round": rnd, "reviewers": reviewers}


@frappe.whitelist()
def complete_qa_review(docname, decision, comments=None):
    """The reviewer currently holding the queue head records their outcome.

    decision 'Approve' → the task passes to the next reviewer in the queue
    (or, when the queue drains, the document moves on to the Manager).
    decision 'Return' → the queue halts and the document goes back to the QA
    Supervisor; a reason is mandatory."""
    doc = _get_doc_for_qa_action(docname, WF_QA_IN_PROGRESS)
    head = doc._qa_queue_head()
    if not head:
        frappe.throw(_("No QA reviewer is currently awaiting review on this document."))
    user = frappe.session.user
    if user not in (head.reviewer, "Administrator"):
        frappe.throw(
            _("It is {0}'s turn in the review queue — only they can complete this review.").format(
                frappe.bold(head.reviewer)
            ),
            frappe.PermissionError,
        )
    if decision not in ("Approve", "Return"):
        frappe.throw(_("Decision must be Approve or Return."))
    comments = (comments or "").strip()
    if decision == "Return" and not comments:
        frappe.throw(_("A reason is mandatory when returning the document."))

    status = QA_APPROVED if decision == "Approve" else QA_RETURNED
    frappe.db.set_value(
        "GMP QA Review", head.name,
        {"status": status, "completed_on": now_datetime(), "comments": comments},
        update_modified=False,
    )
    head.status = status
    _unassign_queue_reviewer(doc, head.reviewer)
    doc.add_comment(
        "Workflow",
        _("QA review by {0}: {1}{2}").format(
            head.reviewer, _(status), " — {0}".format(comments) if comments else ""
        ),
    )

    if status == QA_RETURNED:
        doc._server_transition(
            WF_PENDING_QA_SUPERVISOR,
            _("QA reviewer {0} returned the document to the QA Supervisor").format(head.reviewer),
        )
        _create_todo(doc, doc.qa_supervisor, _("QA Supervisor — reviewer returned {0}").format(doc.name))
        return {"status": status, "next": None}

    nxt = doc._activate_next_qa_reviewer()
    if not nxt:
        doc._finish_qa_queue()
    return {"status": status, "next": nxt.reviewer if nxt else None}


@frappe.whitelist()
def skip_qa_reviewer(docname, reason):
    """QA Supervisor skips the reviewer currently holding the queue head
    (absence cover). The skip and its mandatory reason are logged; the task
    passes to the next reviewer in the queue. A round in which everyone was
    skipped returns to the QA Supervisor instead of advancing."""
    doc = _get_doc_for_qa_action(docname, WF_QA_IN_PROGRESS)
    _require_qa_supervisor(doc)
    reason = (reason or "").strip()
    if not reason:
        frappe.throw(_("A reason is mandatory when skipping a reviewer."))
    head = doc._qa_queue_head()
    if not head:
        frappe.throw(_("No QA reviewer is currently awaiting review on this document."))

    frappe.db.set_value(
        "GMP QA Review", head.name,
        {"status": QA_SKIPPED, "completed_on": now_datetime(), "skip_reason": reason},
        update_modified=False,
    )
    head.status = QA_SKIPPED
    _unassign_queue_reviewer(doc, head.reviewer)
    doc.add_comment(
        "Workflow",
        _("QA reviewer {0} skipped by {1}: {2}").format(head.reviewer, frappe.session.user, reason),
    )

    nxt = doc._activate_next_qa_reviewer()
    if not nxt:
        doc._finish_qa_queue()
    return {"skipped": head.reviewer, "next": nxt.reviewer if nxt else None}


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


# Fields naming a workflow participant on the document. Any user in one of
# them (or in the QA review queue) may read the document throughout its
# lifecycle — restricted access during the pipeline, but participants are
# never locked out.
_PARTICIPANT_FIELDS = (
    "prepared_by",
    "supervisor",
    "reviewer",
    "qa_supervisor",
    "regulatory_manager",
    "qa_approver",
)

# Which participant may WRITE while the document sits in each state: the
# preparer while authoring, otherwise the actor the state is waiting on
# (apply_workflow saves as the acting user, so the current actor needs write).
_STATE_WRITE_ACTOR = {
    WF_DRAFT: "prepared_by",
    WF_REVISION: "prepared_by",
    WF_PENDING_SUPERVISOR: "supervisor",
    WF_UNDER_REVIEW: "reviewer",
    WF_PENDING_QA_SUPERVISOR: "qa_supervisor",
    WF_QA_IN_PROGRESS: "qa_supervisor",
    WF_PENDING_MANAGER: "reviewer",
    WF_PENDING_REGULATORY: "regulatory_manager",
    WF_PENDING_FINAL_QA: "qa_approver",
}


def get_permission_query_conditions(user=None):
    """SQL visibility filter for GMP Document lists, reports and search.

    Empty string = no restriction (unrestricted roles). Restricted users get
    their department's approved/active documents OR any document naming them
    as a workflow participant (including the delegated QA review queue)."""
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
    for fieldname in _PARTICIPANT_FIELDS:
        conditions.append("`tabGMP Document`.{0} = {1}".format(fieldname, u))
    conditions.append(
        "exists (select 1 from `tabGMP QA Review` qr "
        "where qr.parenttype = 'GMP Document' "
        "and qr.parent = `tabGMP Document`.name "
        "and qr.reviewer = {0})".format(u)
    )

    return "(" + " or ".join(conditions) + ")"


def _in_qa_queue(doc, user):
    if doc.get("qa_reviews") is not None:
        return any(r.reviewer == user for r in doc.qa_reviews)
    return bool(
        doc.get("name")
        and frappe.db.exists(
            "GMP QA Review",
            {"parenttype": "GMP Document", "parent": doc.get("name"), "reviewer": user},
        )
    )


def has_permission(doc, ptype="read", user=None):
    """Per-document gate mirroring get_permission_query_conditions().

    Returning False denies; True/None defers to the role permission.
    Unrestricted roles always pass. A workflow participant may always read
    (and print) the document; write is granted only to the participant the
    current state is waiting on (that's what apply_workflow saves as). A
    plain member may read an approved, active document of their department.
    Everything else is denied here (role perms already withhold
    create/delete/etc. — this is defence in depth)."""
    if doc is None:
        return None  # doctype-level check; leave to role perms + query conditions

    user = user or frappe.session.user
    if _is_unrestricted(user):
        return True

    if getattr(doc, "is_new", None) and doc.is_new():
        return None  # creation is governed by role perms (DMS Initiator)

    read_like = ptype in ("read", "print")
    named = user in tuple(doc.get(f) for f in _PARTICIPANT_FIELDS)
    if named:
        if read_like:
            return True
        write_actor_field = _STATE_WRITE_ACTOR.get(doc.get("workflow_status"))
        return bool(
            ptype == "write"
            and cint(doc.get("docstatus")) == 0
            and write_actor_field
            and doc.get(write_actor_field) == user
        )

    if _in_qa_queue(doc, user):
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
            # Logical base = the ID without the trailing version segment
            # ([Dept]-[Type]-[Number]), so every version collapses to one node.
            base = d.name.rsplit("-", 1)[0]
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
    """Stream the clean rendered deliverable (text fields rendered, no signatures
    or QA stamp) in its native format (.docx/.xlsx/.vsdx).

    This is the CLEAN deliverable file, kept separate from the immutable pristine
    template at ``attachment_file`` — so it never exposes the raw ``{{ tags }}``.
    The signed PDF lives at /api/method/...download_watermarked_pdf; this endpoint
    serves the editable counterpart that GMP audits can store without exposing
    actor signatures."""
    doc = frappe.get_doc("GMP Document", docname)
    doc.check_permission("read")

    # The clean deliverable is a manager-only control-distribution artifact;
    # department members are limited to the watermarked controlled-copy PDF.
    if not _is_unrestricted(frappe.session.user):
        frappe.throw(
            _("Only DMS/QA managers may download the source document file. Use 'Download PDF (Controlled Copy)' instead."),
            frappe.PermissionError,
        )

    if not doc.attachment_file:
        frappe.throw(_("No template is attached to {0}.").format(docname))

    physical_path = _resolve_deliverable_path(doc)
    with open(physical_path, "rb") as fh:
        content = fh.read()

    frappe.local.response.filename = f"{docname}{doc._controlled_ext()}"
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


def _is_pending_effective_date(effective_date):
    """True when the date lies in the future (document scheduled, not yet
    effective)."""
    return bool(effective_date) and getdate(effective_date) > getdate(today())


def _is_pending_effective(doc):
    """True for a QA-approved document that is not yet the effective version:
    submitted and still workflow-Approved, but deliberately kept inactive so
    the predecessor (if any) stays effective. Deliberately NOT date-gated —
    between the effective date arriving and the daily activation sweep
    running, the document is still pending, not obsolete. Obsoleted documents
    also carry is_active == 0 but their workflow_status is Obsolete, never
    Approved."""
    return (
        doc.docstatus == 1
        and doc.workflow_status == WF_APPROVED
        and not cint(doc.is_active)
    )


def _resolve_watermark_text(doc):
    if doc.docstatus == 1 and doc.is_active:
        return "CONTROLLED COPY"
    if _is_pending_effective(doc):
        # Approved but future-dated: not obsolete, but must not read as a
        # controlled effective copy either.
        return "NOT YET EFFECTIVE"
    if not doc.is_active:
        return "OBSOLETE"
    return "DRAFT - NOT FOR USE"


def _gregorian_to_jalali(gy, gm, gd):
    """Convert a Gregorian date to the Jalali (Solar Hijri) calendar.

    Pure-python implementation of the standard `jdf` algorithm — avoids adding a
    third-party calendar dependency for a single stamp. Returns (jy, jm, jd)."""
    g_days_in_month = [0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334]
    gy2 = gy + 1 if gm > 2 else gy
    days = (
        355666
        + (365 * gy)
        + ((gy2 + 3) // 4)
        - ((gy2 + 99) // 100)
        + ((gy2 + 399) // 400)
        + gd
        + g_days_in_month[gm - 1]
    )
    jy = -1595 + (33 * (days // 12053))
    days %= 12053
    jy += 4 * (days // 1461)
    days %= 1461
    if days > 365:
        jy += (days - 1) // 365
        days = (days - 1) % 365
    if days < 186:
        jm = 1 + (days // 31)
        jd = 1 + (days % 31)
    else:
        jm = 7 + ((days - 186) // 30)
        jd = 1 + ((days - 186) % 30)
    return jy, jm, jd


def _jalali_now_stamp():
    """Current site-time as a Jalali `YYYY/MM/DD HH:MM:SS` stamp."""
    now = now_datetime()
    jy, jm, jd = _gregorian_to_jalali(now.year, now.month, now.day)
    return f"{jy:04d}/{jm:02d}/{jd:02d} {now.hour:02d}:{now.minute:02d}:{now.second:02d}"


def _apply_watermark(pdf_path, watermark_text, footer_text=None):
    """Merge a diagonal watermark and/or a footer line onto every page.

    ``watermark_text=None`` skips the diagonal stamp (plain variant); both
    strings must stay WinAnsi-encodable (built-in Helvetica)."""
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
        if watermark_text:
            c.saveState()
            c.translate(width / 2, height / 2)
            c.rotate(45)
            c.setFillColor(Color(0.85, 0.10, 0.10, alpha=0.30))
            c.setFont("Helvetica-Bold", 80)
            c.drawCentredString(0, 0, watermark_text)
            c.restoreState()

        if footer_text:
            c.saveState()
            c.setFillColor(Color(0.20, 0.20, 0.20, alpha=0.85))
            c.setFont("Helvetica", 8)
            c.drawCentredString(width / 2, 18, footer_text)
            c.restoreState()

        c.save()
        overlay_buffer.seek(0)

        overlay_page = PdfReader(overlay_buffer).pages[0]
        page.merge_page(overlay_page)
        writer.add_page(page)

    output = BytesIO()
    writer.write(output)
    return output.getvalue()
