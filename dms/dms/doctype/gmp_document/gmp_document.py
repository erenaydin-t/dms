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
import re
import shutil
import signal
import subprocess
import tempfile
from io import BytesIO

import frappe
from frappe import _
from frappe.utils import add_months, add_years, cint, cstr, getdate, now_datetime, today
from frappe.utils.nestedset import NestedSet

from docx.shared import Mm
from docxtpl import DocxTemplate, InlineImage
from jinja2.sandbox import SandboxedEnvironment

from dms.dms import storage

from .font_enforcement import enforce_docx_font, enforce_vsdx_font, font_covers, shape_rtl


# Delegated-approval role: may perform any stage's workflow action on behalf of
# the assigned role-holder (install.py ships a proxy twin of every actor-gated
# transition). It is the named, auditable alternative to sharing the
# Administrator account when an approver is unavailable — every action it takes
# is recorded on the document as "<actor> on behalf of <role-holder>" via the
# `*_on_behalf_of` fields.
PROXY_ROLE = "DMS Proxy Approver"

# Roles that read and operate on every GMP Document regardless of department or
# creator. "DMS Manager" is the module-owner/admin role (full CRUD + cancel);
# "QA Manager" drives the review/approval workflow; "System Manager" is the
# Frappe super-admin; the proxy approver must reach every document to be able
# to stand in for any actor. Everyone else (a plain "Employee") is a read-only
# consumer scoped to their own department's approved, active documents — see
# get_permission_query_conditions() / has_permission().
UNRESTRICTED_ROLES = frozenset({"System Manager", "QA Manager", "DMS Manager", PROXY_ROLE})

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


def _document_font_policy():
    """The enforced-font policy, or None when enforcement is off/unconfigured.

    Isolated here so every render path asks the same question, and so a broken
    or missing DMS Settings can never take the render down with it — an
    unresolvable policy simply means "leave the template's own fonts alone"."""
    try:
        from dms.dms.doctype.dms_settings.dms_settings import resolve_document_font

        policy = resolve_document_font()
    except Exception:
        frappe.log_error(frappe.get_traceback(), "GMP Document: font policy lookup failed")
        return None
    return policy if policy.get("enforce") else None


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
    ("supervisor", "Supervisor (user)"),
    ("supervisor_name", "Supervisor (name)"),
    ("reviewer", "Reviewer (user)"),
    ("reviewer_name", "Reviewer (name)"),
    ("qa_supervisor", "QA Supervisor (user)"),
    ("qa_supervisor_name", "QA Supervisor (name)"),
    ("regulatory_manager", "Regulatory Manager (user)"),
    ("regulatory_manager_name", "Regulatory Manager (name)"),
    ("qa_approver", "QA Approver (user)"),
    ("qa_approver_name", "QA Approver (name)"),
    ("ceo", "CEO (user)"),
    ("ceo_name", "CEO Name"),
    ("reviewed_by", "Reviewed By (user)"),
    ("reviewed_by_name", "Reviewed By (name)"),
    ("reviewed_on", "Reviewed On"),
    ("approved_by", "Approved By (user)"),
    ("approved_by_name", "Approved By (name)"),
    ("approved_on", "Approved On"),
    ("workflow_status", "Workflow Status"),
    # ----- approval timeline (one Date per stage) -----
    ("preparer_date", "Preparer Date"),
    ("supervisor_date", "Supervisor Date"),
    ("reviewer_date", "Reviewer Date"),
    ("qa_supervisor_date", "QA Supervisor Date"),
    ("regulatory_date", "Regulatory Date"),
    ("manager_date", "Manager Date"),
    ("qa_approver_date", "QA Approver Date"),
    ("ceo_date", "CEO Date"),
    ("publish_date", "Publish Date"),
    ("current_date", "Current Date (date of printing)"),
    # ----- delegated approvals ("on behalf of") -----
    ("supervisor_on_behalf_of_name", "Supervisor — On Behalf Of (name)"),
    ("reviewer_on_behalf_of_name", "Reviewer — On Behalf Of (name)"),
    ("qa_supervisor_on_behalf_of_name", "QA Supervisor — On Behalf Of (name)"),
    ("regulatory_on_behalf_of_name", "Regulatory — On Behalf Of (name)"),
    ("manager_on_behalf_of_name", "Manager — On Behalf Of (name)"),
    ("qa_approver_on_behalf_of_name", "QA Approver — On Behalf Of (name)"),
    # ----- images -----
    ("preparer_signature", "Preparer Signature (image)"),
    ("supervisor_signature", "Supervisor Signature (image)"),
    ("reviewer_signature", "Reviewer Signature (image)"),
    ("qa_signature", "QA Approver Signature (image)"),
    ("ceo_signature", "CEO Signature (image)"),
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
    (WF_PENDING_REGULATORY, WF_PENDING_QA_SUPERVISOR): ("regulatory_manager", "qa_supervisor",  "Re-assess — Regulatory returned {0} to QA"),
    (WF_PENDING_MANAGER, WF_PENDING_REGULATORY):   ("reviewer",           "regulatory_manager", "Re-validate — Manager returned {0}"),
    (WF_PENDING_FINAL_QA, WF_PENDING_MANAGER):     ("qa_approver",        "reviewer",           "Re-approve — Final QA returned {0}"),
}


# Reference types that name one particular version on purpose; approving a
# new version must not move them (see _repoint_references_to_self).
_VERSION_PINNED_REFERENCE_TYPES = ("Supersedes", "Derived From")


# Per-stage audit wiring — the single source of truth for the approval
# timeline. For each stage of the chain:
#   actor     — field naming the user the stage is assigned to
#   by        — field recording who ACTUALLY performed it (None for the
#               preparer, whose assignment field already is that record)
#   on        — precise timestamp
#   date      — report-friendly Date mirror of `on`, so turnaround between
#               stages can be filtered and grouped without datetime maths
#   on_behalf — filled only when a delegated approver acted for `actor`
#   signature — optional field snapshotting the signature image applied here
_STAGE_STAMPS = {
    "preparer": {
        "actor": "prepared_by",
        "by": None,
        "on": "submitted_on",
        "date": "preparer_date",
        "on_behalf": None,
        "signature": None,
        "label": "Preparer",
    },
    "supervisor": {
        "actor": "supervisor",
        "by": "supervisor_approved_by",
        "on": "supervisor_approved_on",
        "date": "supervisor_date",
        "on_behalf": "supervisor_on_behalf_of",
        "signature": "supervisor_signature",
        "label": "Supervisor",
    },
    "reviewer": {
        "actor": "reviewer",
        "by": "reviewed_by",
        "on": "reviewed_on",
        "date": "reviewer_date",
        "on_behalf": "reviewer_on_behalf_of",
        "signature": None,
        "label": "Reviewer",
    },
    "qa_supervisor": {
        "actor": "qa_supervisor",
        "by": "qa_supervisor_approved_by",
        "on": "qa_supervisor_approved_on",
        "date": "qa_supervisor_date",
        "on_behalf": "qa_supervisor_on_behalf_of",
        "signature": None,
        "label": "QA Supervisor",
    },
    "regulatory": {
        "actor": "regulatory_manager",
        "by": "regulatory_validated_by",
        "on": "regulatory_validated_on",
        "date": "regulatory_date",
        "on_behalf": "regulatory_on_behalf_of",
        "signature": None,
        "label": "Regulatory Manager",
    },
    # The Manager step is performed by the same user who reviewed earlier —
    # hence actor "reviewer" — but it is a distinct stage with its own stamps.
    "manager": {
        "actor": "reviewer",
        "by": "manager_approved_by",
        "on": "manager_approved_on",
        "date": "manager_date",
        "on_behalf": "manager_on_behalf_of",
        "signature": None,
        "label": "Manager",
    },
    "qa_approver": {
        "actor": "qa_approver",
        "by": "approved_by",
        "on": "approved_on",
        "date": "qa_approver_date",
        "on_behalf": "qa_approver_on_behalf_of",
        "signature": None,
        "label": "QA Approver",
    },
}


# What an approval cycle writes: the per-stage stamps, timeline, delegations
# and signature snapshots, plus the CEO block, the publication date and the
# return bookkeeping. Written only by the controller, never by a form save.
_APPROVAL_RECORD_FIELDS = tuple(
    dict.fromkeys(
        [
            fieldname
            for spec in _STAGE_STAMPS.values()
            for fieldname in (spec["by"], spec["on"], spec["date"], spec["on_behalf"], spec["signature"])
            if fieldname
        ]
        + [
            "reviewed_by",
            "reviewed_on",
            "approved_by",
            "approved_on",
            "qa_review_complete",
            "last_revision_request",
            "last_revision_by",
            "last_revision_on",
            "publish_date",
            "ceo",
            "ceo_name",
            "ceo_date",
            "ceo_signature",
        ]
    )
)

# Fields only the controller writes on a saved draft. Frappe enforces
# read_only in the form, not on the server, so validate() puts back the stored
# value of each whenever a save (form or REST) carries a different one:
# - the approval record — a forged CEO block, signature or "on behalf of"
#   would print on the approved PDF;
# - revision_of — pointing a draft at another document would retire THAT
#   document on approval; clearing it would leave two effective versions;
# - the integrity hash and the pinned render font.
# The controller writes all of them with db_set (or, for a new attachment,
# in before_save, which runs after this check), so its own updates are kept.
_SYSTEM_OWNED_FIELDS = _APPROVAL_RECORD_FIELDS + (
    "revision_of",
    "file_integrity_hash",
    "render_font_family",
    "render_preserve_symbol_fonts",
    "pending_with",
    "pending_with_name",
    "pending_since",
)

# The approval chain in display order: each stage and the workflow states in
# which the document waits on it. Drives both "who is it waiting on" (the
# stage's actor, from _STAGE_STAMPS) and the progress bar on the form. The
# delegated QA review sits inside the QA Supervisor stage, whose queue is
# shown as sub-steps.
_PROGRESS_STAGES = (
    ("preparer", (WF_DRAFT, WF_REVISION)),
    ("supervisor", (WF_PENDING_SUPERVISOR,)),
    ("reviewer", (WF_UNDER_REVIEW,)),
    ("qa_supervisor", (WF_PENDING_QA_SUPERVISOR, WF_QA_IN_PROGRESS)),
    ("regulatory", (WF_PENDING_REGULATORY,)),
    ("manager", (WF_PENDING_MANAGER,)),
    ("qa_approver", (WF_PENDING_FINAL_QA,)),
)
_STAGE_OF_STATE = {state: stage for stage, states in _PROGRESS_STAGES for state in states}


def _acts_on_behalf(user):
    """True when ``user`` may act for another role-holder.

    Two accounts can: a holder of the delegated-approval role, and
    Administrator — the long-standing escape hatch, which is the same
    situation and is now recorded the same way rather than silently."""
    if not user:
        return False
    if user == "Administrator":
        return True
    return PROXY_ROLE in frappe.get_roles(user)


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
        # The abbreviation is free text on Department. A dash would add a fifth
        # name segment, "_" and "%" are LIKE wildcards in the number lookup,
        # and a slash or space breaks the file names derived from the ID.
        if any(ch in dept_abbr for ch in r' -_%/\:*?"<>|'):
            frappe.throw(
                _(
                    "The abbreviation {0} of department {1} cannot be used in a document ID: "
                    "remove spaces, dashes, underscores, % and path characters."
                ).format(frappe.bold(dept_abbr), self.department)
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

        # A new record starts with no approval history, whatever the caller
        # sent: the form never shows these fields as editable, but an API
        # insert could pre-fill a CEO block, a signature or a delegation.
        self._reset_approval_record()

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
        #
        # Since 2.8.0 the predecessor's source is a stored file, whose URL no
        # upload ever produces — so that URL is always inherited.
        inherited_attachment = bool(storage.parse_file_url(self.attachment_file))
        if self.attachment_file and not inherited_attachment:
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
        # The stage stamps were already blanked by _reset_approval_record().
        self.supervisor = None
        self.reviewer = None
        self.qa_supervisor = None
        self.regulatory_manager = None
        self.qa_approver = None

    def _reset_approval_record(self):
        """Blank everything an approval cycle records: the stage stamps and
        timeline, the delegation record, the signature snapshots, the CEO
        block, the QA queue and the return bookkeeping. A revision earns its
        own; a brand-new document has none."""
        self.qa_reviews = []
        for fieldname in _APPROVAL_RECORD_FIELDS:
            self.set(fieldname, 0 if fieldname == "qa_review_complete" else None)

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

        self._restore_system_owned_fields()

        if (self.amended_from or self.revision_of) and not (
            self.reason_for_change and self.reason_for_change.strip()
        ):
            frappe.throw(_("Reason for Change is mandatory when revising a GMP Document."))
        self._enforce_effective_date_policy()
        self._check_circular_references()
        self._resolve_workflow_actors_on_submit_for_approval()
        self._resolve_stage_actor_if_missing()
        self._validate_signatures()

    def _restore_system_owned_fields(self):
        """Put back the stored value of every field only the controller may
        write (see _SYSTEM_OWNED_FIELDS). Silent, like the effective-date
        normalisation: a form opened before an approver's stamp landed carries
        the old, empty values, and saving it must neither fail nor wipe the
        stamp."""
        if self.is_new() or self.docstatus != 0:
            return
        before = self.get_doc_before_save()
        if not before:
            return
        for fieldname in _SYSTEM_OWNED_FIELDS:
            if self.get(fieldname) != before.get(fieldname):
                self.set(fieldname, before.get(fieldname))

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

        previous = self.get_doc_before_save()
        if not cint(self.set_effective_date):
            # A draft never holds a system date — the system stamps it only at
            # submit — so the only value to keep is one saved while the box
            # was already off. A date saved under a ticked box is a manual one
            # and must go when the box is unticked, or it would survive to
            # submit and schedule (or backdate) the document after all.
            system_value = (
                previous.effective_date
                if previous and not cint(previous.set_effective_date)
                else None
            )
            if cstr(self.effective_date) != cstr(system_value):
                self.effective_date = system_value
                self.expiry_date = self.next_revision_date = None
                self._calculate_lifecycle_dates()
            return

        # Only a change needs the privilege. Every workflow action saves the
        # whole document, so checking the mere presence of a ticked box would
        # block each approver after the manager who set the date.
        if (
            previous
            and cint(previous.set_effective_date)
            and cstr(previous.effective_date) == cstr(self.effective_date)
        ):
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
        """Prevent this document's references from leading back to itself
        (A → B → A).

        Only a loop through THIS document is an error. Loops elsewhere in the
        graph are not this save's doing and can arise legitimately: approving
        a revision repoints its dependents' references onto it, so an SOP and
        a form that cite each other end up in a loop. Throwing on those would
        make both documents impossible to revise."""
        if not self.references:
            return

        seen = set()
        stack = []
        for row in self.references:
            if row.referenced_document == self.name:
                frappe.throw(_("A document cannot reference itself."))
            stack.append(row.referenced_document)

        while stack:
            doc_name = stack.pop()
            if doc_name in seen:
                continue
            seen.add(doc_name)
            for child in frappe.get_all(
                "GMP Document Reference",
                filters={"parenttype": "GMP Document", "parent": doc_name},
                pluck="referenced_document",
            ):
                if child == self.name:
                    frappe.throw(
                        _("Circular reference detected: {0} already leads back to {1}.").format(
                            frappe.bold(doc_name), frappe.bold(self.name)
                        )
                    )
                stack.append(child)

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
        before = self.get_doc_before_save()
        self._refresh_pending(moved=not before or before.workflow_status != self.workflow_status)

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
        # Before the render, so the CEO's signature is resolved and embedded in
        # the same pass as the other actors' signatures.
        self._stamp_ceo_authorization()

        if not self.effective_date:
            self.effective_date = today()
            self._calculate_lifecycle_dates()
            self.db_set("effective_date", self.effective_date, update_modified=False)
            self.db_set("expiry_date", self.expiry_date, update_modified=False)
            self.db_set("next_revision_date", self.next_revision_date, update_modified=False)

        # Publication date = the day the document actually enters force in the
        # system. A future-dated document is approved but NOT yet published, so
        # it is left blank here and stamped by activate_effective_documents()
        # on the day it goes live; a backdated one still publishes today, since
        # that is when it became the version people are handed.
        if not _is_pending_effective_date(self.effective_date):
            self.db_set("publish_date", today(), update_modified=False)

        # Approval makes this the live version (a future-dated one is switched
        # off again below, after the render). Never keep an is_active a draft
        # happened to carry: an unticked box would publish a document nobody
        # can see, rendered with the rejected stamp.
        if not cint(self.is_active):
            self.db_set("is_active", 1, update_modified=False)

        # The only point where DMS Settings decides the font: every later
        # re-render of this version reuses what is frozen here.
        self._pin_render_font()
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
        approved_by, this only tops up the Date mirror; otherwise it stamps the
        whole stage from the submitting session user. Either way approved_by is
        guaranteed populated before the signature render so the approver's
        signature is embedded in the PDF."""
        if not self.approved_by and not self.is_new():
            self._stamp_stage("qa_approver", frappe.session.user)
            return

        # Fallbacks for the paths db_set cannot serve (a brand-new document) and
        # for documents whose approval predates the timeline fields.
        if not self.approved_by:
            self.approved_by = frappe.session.user
        if not self.approved_on:
            self.approved_on = now_datetime()
        if not self.qa_approver_date:
            self.qa_approver_date = getdate(self.approved_on)
        if not self.is_new():
            self.db_set("approved_by", self.approved_by, update_modified=False)
            self.db_set("approved_on", self.approved_on, update_modified=False)
            self.db_set("qa_approver_date", self.qa_approver_date, update_modified=False)

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
        # Its stored files go with it (removed once the deletion commits).
        storage.delete_document_files(self.name)

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

        if self.attachment_file == storage.file_url(self.name, self._source_filename()):
            # Already this document's own stored source (e.g. a re-save with no
            # new upload) — just keep the integrity hash current.
            row = storage.get_row(self.name, self._source_filename())
            if not row:
                frappe.throw(_("Attached file is missing: {0}").format(self._source_filename()))
            self.file_integrity_hash = row.sha256
            return

        if storage.parse_file_url(self.attachment_file):
            # A file stored for another document. A revision never inherits one
            # (before_insert clears it), so this can only be an attempt to copy a
            # file the user may not be allowed to download — upload it instead.
            frappe.throw(
                _("Upload the file itself: a file stored for another GMP Document cannot be attached.")
            )

        src = self._get_file_doc(self.attachment_file)
        try:
            content = storage.read_frappe_file(src)
        except FileNotFoundError:
            frappe.throw(_("Attached file is missing on disk: {0}").format(self.attachment_file))

        # Promote the upload to this document's OWN pristine template, kept in
        # the DMS storage rather than as a File row: an attachment can be
        # downloaded by everyone who can read the document, the stored source
        # only under document_file()'s rules. Copying also decouples the
        # document from Frappe's upload deduplication, which can point several
        # uploads at one physical file. The stored source is IMMUTABLE — the
        # render writes its clean deliverable to a separate file so the
        # pristine tagged template always remains available as the render
        # source.
        self.attachment_file = storage.put(self.name, self._source_filename(), content)
        self.file_integrity_hash = hashlib.sha256(content).hexdigest()
        for ext in ALLOWED_EXTENSIONS:  # a re-upload in another format
            if f"{self.name}{ext}" != self._source_filename():
                storage.delete(self.name, f"{self.name}{ext}")

        self._purge_superseded_attachments(previous_url=previous_url, also_remove=[src.name])
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

    def _base_pdf_filename(self):
        """Filename of the signed base PDF that every download is watermarked
        from (download_watermarked_pdf)."""
        return f"{self.name}.pdf"

    def _deliverable_filename(self):
        """Filename of the CLEAN rendered deliverable (tags populated, no images)
        stored as a separate file so the pristine source survives. Downloaded by
        managers via download_word_document; regenerated on every render."""
        return f"{self.name}-controlled{self._controlled_ext()}"

    def _purge_superseded_attachments(self, previous_url, also_remove=None):
        """Delete File rows left behind by an attachment change — the original
        upload, a previous controlled file of a release before 2.8.0, and any
        other source-format file (.docx/.xlsx/.vsdx) attached to this document.
        The promoted source lives in the DMS storage, not in a File row, so
        nothing here is still needed. Other attachments (a scanned PDF) are
        left alone. Frappe guards the physical delete when another File row
        references the same path, so a shared (deduplicated) upload never
        removes another document's bytes."""
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
        font = self._render_font_policy()

        with tempfile.TemporaryDirectory() as tmpdir:
            if ext == ".docx":
                clean_bytes, pdf_bytes = self._render_docx(source_path, field_mappings, soffice, tmpdir, font)
            elif ext == ".xlsx":
                clean_bytes, pdf_bytes = self._render_xlsx(source_path, field_mappings, soffice, tmpdir, font)
            else:  # .vsdx
                clean_bytes, pdf_bytes = self._render_vsdx(source_path, field_mappings, soffice, tmpdir, font)

        self._persist_render_artifacts(clean_bytes, pdf_bytes)

    def _pin_render_font(self):
        """Freeze on the document the font policy this version is approved in.

        An approved version is re-rendered later — on retirement, cancellation,
        expiry and regeneration of a missing file — and each re-render rewrites
        the deliverable, its SHA-256 and the base PDF. Reading DMS Settings at
        those points would re-set the approved content in whatever font is
        configured *then*, so the archived record would stop matching what QA
        approved. Settings are therefore read once, here, at approval;
        _render_font_policy() serves every render from the frozen values.

        A configured family that is not installed on this host is not pinned:
        LibreOffice would silently draw a substitute, so the document keeps its
        template's own fonts instead and the miss goes to the Error Log."""
        from dms.dms.doctype.dms_settings.dms_settings import is_font_installed

        policy = _document_font_policy()
        family = policy["family"] if policy else ""
        if family and not is_font_installed(family):
            frappe.log_error(
                f"Document font '{family}' is enforced in DMS Settings but is not "
                f"installed on this host, so {self.name} was approved in its "
                f"template's own fonts. Install the font in every container that "
                f"runs soffice (check with: fc-list : family).",
                "DMS: document font",
            )
            family = ""
        self.db_set(
            {
                "render_font_family": family,
                "render_preserve_symbol_fonts": 1 if family and policy["preserve_symbols"] else 0,
            },
            update_modified=False,
        )

    def _render_font_policy(self):
        """``{"family", "preserve_symbols"}`` for rendering THIS version — the
        values pinned at approval, never the live DMS Settings — or None for the
        template's own fonts (approved with enforcement off, or before 2.7.0;
        2.7.0 approvals are backfilled by patches/v2_7_1/pin_render_font.py).

        Logs, but does not block, when the pinned family is missing on this
        host: refusing would leave an expired document active or a retired one
        unstamped, which is worse than a PDF drawn in a substitute font."""
        family = (self.render_font_family or "").strip()
        if not family:
            return None

        from dms.dms.doctype.dms_settings.dms_settings import is_font_installed

        if not is_font_installed(family):
            frappe.log_error(
                f"{self.name} was approved in '{family}', which is not installed on "
                f"this host: its re-rendered PDF uses a substitute font. Install the "
                f"font in every container that runs soffice.",
                "DMS: document font",
            )
        return {"family": family, "preserve_symbols": bool(cint(self.render_preserve_symbol_fonts))}

    def _persist_render_artifacts(self, clean_bytes, pdf_bytes):
        """Common tail for every format: store the clean deliverable as its OWN
        file (leaving the pristine source template untouched) and replace the
        signed base PDF.

        Critically, this does NOT overwrite ``attachment_file`` / the source
        template — that immutability is what lets cancel, expiry and download-time
        regeneration re-render from a still-tagged template. The clean deliverable
        lives at ``_deliverable_filename()`` and is served by download_word_document.

        Both go to the DMS storage (not File rows), so neither can be fetched
        around the watermark or the managers-only Word download."""
        storage.put(self.name, self._deliverable_filename(), clean_bytes)

        # SHA-256 reflects the deliverable (clean) bytes, not the signed PDF.
        self.db_set(
            "file_integrity_hash",
            hashlib.sha256(clean_bytes).hexdigest(),
            update_modified=False,
        )

        storage.put(self.name, self._base_pdf_filename(), pdf_bytes)

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
        # Its own process group: the soffice launcher forks soffice.bin, and a
        # timeout that killed only the launcher would leave a hung soffice.bin
        # behind on every stuck conversion.
        proc = subprocess.Popen(
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
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        try:
            stdout, stderr = proc.communicate(timeout=180)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.communicate()
            frappe.throw(_("PDF conversion timed out."))
        if proc.returncode != 0:
            frappe.log_error(
                title="GMP Document: PDF conversion failed",
                message=(stderr or b"").decode("utf-8", errors="ignore"),
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
                        (stdout or b"").decode("utf-8", errors="ignore"),
                        (stderr or b"").decode("utf-8", errors="ignore"),
                    )
                    if part
                ),
            )
            frappe.throw(_("PDF was not generated by LibreOffice. Check Error Log for details."))
        with open(generated_pdf, "rb") as fh:
            return fh.read()

    def _signature_paths(self):
        """``{template_tag: absolute_image_path}`` for every signature the
        current state resolves — the single source used by BOTH render paths
        (docxtpl's InlineImage pass and the xlsx/vsdx image overlay), so the
        two can never drift apart. Tags that resolve to nothing are omitted,
        which is what makes an unsigned stage render as empty space.

        Priority per stage:
          1. the person the stage was performed ON BEHALF OF — a delegated
             approval prints the assigned approver's signature block, with the
             delegation disclosed by the *_on_behalf_of_name tags beside it;
          2. the actual actor;
          3. the assigned role-holder — a step performed by someone else may
             leave the actor without a signature, and without this fallback
             nothing would render at all.
        Supervisor and CEO prefer the snapshot frozen on the document when the
        stage completed; their live lookup only serves documents approved
        before those fields existed."""
        paths = {
            "preparer_signature": _resolve_signature_path(self.prepared_by),
            "supervisor_signature": (
                _signature_path_from_url(self.supervisor_signature)
                or _resolve_signature_path(
                    self.supervisor_on_behalf_of or self.supervisor_approved_by or self.supervisor
                )
            ),
            "reviewer_signature": (
                _resolve_signature_path(self.reviewer_on_behalf_of)
                or _resolve_signature_path(self.reviewed_by)
                or _resolve_signature_path(self.reviewer)
            ),
            "qa_signature": (
                _resolve_signature_path(self.qa_approver_on_behalf_of)
                or _resolve_signature_path(self.approved_by)
                or _resolve_signature_path(self.qa_approver)
            ),
            "ceo_signature": (
                _signature_path_from_url(self.ceo_signature) or _resolve_signature_path(self.ceo)
            ),
        }
        return {tag: path for tag, path in paths.items() if path}

    def _resolve_render_images(self, field_mappings=None):
        """Map of ``{tag: absolute_png_path}`` for the image placeholders, from
        current state — the QA status stamp plus every resolved signature.
        User-defined aliases (custom_tag -> an image system_field) are
        propagated so an aliased image tag resolves in xlsx/vsdx too."""
        images = {}
        stamp = self._resolve_stamp_path()
        if stamp:
            images["qa_stamp"] = stamp
        images.update(self._signature_paths())
        for row in (field_mappings or []):
            alias = (row.custom_tag or "").strip()
            if alias and row.system_field in images:
                images[alias] = images[row.system_field]
        return images

    def _enforce_font_on(self, path, font):
        """Rewrite every font reference in a rendered .docx/.vsdx to the family
        pinned on this version (``font``, from _render_font_policy), so the
        output is standardised regardless of what the template author chose.

        Runs after the template render and before the PDF conversion, so both
        the distributed source file and the PDF come out in the same font.
        Never fatal: a font pass that fails leaves a document in the template's
        own font — the rewrite only replaces the file once it is complete — which
        is strictly better than blocking an approval."""
        if not font:
            return
        try:
            if path.lower().endswith(".docx"):
                enforce_docx_font(path, font["family"], font["preserve_symbols"])
            elif path.lower().endswith(".vsdx"):
                enforce_vsdx_font(path, path, font["family"], font["preserve_symbols"])
        except Exception:
            frappe.log_error(
                frappe.get_traceback(),
                f"GMP Document: font enforcement failed for {os.path.basename(path)}",
            )

    def _render_docx(self, source_path, field_mappings, soffice, tmpdir, font):
        """DOCX: two docxtpl passes (clean deliverable + with-images source)."""
        clean_path = os.path.join(tmpdir, f"{self.name}.docx")
        try:
            clean_template = DocxTemplate(source_path)
            _render_docx_template(
                clean_template, self._build_template_context(field_mappings=field_mappings)
            )
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
            _render_docx_template(sig_template, sig_context)
            sig_template.save(sig_path)
        except Exception:
            frappe.log_error(
                frappe.get_traceback(),
                "GMP Document: Word template render (with signatures) failed",
            )
            frappe.throw(_("Failed to render Word template with signatures. Check Error Log."))

        # Both files: the clean .docx is distributed to authors, so it must
        # carry the standard font too — not just the PDF people read.
        self._enforce_font_on(clean_path, font)
        self._enforce_font_on(sig_path, font)

        pdf_bytes = self._convert_to_pdf(soffice, sig_path, tmpdir)
        with open(clean_path, "rb") as fh:
            return fh.read(), pdf_bytes

    def _render_xlsx(self, source_path, field_mappings, soffice, tmpdir, font):
        """XLSX: openpyxl renders text tags per cell; images are anchored as real
        pictures in the PDF-source pass. Calc rasterises anchored images on PDF
        export, so the stamp/signatures need no post-overlay."""
        from .format_renderers import render_xlsx

        text_context = self._build_template_context(field_mappings=field_mappings)
        clean_path = os.path.join(tmpdir, f"{self.name}.xlsx")
        img_path = os.path.join(tmpdir, f"{self.name}-with-images.xlsx")
        try:
            render_xlsx(source_path, clean_path, text_context, images={}, font=font)
            render_xlsx(
                source_path, img_path, text_context,
                images=self._resolve_render_images(field_mappings),
                font=font,
            )
        except Exception:
            frappe.log_error(frappe.get_traceback(), "GMP Document: Excel render failed")
            frappe.throw(_("Failed to render Excel template. Check Error Log for details."))

        pdf_bytes = self._convert_to_pdf(soffice, img_path, tmpdir)
        with open(clean_path, "rb") as fh:
            return fh.read(), pdf_bytes

    def _render_vsdx(self, source_path, field_mappings, soffice, tmpdir, font):
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

        self._enforce_font_on(clean_path, font)

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
        try:
            source_path = storage.local_path(self.name, self._source_filename())
        except storage.StoredFileMissing:
            if not _adopt_legacy_files(self):
                frappe.throw(
                    _("The source template of {0} is missing: upload it again.").format(self.name)
                )
            source_path = storage.local_path(self.name, self._source_filename())
        return source_path, mappings

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
            "supervisor": self.supervisor or "",
            "supervisor_name": user_full_name(self.supervisor),
            "reviewer": self.reviewer or "",
            "reviewer_name": user_full_name(self.reviewer),
            "qa_supervisor": self.qa_supervisor or "",
            "qa_supervisor_name": user_full_name(self.qa_supervisor),
            "regulatory_manager": self.regulatory_manager or "",
            "regulatory_manager_name": user_full_name(self.regulatory_manager),
            "qa_approver": self.qa_approver or "",
            "qa_approver_name": user_full_name(self.qa_approver),
            "ceo": self.ceo or "",
            "ceo_name": self.ceo_name or "",
            # ----- workflow actuals -----
            "reviewed_by": self.reviewed_by or "",
            "reviewed_by_name": user_full_name(self.reviewed_by),
            "reviewed_on": cstr(self.reviewed_on) if self.reviewed_on else "",
            "approved_by": self.approved_by or "",
            "approved_by_name": user_full_name(self.approved_by),
            "approved_on": cstr(self.approved_on) if self.approved_on else "",
            "workflow_status": self.workflow_status or "",
            # ----- approval timeline -----
            # One Date per stage plus current_date, which is resolved at render
            # time rather than stored: it is the date the copy in the reader's
            # hand was produced, which is exactly what a "printed on" box means.
            "preparer_date": cstr(self.preparer_date) if self.preparer_date else "",
            "supervisor_date": cstr(self.supervisor_date) if self.supervisor_date else "",
            "reviewer_date": cstr(self.reviewer_date) if self.reviewer_date else "",
            "qa_supervisor_date": cstr(self.qa_supervisor_date) if self.qa_supervisor_date else "",
            "regulatory_date": cstr(self.regulatory_date) if self.regulatory_date else "",
            "manager_date": cstr(self.manager_date) if self.manager_date else "",
            "qa_approver_date": cstr(self.qa_approver_date) if self.qa_approver_date else "",
            "ceo_date": cstr(self.ceo_date) if self.ceo_date else "",
            "publish_date": cstr(self.publish_date) if self.publish_date else "",
            "current_date": today(),
            # ----- delegated approvals -----
            # Empty unless a proxy approver acted, so a template can print
            # "on behalf of {{ ..._on_behalf_of_name }}" under the signature and
            # simply collapse to nothing on a normally-approved document.
            "supervisor_on_behalf_of_name": user_full_name(self.supervisor_on_behalf_of),
            "reviewer_on_behalf_of_name": user_full_name(self.reviewer_on_behalf_of),
            "qa_supervisor_on_behalf_of_name": user_full_name(self.qa_supervisor_on_behalf_of),
            "regulatory_on_behalf_of_name": user_full_name(self.regulatory_on_behalf_of),
            "manager_on_behalf_of_name": user_full_name(self.manager_on_behalf_of),
            "qa_approver_on_behalf_of_name": user_full_name(self.qa_approver_on_behalf_of),
            # ----- signatures (default: empty for clean DOCX) -----
            "preparer_signature": "",
            "supervisor_signature": "",
            "reviewer_signature": "",
            "qa_signature": "",
            "ceo_signature": "",
            # ----- QA status stamp (default: empty for clean DOCX) -----
            "qa_stamp": "",
        }

        if template_for_images is not None:
            for key, path in self._signature_paths().items():
                context[key] = InlineImage(
                    template_for_images, path, width=Mm(SIGNATURE_WIDTH_MM)
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

        # A return's reason stays on the form for whoever has to address it,
        # and goes once the document moves on — so a later return recorded
        # without one can never be logged under the earlier actor's words.
        if (prev, curr) not in _RETURN_EDGES and self.last_revision_request:
            self.db_set("last_revision_request", None, update_modified=False)

        # ---------------- forward path ---------------- #
        if curr == WF_PENDING_SUPERVISOR and prev in (WF_DRAFT, WF_REVISION):
            # Preparer submitted for approval; actors were resolved in
            # validate() during this same save.
            self._stamp_stage("preparer", actor)
            self.add_comment(
                "Workflow",
                _("Submitted for approval by {0} — routed to supervisor {1}").format(actor, self.supervisor),
            )
            _close_open_todos(self, allocated_to=self.prepared_by)
            _create_todo(self, self.supervisor, _("Supervisor approval — GMP Document {0}").format(self.name))

        elif curr == WF_UNDER_REVIEW and prev == WF_PENDING_SUPERVISOR:
            self._stamp_stage("supervisor", actor)
            self.add_comment("Workflow", _("Supervisor approved — forwarded to reviewer {0}").format(self.reviewer))
            _close_open_todos(self, allocated_to=self.supervisor)
            _create_todo(self, self.reviewer, _("Review GMP Document {0}").format(self.name))

        elif curr == WF_PENDING_QA_SUPERVISOR and prev == WF_UNDER_REVIEW:
            self._stamp_stage("reviewer", actor)
            self.add_comment("Workflow", _("Reviewer approved — forwarded to QA Supervisor {0}").format(self.qa_supervisor))
            _close_open_todos(self, allocated_to=self.reviewer)
            _create_todo(self, self.qa_supervisor, _("QA Supervisor — approve or delegate review of {0}").format(self.name))

        elif curr == WF_PENDING_REGULATORY and prev == WF_PENDING_QA_SUPERVISOR:
            # QA Supervisor approved directly, without delegating a queue (or
            # past a halted one — retire any rows still queued from it).
            self._stamp_stage("qa_supervisor", actor)
            self._supersede_open_qa_rows()
            self.add_comment("Workflow", _("QA Supervisor approved — forwarded to Regulatory {0}").format(self.regulatory_manager))
            _close_open_todos(self, allocated_to=self.qa_supervisor)
            _create_todo(self, self.regulatory_manager, _("Regulatory validation — GMP Document {0}").format(self.name))

        elif curr == WF_PENDING_MANAGER and prev == WF_PENDING_REGULATORY:
            self._stamp_stage("regulatory", actor)
            self.add_comment("Workflow", _("Regulatory validated — forwarded to Manager {0}").format(self.reviewer))
            _close_open_todos(self, allocated_to=self.regulatory_manager)
            _create_todo(self, self.reviewer, _("Manager approval — GMP Document {0}").format(self.name))

        elif curr == WF_PENDING_FINAL_QA and prev == WF_PENDING_MANAGER:
            self._stamp_stage("manager", actor)
            self.add_comment("Workflow", _("Manager approved — forwarded to QA Approver {0}").format(self.qa_approver))
            _close_open_todos(self, allocated_to=self.reviewer)
            _create_todo(self, self.qa_approver, _("Final QA authorization — publish GMP Document {0}").format(self.name))

        elif curr == WF_APPROVED:
            # Final QA published (this save also auto-submits the document).
            self._stamp_stage("qa_approver", actor)
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

    def _stamp_stage(self, stage, actor):
        """Record the completion of one approval stage: who performed it, the
        precise timestamp, the report-friendly Date mirror, and — when the
        actor stood in for the assigned role-holder — the delegation.

        `<stage>_approved_by` always names the user who ACTUALLY acted, which
        is what 21 CFR Part 11 requires of an electronic signature record;
        `<stage>_on_behalf_of` names the role-holder they acted for. Only the
        signature follows the represented person, so the printed signature
        block reads as the assigned approver's — with the delegation stated on
        the document beside it, never in place of it."""
        spec = _STAGE_STAMPS[stage]
        stamped_at = now_datetime()
        if spec["by"]:
            self.db_set(spec["by"], actor, update_modified=False)
        self.db_set(spec["on"], stamped_at, update_modified=False)
        self.db_set(spec["date"], getdate(stamped_at), update_modified=False)

        signer = actor
        assigned = self.get(spec["actor"])
        if assigned and assigned != actor and _acts_on_behalf(actor):
            signer = assigned
            if spec["on_behalf"]:
                self.db_set(spec["on_behalf"], assigned, update_modified=False)
            self.add_comment(
                "Workflow",
                _("{0} acted as {1} on behalf of {2}.").format(
                    actor, _(spec["label"]), assigned
                ),
            )

        if spec["signature"]:
            self._snapshot_signature(spec["signature"], signer)

    def _snapshot_signature(self, fieldname, user):
        """Freeze the signature image applied at a stage onto the document, by
        COPYING it into a file this document owns (in the DMS storage).

        It must be a copy, never a reference to the Employee's own File.
        Frappe's global `attach_files_to_document` hook claims any *unattached*
        File named by an Attach Image field, so pointing these fields at the
        employee's signature re-parented it here, and deleting this document
        then deleted that employee's signature for every future document too.

        Copying is also the truthful thing for a controlled record: the bytes
        that were actually applied at this stage stay with the document even
        after the employee replaces their signature.

        A user without a usable signature leaves the field empty — the same
        graceful degradation the PDF render has always had."""
        signature = _resolve_signature_file(user)
        if not signature:
            return

        try:
            content = storage.read_frappe_file(signature.file)
        except Exception:
            frappe.log_error(
                f"Could not read signature {signature.url} for {user}\n{frappe.get_traceback()}",
                "GMP Document: signature snapshot failed",
            )
            return

        ext = os.path.splitext(signature.file.file_name or "")[1].lower() or ".png"
        url = storage.put(self.name, f"{self.name}-{fieldname}{ext}", content)
        self.db_set(fieldname, url, update_modified=False)

    def _stamp_ceo_authorization(self):
        """Stamp the CEO authorization block at publication.

        The CEO is resolved from DMS Settings (per-department override, else
        the global default) and is entirely optional — a site whose documents
        carry no CEO sign-off simply leaves it unset and the block stays empty
        and hidden. The name is captured as text alongside the User link so the
        controlled record keeps the name that was in force at publication even
        if the user is later renamed, replaced or disabled."""
        if self.ceo:
            return  # already stamped (re-render / re-submit)

        from dms.dms.doctype.dms_settings.dms_settings import resolve_department_actors

        ceo = resolve_department_actors(self.department).get("ceo")
        if not ceo:
            return

        self.db_set("ceo", ceo, update_modified=False)
        self.db_set(
            "ceo_name",
            frappe.db.get_value("User", ceo, "full_name") or ceo,
            update_modified=False,
        )
        self.db_set("ceo_date", getdate(now_datetime()), update_modified=False)
        self._snapshot_signature("ceo_signature", ceo)

    def _stamp_revision_request(self, actor):
        """Record who/when on a revision request. The reason is stored by
        set_return_reason() — the form prompts for it when a Return action is
        chosen — before apply_workflow reloads the document from the database."""
        self.db_set("last_revision_by", actor, update_modified=False)
        self.db_set("last_revision_on", now_datetime(), update_modified=False)
        reason = (self.last_revision_request or "").strip()
        self.add_comment(
            "Workflow",
            _("Revision requested by {0}: {1}").format(actor, reason or _("(no reason given)")),
        )

    # ------------------------------------------------------------------ #
    #  Who the document waits on, and the progress bar                    #
    # ------------------------------------------------------------------ #

    def _pending_actor(self):
        """The user the document is waiting on right now, or None once it has
        left the approval chain (approved, obsolete, cancelled revision). In
        the delegated QA review it is the reviewer at the head of the queue."""
        if self.docstatus != 0:
            return None
        if self.workflow_status == WF_QA_IN_PROGRESS:
            head = self._qa_queue_head()
            if head:
                return head.reviewer
        stage = _STAGE_OF_STATE.get(self.workflow_status)
        return self.get(_STAGE_STAMPS[stage]["actor"]) if stage else None

    def _refresh_pending(self, moved=False):
        """Keep pending_with / pending_since in step with the workflow, for the
        list's "Waiting On" column and filter. ``moved`` means the document
        just changed hands, which restarts the clock even when the same person
        holds the next stage (the reviewer acts again as Manager)."""
        who = self._pending_actor()
        if who == self.pending_with and not moved:
            return
        self.db_set(
            {
                "pending_with": who,
                "pending_with_name": _full_names([who]).get(who) if who else None,
                "pending_since": now_datetime() if who else None,
            },
            update_modified=False,
        )

    def _approval_progress(self):
        """The approval chain as the form's progress bar draws it: every stage
        with its person and status (done / current / upcoming / skipped), the
        QA review queue, the latest return and how the chain ended."""
        state = self.workflow_status
        if self.docstatus == 2 or state == WF_OBSOLETE:
            end = "obsolete"
        elif state == WF_REVISION_CANCELLED:
            end = "cancelled"
        elif self.docstatus == 1:
            end = "pending_effective" if _is_pending_effective(self) else "approved"
        else:
            end = None
        current = _STAGE_OF_STATE.get(state) if self.docstatus == 0 else None
        stage_keys = [stage for stage, _states in _PROGRESS_STAGES]
        current_index = stage_keys.index(current) if current else None

        steps = []
        for index, stage in enumerate(stage_keys):
            spec = _STAGE_STAMPS[stage]
            assigned = self.get(spec["actor"])
            at = self.get(spec["on"])
            if self.docstatus == 1 or end == "obsolete":
                status = "done"
            elif current_index is None:
                # A cancelled revision: only the stages it actually reached.
                status = "done" if at else "skipped"
            elif index < current_index:
                status = "done"
            else:
                status = "current" if index == current_index else "upcoming"
            done = status == "done"
            acted_by = (self.get(spec["by"]) if spec["by"] else assigned) if done and at else None
            steps.append(
                {
                    "stage": stage,
                    "label": _(spec["label"]),
                    "status": status,
                    "user": acted_by or assigned,
                    "on_behalf_of": (self.get(spec["on_behalf"]) if spec["on_behalf"] else None) if done else None,
                    # A stamp left from an earlier pass (before a return) is
                    # not this pass's completion.
                    "at": at if done else None,
                }
            )

        queue = [
            {"user": row.reviewer, "status": row.status, "at": row.completed_on, "comments": row.comments}
            for row in self._current_round_rows()
            if row.status != QA_SUPERSEDED
        ]
        returned = None
        if self.last_revision_request and self.docstatus == 0:
            returned = {
                "user": self.last_revision_by,
                "at": self.last_revision_on,
                "reason": self.last_revision_request,
            }

        names = _full_names(
            [s["user"] for s in steps]
            + [s["on_behalf_of"] for s in steps]
            + [q["user"] for q in queue]
            + [self.pending_with, returned and returned["user"]]
        )
        for item in steps + queue + ([returned] if returned else []):
            item["user_name"] = names.get(item["user"]) if item["user"] else None
        for step in steps:
            step["on_behalf_of_name"] = names.get(step["on_behalf_of"]) if step["on_behalf_of"] else None

        return {
            "state": state,
            "steps": steps,
            "qa_queue": queue,
            "returned": returned,
            "pending": {
                "user": self.pending_with,
                "user_name": names.get(self.pending_with),
                "since": self.pending_since,
            }
            if self.pending_with and self.docstatus == 0
            else None,
            "end": end,
            "effective_date": self.effective_date,
        }

    # ------------------------------------------------------------------ #
    #  Sequential QA review queue engine                                 #
    # ------------------------------------------------------------------ #
    # The queue lives in the qa_reviews child table; row order within the
    # highest `round` IS the review order. The engine (not manual workflow
    # actions) moves the document out of 'QA Review In Progress': forward to
    # 'Pending Regulatory Validation' once every row of the round is closed
    # with at least one actual approval, or back to 'Pending QA Supervisor' when
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
        self._refresh_pending(moved=True)

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
                self._refresh_pending(moved=True)
                return row
        return None

    def _finish_qa_queue(self):
        """The current round has no Queued/Awaiting rows left. Advance to
        Regulatory if at least one reviewer actually approved; a round that
        ended with zero approvals (everyone skipped) goes back to the QA
        Supervisor — a skipped-out queue must never count as a completed
        review."""
        rows = self._current_round_rows()
        approvals = [r for r in rows if r.status == QA_APPROVED]
        if approvals:
            self.db_set("qa_review_complete", 1, update_modified=False)
            # QA clearance came from the delegated queue rather than a single
            # click, so the stage is stamped to the QA Supervisor who owns the
            # delegation — not to the session user, who is merely the last
            # reviewer in the queue. The individual reviewers and their verdicts
            # stay on the qa_reviews rows.
            self._stamp_stage("qa_supervisor", self.qa_supervisor)
            self._server_transition(
                WF_PENDING_REGULATORY,
                _("All delegated QA reviews completed ({0} approved, {1} skipped) — forwarded to Regulatory {2}").format(
                    len(approvals),
                    len([r for r in rows if r.status == QA_SKIPPED]),
                    self.regulatory_manager,
                ),
            )
            _create_todo(self, self.regulatory_manager, _("Regulatory validation — GMP Document {0}").format(self.name))
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
        controlled version (Issue #4).

        Only live references move. A "Supersedes" or "Derived From" row names
        one particular version on purpose, and a retired document (obsolete,
        cancelled, an abandoned revision) is a record of what it cited when it
        was in force — rewriting either would falsify history."""
        predecessor_name = self.revision_of or self.amended_from
        if not predecessor_name:
            return

        rows = [
            r
            for r in frappe.get_all(
                "GMP Document Reference",
                filters={"referenced_document": predecessor_name},
                fields=["name", "parent", "parenttype", "reference_type"],
            )
            if r.reference_type not in _VERSION_PINNED_REFERENCE_TYPES
        ]
        retired = set(
            frappe.get_all(
                "GMP Document",
                filters={
                    "name": ["in", list({r.parent for r in rows if r.parenttype == "GMP Document"}) or [""]],
                },
                or_filters={
                    "docstatus": 2,
                    "workflow_status": ["in", (WF_OBSOLETE, WF_REVISION_CANCELLED)],
                },
                pluck="name",
            )
        )
        repointed_parents = set()
        for row in rows:
            if row.parent == self.name:
                continue  # never repoint this revision's own reference rows
            if row.parent in retired:
                continue
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


def _render_docx_template(template, context):
    """Render an uploaded .docx template.

    The file IS the Jinja template, and its author is the preparer. Sandboxed,
    so a tag hidden in it (white text, a field code) cannot reach Python
    internals — ``{{ cycler.__init__.__globals__.os... }}`` would otherwise run
    on the server at every approval and re-render. Autoescaped, so a value
    such as "Research & Development" or "pH < 7" lands as text instead of
    breaking the XML (silently dropped in the body, a failed approval in a
    header). docxtpl's InlineImage/RichText mark themselves safe."""
    template.render(context, jinja_env=SandboxedEnvironment(), autoescape=True)


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
    custom_signature_image -> a PNG/JPG/JPEG File, on disk or in the S3 bucket)
    but returns a message instead of logging, so it can drive save-time
    validation and the client-side pre-check. Used by
    GMPDocument._validate_signatures and check_signature."""
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

    file_doc = frappe.get_doc("File", file_name)
    if not (file_doc.file_name or file_doc.file_url or "").lower().endswith(
        ALLOWED_SIGNATURE_EXTENSIONS
    ):
        return _("the signature must be a PNG, JPG or JPEG image")
    # A file in the bucket is not fetched on every save; a local one is checked.
    if not storage.is_s3_file(file_doc) and not os.path.exists(file_doc.get_full_path()):
        return _("the signature image is missing on disk")
    return None


def _resolve_signature_path(user_email):
    """A local path holding a user's signature image, or None.

    Thin wrapper over _resolve_signature_file for the render paths, which only
    ever need a path: the file itself, or a cached copy of one in the bucket."""
    signature = _resolve_signature_file(user_email)
    if not signature:
        return None
    try:
        return storage.frappe_file_path(signature.file)
    except Exception:
        frappe.log_error(
            f"Could not read signature {signature.url} for {user_email}\n{frappe.get_traceback()}",
            "GMP Document signature lookup",
        )
        return None


def _signature_path_from_url(file_url):
    """The on-disk path behind a stored signature URL, or None.

    Used for the snapshotted `*_signature` fields: they hold the URL of the
    image that was in force when a stage was completed, and the render needs
    the file it points at. Returns None (rather than throwing) when the File
    row or the image itself has since gone, so the render degrades the same way
    a never-uploaded signature does. A snapshot whose bytes no longer match
    their checksum is refused (StoredFileIntegrityError), never rendered."""
    stored = storage.parse_file_url(file_url)
    if not stored or not stored[1].lower().endswith(ALLOWED_SIGNATURE_EXTENSIONS):
        return None
    try:
        return storage.local_path(*stored)
    except storage.StoredFileMissing:
        return None


def _resolve_signature_file(user_email):
    """Return frappe._dict(url=..., file=<File>) for a user's signature, or
    None.

    Resolution chain:
        User (user_email) -> Employee.user_id -> Employee.custom_signature_image
        -> File.file_url -> the File (on the local disk or in the S3 bucket)

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

    file_doc = frappe.get_doc("File", file_name)
    if not (file_doc.file_name or file_doc.file_url or "").lower().endswith(
        ALLOWED_SIGNATURE_EXTENSIONS
    ):
        frappe.log_error(
            title=log_title,
            message=(
                f"Employee {employee_name} signature must be one of "
                f"{', '.join(ALLOWED_SIGNATURE_EXTENSIONS)} — got '{file_doc.file_name}'."
            ),
        )
        return None

    if not storage.is_s3_file(file_doc) and not os.path.exists(file_doc.get_full_path()):
        frappe.log_error(
            title=log_title,
            message=(
                f"Employee {employee_name} signature missing on disk: {file_doc.get_full_path()}"
            ),
        )
        return None

    return frappe._dict(url=sig_url, file=file_doc)


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
            # on_submit left publish_date blank for a future-dated document
            # precisely so it could be stamped here: today is the day it
            # actually entered force.
            doc.db_set("publish_date", today(), update_modified=False)
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
            continue
        # The approval render ran before publish_date existed; re-render so a
        # template's {{ publish_date }} is filled in the copy that is now in
        # force. After the commit, so a render failure cannot undo activation.
        _rerender_after_status_change(doc, "effective-date activation re-render")


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
            frappe.db.commit()
        except Exception:
            frappe.db.rollback()
            frappe.log_error(
                frappe.get_traceback(),
                f"GMP Document: auto-expire failed for {name}",
            )
            continue
        # Separately from the expiry itself: a render failure (LibreOffice
        # down, source file gone) must not roll the document back to active
        # and keep it in circulation. Downloads are OBSOLETE-watermarked from
        # the status regardless; only the base PDF's stamp waits for a re-run.
        _rerender_after_status_change(doc, "auto-expire re-stamp")


def _rerender_after_status_change(doc, what):
    """Re-render after a committed status change; failures are logged, and
    their partial writes discarded, without touching the status change."""
    if not doc.attachment_file:
        return
    try:
        doc._render_and_generate_pdf()
        frappe.db.commit()
    except Exception:
        frappe.db.rollback()
        frappe.log_error(frappe.get_traceback(), f"GMP Document: {what} failed for {doc.name}")


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

    # The caller becomes the draft's preparer, so they must be able to take
    # it forward: a revision started by someone the workflow does not let
    # submit a draft would sit unsubmittable — and, as the one open revision,
    # block every other revision of the document.
    if not _can_author(frappe.session.user):
        frappe.throw(
            _(
                "You would be the preparer of this revision, but your roles cannot "
                "submit a draft in the GMP Document workflow (usually the DMS "
                "Initiator role). Ask a document author to start the revision."
            ),
            frappe.PermissionError,
        )

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


@frappe.whitelist(methods=["POST"])
def set_return_reason(docname, reason):
    """Store the reason for a Return action the caller is about to apply.

    The form calls this from before_workflow_action. It has to be its own
    request: apply_workflow reloads the document from the database, so a
    reason typed into the form beforehand would never reach the save."""
    doc = frappe.get_doc("GMP Document", docname)
    doc.check_permission("read")

    reason = cstr(reason).strip()
    if not reason:
        frappe.throw(_("Enter the reason for returning the document."))
    if doc.docstatus != 0:
        frappe.throw(_("{0} is no longer in review.").format(docname))

    user = frappe.session.user
    actor_field = _STATE_WRITE_ACTOR.get(doc.workflow_status)
    if not (_is_unrestricted(user) or (actor_field and doc.get(actor_field) == user)):
        frappe.throw(
            _("Only the approver {0} is waiting on can return it.").format(docname),
            frappe.PermissionError,
        )

    doc.db_set("last_revision_request", reason, update_modified=False)


def _can_author(user):
    """True when the workflow lets ``user`` act on a draft (a transition out
    of Draft is given to one of their roles). Read from the site's workflow —
    which the site owns — rather than from a fixed role name."""
    if user == "Administrator":
        return True
    from frappe.model.workflow import get_workflow_name

    workflow = get_workflow_name("GMP Document")
    if not workflow:
        return True  # no workflow: role permissions alone decide
    return bool(
        frappe.get_all(
            "Workflow Transition",
            filters={
                "parent": workflow,
                "state": WF_DRAFT,
                "allowed": ["in", frappe.get_roles(user)],
            },
            limit=1,
        )
    )


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

    # A cancelled document (docstatus 2) stays downloadable — always stamped
    # OBSOLETE below — so its retired copy can still be produced for audit.
    if doc.docstatus == 0:
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
    elif not doc.is_active or doc.docstatus == 2:
        watermark_text = "OBSOLETE"

    # 21 CFR Part 11: a reference print outside change control is only
    # meaningful with a print timestamp. GMP sites in Iran expect the Jalali
    # calendar.
    #
    # Left untranslated by default. It is no longer *impossible* to translate —
    # with a font configured in DMS Settings the overlay embeds a real TTF and
    # shape_rtl() supplies the joining and bidi reportlab lacks, so a Persian
    # footer renders correctly. But with no font configured the overlay is still
    # built-in Helvetica (WinAnsi-only), where a Persian string would draw as
    # boxes. Translate these via Translation records once the font is in place
    # and verified on a real download.
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
    """Return a local path holding the document's verified base PDF,
    regenerating it if it is missing. Regeneration requires the source
    template to still be present."""
    return _stored_output_path(
        doc,
        doc._base_pdf_filename(),
        unavailable=_(
            "The controlled PDF for {0} is temporarily unavailable. "
            "Please ask a DMS/QA manager to regenerate it."
        ),
        no_source=_(
            "Base PDF for {0} is unavailable and cannot be regenerated: "
            "the controlled .docx attachment is missing."
        ),
        failed=_("Base PDF for {0} could not be generated."),
    )


def _keep_regeneration():
    """Downloads arrive as GET, and Frappe rolls back every GET request. A
    regeneration's stored files and integrity hash would be discarded, and the
    next download would regenerate all over again."""
    frappe.local.flags.commit = True


def _resolve_deliverable_path(doc):
    """Return a local path holding the clean rendered deliverable (the
    downloadable Word/Excel/Visio file), regenerating it from the pristine
    template if it is missing."""
    return _stored_output_path(
        doc,
        doc._deliverable_filename(),
        unavailable=_(
            "The controlled document file for {0} is temporarily unavailable. "
            "Please ask a DMS/QA manager to regenerate it."
        ),
        no_source=_(
            "The deliverable for {0} is unavailable and cannot be regenerated: "
            "the source template is missing."
        ),
        failed=_("The deliverable for {0} could not be generated."),
    )


def _stored_output_path(doc, file_name, unavailable, no_source, failed):
    """A render output's local path; adopts files of a release before 2.8.0,
    then regenerates a missing output from the source template.

    Regeneration mutates the document (db_set of the integrity hash) and
    rewrites stored files, so it must not be reachable by a read-only member
    downloading a controlled copy: it is restricted to the manager/admin
    roles. A file that fails its integrity check is never regenerated over —
    the mismatch is the evidence, and is reported instead."""
    try:
        return storage.local_path(doc.name, file_name)
    except storage.StoredFileMissing:
        pass

    if _adopt_legacy_files(doc):
        _keep_regeneration()
        try:
            return storage.local_path(doc.name, file_name)
        except storage.StoredFileMissing:
            pass

    if not _is_unrestricted(frappe.session.user):
        frappe.throw(unavailable.format(doc.name))
    if not doc.attachment_file:
        frappe.throw(no_source.format(doc.name))
    doc._render_and_generate_pdf()
    _keep_regeneration()

    try:
        return storage.local_path(doc.name, file_name)
    except storage.StoredFileMissing:
        frappe.throw(failed.format(doc.name))


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
#   Pending QA Supervisor ── Approve (QA Supervisor) ──► Pending Regulatory Validation
#   Pending QA Supervisor ── delegate_qa_review() ──► QA Review In Progress
#   QA Review In Progress ── queue engine (all reviews done) ──► Pending Regulatory Validation
#   Pending Regulatory Validation ── Validate (Regulatory) ──► Pending Manager Approval
#   Pending Manager Approval ── Approve (Manager) ──► Pending Final QA Approval
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
    (counted only when the user is the row currently awaiting review).

    Someone else's workload is visible only to the unrestricted roles."""
    if user and user != frappe.session.user and not _is_unrestricted(frappe.session.user):
        frappe.throw(_("You can only see your own pending documents."), frappe.PermissionError)
    user = user or frappe.session.user
    stage_actor = (
        ("supervisor_approval", WF_PENDING_SUPERVISOR, "supervisor"),
        ("to_review", WF_UNDER_REVIEW, "reviewer"),
        ("qa_supervisor", WF_PENDING_QA_SUPERVISOR, "qa_supervisor"),
        ("regulatory", WF_PENDING_REGULATORY, "regulatory_manager"),
        ("manager_approval", WF_PENDING_MANAGER, "reviewer"),
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


@frappe.whitelist()
def get_approval_progress(docname):
    """The approval chain of one document — who did what and when, and who it
    is waiting on now — for the progress bar at the top of its form."""
    doc = frappe.get_doc("GMP Document", docname)
    doc.check_permission("read")
    return doc._approval_progress()


def _full_names(users):
    """``{user: full name}`` for the given users, in one query."""
    users = list({u for u in users if u})
    if not users:
        return {}
    return {
        row.name: row.full_name or row.name
        for row in frappe.get_all("User", filters={"name": ["in", users]}, fields=["name", "full_name"])
    }


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
    (Employee.user_id == user). A user with no Employee link has none. Only
    Active records count: after a transfer or rehire the old record (Left) may
    still carry the user id, and must not keep the old department's documents
    visible.

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
                "Employee", filters={"user_id": user, "status": "Active"}, pluck="department"
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


def _may_write(doc, user):
    """Write gate for a named participant of a saved draft.

    Judged on the STORED row, never the in-memory document: apply_workflow
    sets the next state before it saves, and Frappe checks write permission
    inside that save — so the in-memory state names the NEXT stage's actor,
    and a plain REST save carries whatever actor fields the client sent.

    A save that changes the state is a workflow transition; whether this user
    may make it is decided by the Workflow engine (validate_workflow checks
    the transition's role and condition), so it is allowed here. That keeps
    actions offered to someone other than the waiting actor — the preparer's
    Cancel Revision, or a transition a site adds to its own workflow —
    working. Any other save is an edit, and only the waiting actor may edit."""
    stored = frappe.db.get_value(
        doc.doctype, doc.name, ["workflow_status", *_PARTICIPANT_FIELDS], as_dict=True
    )
    if not stored:
        return False
    if doc.get("workflow_status") != stored.workflow_status:
        return user in tuple(stored.get(f) for f in _PARTICIPANT_FIELDS)
    actor_field = _STATE_WRITE_ACTOR.get(stored.workflow_status)
    return bool(actor_field and stored.get(actor_field) == user)


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
    (and print) the document; on a draft, write is granted to the participant
    the stored state is waiting on, and to a participant making a workflow
    transition (see _may_write). A plain member may read an approved, active
    document of their department.
    Everything else is denied here (role perms already withhold
    create/delete/etc. — this is defence in depth)."""
    if doc is None:
        return None  # doctype-level check; leave to role perms + query conditions

    user = user or frappe.session.user
    if _is_unrestricted(user):
        return True

    if getattr(doc, "is_new", None) and doc.is_new():
        # Creation is governed by role perms (DMS Initiator). True, not None:
        # Frappe reads any falsy hook result as a denial, and a hook can only
        # deny — it never grants a permission the role does not have.
        return True

    read_like = ptype in ("read", "print")
    named = user in tuple(doc.get(f) for f in _PARTICIPANT_FIELDS)
    if named:
        if read_like:
            return True
        if ptype != "write" or cint(doc.get("docstatus")) != 0:
            return False
        return _may_write(doc, user)

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
            fields=["name", "version_number", "document_name_en", "is_active", "docstatus", "workflow_status"],
        )

        def rank(d):
            # The version in force wins; failing that, the newest. Picking the
            # highest version alone would show an approved revision that waits
            # for its effective date — and hide the version actually in force.
            return (cint(d.is_active), int(d.version_number or 0))

        latest = {}
        for d in docs:
            # Logical base = the ID without the trailing version segment
            # ([Dept]-[Type]-[Number]), so every version collapses to one node.
            base = d.name.rsplit("-", 1)[0]
            if base not in latest or rank(d) > rank(latest[base]):
                latest[base] = d

        nodes = []
        for d in sorted(latest.values(), key=lambda x: x.name):
            title = d.name
            if d.document_name_en:
                title += f"  —  {d.document_name_en}"
            if d.is_active:
                indicator, color = "ACTIVE", "green"
            elif _is_pending_effective(d):
                indicator, color = "NOT YET EFFECTIVE", "orange"
            else:
                indicator, color = "OBSOLETE", "red"
            nodes.append({
                "value": d.name,
                "title": title,
                "expandable": 0,
                "indicator": indicator,
                "indicator_color": color,
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


# The Attach Image fields that hold a stage's frozen signature (see
# GMPDocument._snapshot_signature).
_SIGNATURE_SNAPSHOT_FIELDS = tuple(
    spec["signature"] for spec in _STAGE_STAMPS.values() if spec["signature"]
) + ("ceo_signature",)


@frappe.whitelist()
def document_file(docname, file):
    """Serve a file the DMS stores for a GMP Document — the URL its source and
    signature fields hold — under the DMS rules, not Frappe's attachment rule
    (which lets everyone who can read a document download all its files):

    - a signature image: anyone who can read the document (the form shows it);
    - the source template: managers, and the people named on the document,
      who work on it — not every department reader;
    - the clean deliverable: managers, as download_word_document;
    - the signed base PDF: nobody — it leaves only through
      download_watermarked_pdf, with its watermark."""
    doc = frappe.get_doc("GMP Document", docname)
    doc.check_permission("read")

    user = frappe.session.user
    unrestricted = _is_unrestricted(user)
    signatures = {
        f"{docname}-{field}{ext}"
        for field in _SIGNATURE_SNAPSHOT_FIELDS
        for ext in ALLOWED_SIGNATURE_EXTENSIONS
    }
    if file == doc._base_pdf_filename():
        allowed = False
    elif file in {f"{docname}-controlled{ext}" for ext in ALLOWED_EXTENSIONS}:
        allowed = unrestricted
    elif file in {f"{docname}{ext}" for ext in ALLOWED_EXTENSIONS}:
        allowed = unrestricted or user in _participants(doc) or _in_qa_queue(doc, user)
    elif file in signatures:
        allowed = True
    else:
        allowed = unrestricted
    if not allowed:
        frappe.throw(
            _("You are not permitted to download this file of {0}.").format(docname),
            frappe.PermissionError,
        )

    try:
        content = storage.read(docname, file)
    except storage.StoredFileMissing:
        frappe.throw(_("{0} is not available.").format(file), frappe.DoesNotExistError)

    frappe.local.response.filename = file
    frappe.local.response.filecontent = content
    frappe.local.response.type = "download"
    if file in signatures:
        frappe.local.response.display_content_as = "inline"


def _participants(doc):
    return {doc.get(f) for f in _PARTICIPANT_FIELDS if doc.get(f)}


def _adopt_legacy_files(doc):
    """Move the files a GMP Document owned as File rows (releases before
    2.8.0) into the DMS storage, repoint its fields and remove the File rows.

    Runs for every document in patch v2_8_0, and again whenever a stored file
    is missing, so a document the patch could not finish is picked up later.
    Idempotent; returns True when something was adopted.

    Nothing is removed before its copy is stored. The File rows go in the
    same transaction as the new registry rows, and their bytes are deleted
    only after the commit — and only for files the document owned: a
    signature URL that pointed at the Employee's own File (as 2.6.0 wrote it)
    is copied, never deleted. A file that cannot be read is left as it is and
    logged."""
    name = doc.name
    attached = frappe.get_all(
        "File",
        filters={"attached_to_doctype": doc.doctype, "attached_to_name": name, "is_folder": 0},
        fields=["name", "file_name", "file_url", "attached_to_doctype", "attached_to_name"],
        order_by="creation desc",
    )
    by_file_name = {}
    for row in attached:
        by_file_name.setdefault(row.file_name, row)

    adopted = {}  # File name -> File row, removed once copied
    fields = {}

    def _copy(row, file_name, owned):
        file_doc = frappe.get_doc("File", row.name)
        try:
            content = storage.read_frappe_file(file_doc)
        except Exception:
            frappe.log_error(
                frappe.get_traceback(),
                f"GMP Document: {row.file_url} of {name} could not be moved to the DMS storage",
            )
            return None
        url = storage.put(name, file_name, content)
        if owned:
            adopted[row.name] = row
        return url

    def _row_for(url):
        rows = frappe.get_all(
            "File",
            filters={"file_url": url, "is_folder": 0},
            fields=["name", "file_name", "file_url", "attached_to_doctype", "attached_to_name"],
        )
        mine = [r for r in rows if r.attached_to_doctype == doc.doctype and r.attached_to_name == name]
        return (mine or rows or [None])[0]

    def _is_mine(row):
        return row.attached_to_doctype == doc.doctype and row.attached_to_name == name

    url = doc.attachment_file
    if url and not storage.parse_file_url(url) and url.lower().endswith(ALLOWED_EXTENSIONS):
        row = _row_for(url)
        if row:
            ext = os.path.splitext(url)[1].lower()
            new_url = _copy(row, f"{name}{ext}", owned=_is_mine(row))
            if new_url:
                fields["attachment_file"] = new_url

    # A copy of the source under its controlled name that the field did not
    # point at: dropped when it duplicates the stored source, adopted when no
    # source is stored, and otherwise left alone — it differs, so it is kept.
    for ext in ALLOWED_EXTENSIONS:
        row = by_file_name.get(f"{name}{ext}")
        if not row or row.name in adopted:
            continue
        stored = storage.get_row(name, f"{name}{ext}")
        if not stored:
            new_url = _copy(row, f"{name}{ext}", owned=True)
            if new_url and not storage.parse_file_url(fields.get("attachment_file") or doc.attachment_file):
                fields["attachment_file"] = new_url
            continue
        try:
            content = storage.read_frappe_file(frappe.get_doc("File", row.name))
        except Exception:
            continue
        if hashlib.sha256(content).hexdigest() == stored.sha256:
            adopted[row.name] = row

    for ext in ALLOWED_EXTENSIONS:
        file_name = f"{name}-controlled{ext}"
        row = by_file_name.get(file_name)
        if row and not storage.exists(name, file_name):
            _copy(row, file_name, owned=True)

    # Releases before 2.7.2 could store it with save_file()'s six-character
    # hash suffix; any other PDF attached here (a scan) is not the base PDF.
    suffixed = re.compile(re.escape(name) + r"[0-9a-f]{6}\.pdf")
    base = by_file_name.get(f"{name}.pdf") or next(
        (r for r in attached if suffixed.fullmatch(r.file_name or "")), None
    )
    if base and not storage.exists(name, f"{name}.pdf"):
        _copy(base, f"{name}.pdf", owned=True)

    for field in _SIGNATURE_SNAPSHOT_FIELDS:
        url = doc.get(field)
        if not url or storage.parse_file_url(url):
            continue
        row = _row_for(url)
        if not row:
            continue
        ext = os.path.splitext(row.file_name or url)[1].lower() or ".png"
        owned = _is_mine(row) and (row.file_name or "").startswith(f"{name}-{field}")
        new_url = _copy(row, f"{name}-{field}{ext}", owned=owned)
        if new_url:
            fields[field] = new_url

    if fields:
        frappe.db.set_value(doc.doctype, name, fields, update_modified=False)
        doc.update(fields)
    for row in adopted.values():
        _forget_legacy_file(row)
    return bool(fields or adopted)


def _forget_legacy_file(row):
    """Remove a File row whose bytes now live in the DMS storage; its bytes
    go after the commit, unless another File row still uses them."""
    file_doc = frappe.get_doc("File", row.name)
    s3_key = storage.s3_module().get_s3_key(file_doc) if storage.is_s3_file(file_doc) else None
    path = None if s3_key else file_doc.get_full_path()
    file_url = row.file_url
    frappe.db.delete("File", {"name": row.name})

    def _remove_bytes():
        try:
            if frappe.db.exists("File", {"file_url": file_url}):
                return
            if s3_key:
                # Honours "Delete file from cloud", like any File deletion.
                frappe.get_attr("frappe_s3_attachment.controller.S3Operations")().delete_from_s3(s3_key)
            elif path and os.path.exists(path):
                os.remove(path)
        except Exception:
            frappe.log_error(frappe.get_traceback(), f"GMP Document: could not remove {file_url}")

    frappe.db.after_commit.add(_remove_bytes)


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


def _register_overlay_font():
    """Register the configured font with reportlab and return (bold, regular)
    font names, falling back to ``("Helvetica-Bold", "Helvetica")``.

    Why this matters: reportlab's built-in fonts are WinAnsi-encoded and cannot
    encode Persian at all, so without a registered TTF the watermark and footer
    are limited to Latin. Registering the site's font is what allows them to be
    Persian — combined with shape_rtl(), which supplies the joining and bidi
    reportlab itself does not do.

    Registration is per-process and idempotent. reportlab's registry lives as
    long as the worker, so the font is registered under a name derived from the
    FILE (path, size, mtime), not just the family: keyed on the family alone, a
    corrected .ttf attached in DMS Settings would never replace the first file
    a worker registered. Any failure degrades to Helvetica rather than breaking
    the download."""
    policy = _document_font_policy()
    if not policy:
        return "Helvetica-Bold", "Helvetica"

    family = policy["family"]
    try:
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont

        from dms.dms.doctype.dms_settings.dms_settings import resolve_font_file

        path = resolve_font_file(family)
        if not path:
            return "Helvetica-Bold", "Helvetica"
        stat = os.stat(path)
        file_id = hashlib.sha1(f"{path}|{stat.st_size}|{stat.st_mtime_ns}".encode()).hexdigest()[:12]
        name = f"DMS {family} {file_id}"
        if name not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(TTFont(name, path))
        # One TTF, used for both weights: synthesising a bold face is not worth
        # a second font file, and the watermark is sized for impact anyway.
        return name, name
    except Exception:
        frappe.log_error(
            frappe.get_traceback(), "GMP Document: overlay font registration failed"
        )
        return "Helvetica-Bold", "Helvetica"


def _apply_watermark(pdf_path, watermark_text, footer_text=None):
    """Merge a diagonal watermark and/or a footer line onto every page.

    ``watermark_text=None`` skips the diagonal stamp (plain variant).

    Text is drawn in the font configured in DMS Settings when one is registered,
    which is what lets these strings be Persian; with no configured font they
    fall back to reportlab's built-in Helvetica and must stay WinAnsi-encodable.
    Either way the strings run through shape_rtl(), which applies the Arabic
    joining and bidi reordering reportlab does not do — without it Persian draws
    as disconnected, mirrored letterforms. A configured font that lacks a glyph
    the string needs (a Persian-only build has no Latin at all) is skipped for
    that string in favour of Helvetica: the status words must stay legible.

    The stamp size adapts to the text and page: the font is scaled so the
    string spans ~60% of the page diagonal, capped at 60pt — a long text like
    "UNCONTROLLED COPY" shrinks to fit instead of overrunning the page edges,
    while short texts ("OBSOLETE") don't balloon. Opacity is kept light so the
    document stays readable under the stamp."""
    import math

    from pypdf import PdfReader, PdfWriter
    from reportlab.lib.colors import Color
    from reportlab.pdfbase.pdfmetrics import stringWidth
    from reportlab.pdfgen import canvas

    bold_font, regular_font = _register_overlay_font()
    watermark_text = shape_rtl(watermark_text) if watermark_text else watermark_text
    footer_text = shape_rtl(footer_text) if footer_text else footer_text
    # reportlab draws a missing glyph as a blank box without complaint, so a
    # font with no Latin glyphs would print an OBSOLETE copy with no readable
    # OBSOLETE on it. Checked on the shaped text, which is what gets drawn.
    if watermark_text and not font_covers(bold_font, watermark_text):
        bold_font = "Helvetica-Bold"
    if footer_text and not font_covers(regular_font, footer_text):
        regular_font = "Helvetica"

    reader = PdfReader(pdf_path)
    writer = PdfWriter()

    for page in reader.pages:
        width = float(page.mediabox.width)
        height = float(page.mediabox.height)

        overlay_buffer = BytesIO()
        c = canvas.Canvas(overlay_buffer, pagesize=(width, height))
        if watermark_text:
            font_size = 60.0
            max_span = math.hypot(width, height) * 0.60
            text_span = stringWidth(watermark_text, bold_font, font_size)
            if text_span > max_span:
                font_size *= max_span / text_span
            c.saveState()
            c.translate(width / 2, height / 2)
            c.rotate(math.degrees(math.atan2(height, width)))
            c.setFillColor(Color(0.85, 0.10, 0.10, alpha=0.15))
            c.setFont(bold_font, font_size)
            c.drawCentredString(0, -font_size / 3, watermark_text)
            c.restoreState()

        if footer_text:
            c.saveState()
            c.setFillColor(Color(0.20, 0.20, 0.20, alpha=0.85))
            c.setFont(regular_font, 8)
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
