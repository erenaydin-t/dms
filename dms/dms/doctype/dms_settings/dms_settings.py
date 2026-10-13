# Copyright (c) 2026, ErenAydin - GMP DMS Module
# License: MIT
"""Single doctype holding the workflow actors that cannot be derived from the
Employee reporting chain: the QA Supervisor, the Regulatory Manager (Technical
Lead), the final QA Approver and the (optional) CEO. Global defaults with
optional per-department overrides; the three routing actors are resolved in
GMPDocument._resolve_workflow_actors_on_submit_for_approval() when a draft is
submitted for approval, the CEO in _stamp_ceo_authorization() at publication.
Also where documents are rendered in (the document font) and where their files
are stored (local disk or S3, see dms.dms.storage)."""

import os
import subprocess
import time

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint

from dms.dms import storage

# Font-file lookups, per process. The answer is a path on THIS container's
# filesystem, so it must not be shared through Redis with containers that may
# not have the file. Keyed by the DMS Settings version, so a save — a new family
# or a newly attached .ttf — reaches every worker on its next render.
_font_file_cache = {}
# A failed lookup is retried after this long, so a font installed after the
# first miss (or an fc-match that timed out on a busy host) is picked up without
# anyone having to re-save DMS Settings.
_FONT_MISS_RETRY_SECONDS = 300


class DMSSettings(Document):
    def validate(self):
        if self.document_font_file and not self.document_font_file.lower().endswith(".ttf"):
            frappe.throw(
                _(
                    "The document font file must be a .ttf — reportlab cannot embed "
                    "OpenType/CFF (.otf) fonts, so the watermark would silently fall "
                    "back to Helvetica."
                ),
                title=_("Unsupported Font Format"),
            )

        seen = set()
        for row in self.department_actors:
            if row.department in seen:
                frappe.throw(
                    _("Department {0} appears more than once in the overrides table.").format(
                        frappe.bold(row.department)
                    )
                )
            seen.add(row.department)

        self._warn_if_font_not_installed()

        if cint(self.store_files_on_s3):
            reason = storage.s3_unavailable_reason()
            if reason:
                frappe.throw(reason, title=_("S3 Cannot Be Used"))

    def _warn_if_font_not_installed(self):
        # A warning, not a block: the font may legitimately be installed after
        # the setting is saved. Until it is, approvals skip enforcement (see
        # GMPDocument._pin_render_font) rather than render in a substitute.
        family = (self.document_font or "").strip()
        if not (self.enforce_document_font and family) or is_font_installed(family):
            return
        frappe.msgprint(
            _(
                "Font {0} is not installed on this server, so it is not enforced yet: "
                "documents approved meanwhile keep their template's own fonts. Install "
                "it in every container that runs LibreOffice and enter the family name "
                "exactly as <code>fc-list : family</code> reports it."
            ).format(frappe.bold(family)),
            title=_("Document Font Not Installed"),
            indicator="orange",
        )


def resolve_document_font():
    """Return the enforced-font policy:
    ``{"enforce": bool, "family": str, "preserve_symbols": bool}``.

    Only the family NAME matters for the document body — LibreOffice resolves it
    through fontconfig at conversion time. The actual font file is needed only
    by reportlab for the watermark/footer overlay; see resolve_font_file()."""
    settings = frappe.get_cached_doc("DMS Settings")
    family = (settings.document_font or "").strip()
    return {
        "enforce": bool(settings.enforce_document_font) and bool(family),
        "family": family,
        "preserve_symbols": bool(settings.preserve_symbol_fonts),
    }


def resolve_font_file(family):
    """Absolute path to a .ttf for ``family``, or None.

    Order: the explicitly attached file, then fontconfig.

    The fontconfig step verifies the family it got back. ``fc-match`` never
    fails — asked for a font that is not installed it silently returns the best
    substitute — so matching on the file alone would happily register DejaVu
    Sans under the name "Vazir" and produce Persian-shaped boxes with no error
    anywhere. We compare the returned family exactly — the same test
    is_font_installed() applies for the document body, so the watermark does
    not accept a font the body would not get.

    Cached per process (this runs on every watermark render, and fc-match is a
    subprocess), keyed by the DMS Settings version. A cached path that has
    disappeared is looked up again; a miss is retried after a few minutes."""
    if not family:
        return None

    settings = frappe.get_cached_doc("DMS Settings")
    attached = settings.document_font_file or ""
    key = (family, attached, str(settings.modified))
    cached = _font_file_cache.get(key)
    if cached:
        path, looked_up_at = cached
        if path and os.path.exists(path):
            return path
        if not path and time.monotonic() - looked_up_at < _FONT_MISS_RETRY_SECONDS:
            return None

    path = _resolve_font_file_uncached(family, attached)
    _font_file_cache.clear()  # only the current settings version is ever asked for
    _font_file_cache[key] = (path, time.monotonic())
    return path


def _resolve_font_file_uncached(family, attached):
    if attached:
        name = frappe.db.get_value("File", {"file_url": attached}, "name")
        if name:
            # On the disk, or a cached copy of a file in the S3 bucket.
            try:
                path = storage.frappe_file_path(frappe.get_doc("File", name))
            except Exception:
                path = attached
            if os.path.exists(path) and path.lower().endswith(".ttf"):
                return path
            frappe.log_error(
                f"Attached document font is unusable: {path} "
                f"(must be an existing .ttf; reportlab cannot embed .otf)",
                "DMS: document font",
            )

    match = _fc_match(family)
    if not match:
        return None
    matched_families, path = match
    if not path or not os.path.exists(path):
        return None
    # fc-match returns a substitute rather than nothing; accept only a real hit.
    # Exact, not a substring: "Sans" must not accept "DejaVu Sans", nor "Vazir"
    # a "Vazirmatn" file that LibreOffice would never pick for the body.
    if not _is_exact_match(family, matched_families):
        frappe.log_error(
            f"Font '{family}' is not installed on this host — fc-match fell back "
            f"to '{', '.join(matched_families)}'. Install the font in every container that "
            f"runs soffice, or attach a .ttf in DMS Settings.",
            "DMS: document font",
        )
        return None
    if not path.lower().endswith(".ttf"):
        return None  # reportlab TTFont cannot embed CFF/OTF outlines
    return path


def is_font_installed(family):
    """True when fontconfig on THIS host resolves ``family`` to that family
    itself rather than to a substitute.

    This is the question the document body depends on: LibreOffice asks
    fontconfig for the family named in the file and draws with whatever comes
    back, without complaint. Enforcing a family that is not installed therefore
    does not fail — it silently re-sets every document in a fallback such as
    DejaVu Sans. The comparison is exact, ignoring case and spaces as
    fontconfig does, so an installed "Vazirmatn" does not satisfy "Vazir".

    Uncached and host-local on purpose: it runs once per approval, next to a
    LibreOffice conversion that costs far more, and each container has its own
    fonts. An unverifiable font (fc-match missing) counts as not installed."""
    match = _fc_match(family) if family else None
    return bool(match) and _is_exact_match(family, match[0])


def _is_exact_match(family, matched_families):
    wanted = _normalize_family(family)
    return any(_normalize_family(f) == wanted for f in matched_families)


def _normalize_family(name):
    return "".join((name or "").split()).lower()


def _fc_match(family):
    """``([family names], file)`` that fontconfig resolves ``family`` to, or
    None when fc-match cannot run. fc-match never answers "not found" — it
    returns its best substitute — so callers must check the families."""
    try:
        out = subprocess.run(
            ["fc-match", "-f", "%{family}|%{file}", family],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    matched_family, _, path = out.partition("|")
    return [f.strip() for f in matched_family.split(",") if f.strip()], path


def resolve_department_actors(department):
    """Return {qa_supervisor, regulatory_manager, qa_approver, ceo} for a
    department: the override row's value when set, else the global default.
    Missing values come back as None — the caller decides whether to throw.

    `ceo` is deliberately optional: unlike the three routing actors it gates
    no transition, it only supplies the CEO authorization block stamped at
    publication, so a site with no CEO sign-off simply leaves it empty."""
    settings = frappe.get_cached_doc("DMS Settings")
    override = None
    for row in settings.department_actors:
        if row.department == department:
            override = row
            break

    def pick(fieldname):
        if override and override.get(fieldname):
            return override.get(fieldname)
        return settings.get(fieldname)

    return {
        "qa_supervisor": pick("qa_supervisor"),
        "regulatory_manager": pick("regulatory_manager"),
        "qa_approver": pick("qa_approver"),
        "ceo": pick("ceo"),
    }


@frappe.whitelist()
def get_storage_summary():
    """Where the stored files are, for the DMS Settings form."""
    frappe.only_for(("System Manager", "DMS Manager"))
    on_s3 = cint(frappe.db.get_single_value("DMS Settings", "store_files_on_s3"))
    return {
        "target": storage.S3 if on_s3 else storage.LOCAL,
        "summary": storage.storage_summary(),
        "s3_unavailable": storage.s3_unavailable_reason(),
    }


@frappe.whitelist(methods=["POST"])
def move_stored_files():
    """Move every stored file to the storage selected in DMS Settings, in a
    background job (one at a time, each verified before its old copy goes)."""
    frappe.only_for(("System Manager", "DMS Manager"))
    target = storage.target_storage()
    frappe.enqueue(
        "dms.dms.doctype.dms_settings.dms_settings.move_stored_files_job",
        queue="long",
        timeout=4 * 3600,
        job_id="dms-move-stored-files",
        deduplicate=True,
        target=target,
        user=frappe.session.user,
    )
    return target


def move_stored_files_job(target, user=None):
    result = storage.move_stored_files(target)
    message = _("Moved {0} stored files to {1}.").format(result["moved"], target)
    if result["failed"]:
        message += " " + _(
            "{0} could not be moved and stayed where they were; see the Error Log, "
            "then run Move Stored Files again."
        ).format(result["failed"])
    frappe.get_doc("DMS Settings").add_comment("Info", message)
    frappe.db.commit()
    if user:
        frappe.publish_realtime("msgprint", message, user=user)
