# Changelog

All notable changes to the **Lyra DMS** (GMP / 21 CFR Part 11 Document Management System for ERPNext v16) are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [2.7.0] - 2026-08-11

### Added
- **Enforced document font — first-class Persian support.** A single family, set in **DMS Settings → Document Font**, is now applied to everything the module generates, *regardless of what each Word/Excel/Visio template specifies*. Templates no longer have to be re-authored, and one careless upload can no longer produce a document in Calibri.
  - **DOCX**: every font reference is rewritten after the template render and before PDF conversion — body, `docDefaults` in `styles.xml` (which is what runs with no explicit font inherit from), **headers and footers**, footnotes, endnotes, numbering and the DrawingML theme. Applied to the distributed clean `.docx` as well as the PDF source, so both come out in the same font.
  - **All four OOXML font slots** are set — `w:ascii`, `w:hAnsi`, `w:eastAsia` and, critically, **`w:cs` (complex script)**. Persian and Arabic are complex-script, so `w:cs` is the slot that actually decides their font; Word's normal font box only writes `w:ascii`, which is the usual reason "I set the font but the Persian text didn't change". Theme attributes (`w:asciiTheme`, `w:cstheme`, …) are **removed** rather than rewritten, because they outrank the explicit family and would silently win.
  - **XLSX**: every populated cell is restyled to the family after the text pass. **VSDX**: best-effort rewrite of the FaceNames table shapes reference — unverified against libvisio's PDF export, like the rest of the Visio path.
  - **Watermarks and footers** are drawn in the configured font too: the overlay registers the TTF with reportlab instead of using built-in Helvetica, so Persian is now renderable there at all. Since reportlab applies neither contextual joining nor bidi, text passes through a new `shape_rtl()` (arabic-reshaper + python-bidi) first — without it Persian draws as disconnected, mirrored letterforms.
  - **Symbol fonts are preserved by default** (`Preserve Symbol Fonts`). GMP forms routinely draw checkboxes as Wingdings glyphs, and rewriting those to a text font turns every ☑ into a stray letter. Untick for a literal every-font-replaced pass.
  - Nothing is hardcoded: family, enforcement on/off, symbol preservation and an optional `.ttf` are all DMS Settings fields.

### Notes on behaviour
- **Paragraph direction is deliberately not touched.** Shaping, ligatures and RTL in the document body are LibreOffice's job and it does them correctly once the font resolves; forcing direction would wreck Latin paragraphs, and correctly-authored Persian already carries its own direction marks. This release makes the *font* right, which is what was missing.
- The watermark/footer strings stay English. They are now *translatable* — with a font configured they render correctly — but flipping them is left to Translation records so a site without a font configured cannot end up with unreadable boxes on a controlled PDF.
- `fc-match` never fails: asked for a font that is not installed it silently returns a substitute. The resolver therefore **verifies the family it got back** and refuses a mismatch, logging to the Error Log, rather than registering DejaVu Sans under the name "Vazir".

### Upgrade notes
- **Install the font on the server yourself** — the module enforces a family, it does not ship one. It must be visible to LibreOffice in **every container that runs `soffice`** (backend *and* the queue workers), not just one. Verify with `fc-list : family | grep -i vazir` inside each.
- Set **DMS Settings → Font Family** to the family name **exactly as the server reports it**. Note that recent Vazir releases are named **`Vazirmatn`**, not `Vazir`.
- `bench --site <site> migrate` then `bench restart`. New Python dependencies (`arabic-reshaper`, `python-bidi`) install with the app; if you build a custom image, rebuild it.
- Only needed if the font cannot be located automatically: attach a **`.ttf`** under *Font File for Watermarks*. It must be TrueType — reportlab cannot embed OpenType/CFF (`.otf`), and the setting rejects it rather than silently falling back.
- Existing approved documents keep the PDF they were approved with; the font applies to documents rendered from now on.

## [2.6.1] - 2026-08-11

### Fixed
- **Approving a document could destroy an employee's signature image (data loss, introduced in 2.6.0).** The new `supervisor_signature` / `ceo_signature` snapshot fields stored the *URL of the employee's own signature File*. Frappe's global `attach_files_to_document` hook — which runs on the `on_update` of every doctype — claims any **unattached** File named by an `Attach`/`Attach Image` field and re-parents it to the document being saved. So the first supervisor approval silently re-attached that employee's signature to the GMP Document, and deleting or purging that document then deleted the signature outright: the Employee kept a `custom_signature_image` pointing at a File that no longer existed, and every later save naming them as Reviewer or QA Approver failed with "the signature file record is missing".
  - The snapshot now **copies** the image into a private file owned by the document (via `_own_private_file`, with `attached_to_field` set so the hook does not insert duplicate rows either). The employee's own File is never touched. Copying is also the truthful thing for a controlled record: the bytes actually applied at that stage stay with the document even after the employee replaces their signature.
  - **If you deployed 2.6.0 and approved anything, check for damage** before upgrading — see the upgrade notes.

### Upgrade notes
- Upgrade straight to 2.6.1; no new patch, no schema change. `bench migrate` → `bench build` → `bench restart`.
- **2.6.0 deployments only.** Find employees whose signature pointer is now dangling:
  ```sql
  SELECT e.name, e.employee_name, e.user_id, e.custom_signature_image
    FROM `tabEmployee` e
   WHERE e.custom_signature_image IS NOT NULL AND e.custom_signature_image != ''
     AND NOT EXISTS (SELECT 1 FROM `tabFile` f WHERE f.file_url = e.custom_signature_image);
  ```
  Re-upload the signature on each row returned (Employee → *Signature (PNG)*). Also check for signature Files wrongly re-parented to a GMP Document — `SELECT name, file_url, attached_to_name FROM \`tabFile\` WHERE attached_to_doctype = 'GMP Document' AND file_url LIKE '%sig%'` — and clear `attached_to_doctype`/`attached_to_name` on any that is an employee signature rather than a document's own snapshot.

## [2.6.0] - 2026-08-11

### Added
- **Approval timeline.** Every stage of the chain now stamps a report-friendly `Date` alongside the precise timestamp it already recorded: `preparer_date`, `supervisor_date`, `reviewer_date`, `qa_supervisor_date`, `regulatory_date`, `manager_date`, `qa_approver_date` and `publish_date`, grouped in a new collapsible **Approval Timeline** section. Turnaround between any two stages is now a plain subtraction in the report builder instead of datetime arithmetic. All eight are exposed to Word templates as `{{ ..._date }}` tags, together with `{{ current_date }}` (the date the copy is printed, resolved at render time rather than stored).
- **The QA Supervisor stage now records who cleared it and when** — `qa_supervisor_approved_by` / `qa_supervisor_approved_on`. That step previously stamped nothing at all, leaving a hole in the middle of the audit trail. When QA is cleared through the delegated review queue rather than a direct approval, the stage is credited to the QA Supervisor who owns the delegation; the individual reviewers and their verdicts stay on the queue rows.
- **`submitted_on`** — the moment a draft entered the approval chain.
- **CEO authorization block.** An optional `ceo` actor in DMS Settings (global default plus per-department override, like the QA Approver) is stamped onto the document at publication as `ceo`, `ceo_name`, `ceo_date` and `ceo_signature`, and is available to templates as `{{ ceo_name }}`, `{{ ceo_date }}` and `{{ ceo_signature }}`. The name is captured as text so the record keeps the name that was in force at publication. Leave the setting empty and the block stays empty and hidden — no workflow stage is added.
- **`DMS Proxy Approver` role — delegated approval.** A named, auditable alternative to sharing the Administrator account when an approver is unavailable. The role holder may perform **any** stage's action on **any** document: `install.py` ships a proxy twin of every actor-gated workflow transition, so the single role grant is enough on its own — none of the other DMS roles are needed. Cancelling a draft revision is deliberately *not* delegated.
  - Each delegated action is recorded on both sides: `<stage>_approved_by` names the account that actually acted (what 21 CFR Part 11 §11.200 requires of an electronic signature), and the new `<stage>_on_behalf_of` names the role-holder it was performed for. A **Delegated Approvals** section and an orange form banner show "X on behalf of Y — date" at a glance.
  - The **signature** follows the represented person, so the printed signature block reads as the assigned approver's, with `{{ <stage>_on_behalf_of_name }}` available to disclose the delegation beside it.
  - Administrator's long-standing escape-hatch approvals are now recorded the same way instead of silently.
- **Signature snapshots.** `supervisor_signature` and `ceo_signature` freeze the image that was applied when the stage completed, so a later change to someone's Employee signature cannot retroactively alter an approved document. Both are also rendered into the PDF, joining the preparer/reviewer/QA signatures.
- **More template tags:** `supervisor`, `qa_supervisor`, `regulatory_manager` and `ceo` (with matching `_name` variants), plus `{{ supervisor_signature }}` and `{{ ceo_signature }}`.

### Changed
- **The workflow is now seeded once and then owned by the site.** `GMP Document Workflow` is created on first install with the full chain; from then on it is yours to edit from the standard **Workflow** page, and **no upgrade will overwrite your changes**. Previously every `bench migrate` re-asserted the shipped states, transitions, conditions, roles and `allow_edit`, silently undoing deliberate customisation. `after_install`/`after_migrate` now only create the workflow when it is missing.
  - **This removes the automatic repair.** A transition whose `allowed` role is lost — most often by *renaming a Role record*, which rewrites every link and locks real users out of the Actions menu — no longer heals itself on migrate. The repair is now explicit: `bench --site <site> execute dms.install.restore_workflow_defaults` (the former `_sync_gmp_workflow`). It re-asserts the shipped definition and will overwrite your customisations to those rows, which is the point of running it; rows you added by hand are left alone.
  - **Releases that genuinely need new workflow rows now ship a one-time additive patch** instead of continuous synchronisation. This release does exactly that for the delegated-approval transitions, below.

### Fixed
- **Signature resolution could disagree between output formats.** The `.docx`/PDF pass and the `.xlsx`/`.vsdx` overlay each resolved signatures independently; both now share one `_signature_paths()` resolution, so the same image is applied whatever the source format.

### Upgrade notes
- Run `bench --site <site> migrate`, then `bench build` and `bench restart`.
- Patch `v2_6_0.backfill_approval_timeline` fills the new Date fields on existing documents from the timestamps each stage already recorded, and sets `publish_date` from `effective_date` on documents that are the effective version. Two stages are left blank because no historical source exists: `preparer_date` (nothing recorded when a draft entered the chain before this release) and `qa_supervisor_date` (the stage stamped nothing at all). The CEO block is **not** backfilled — inventing a CEO authorization for documents approved without one would fabricate a controlled record; already-approved documents show an empty CEO block until they are next revised.
- Patch `v2_6_0.add_proxy_transitions` adds the delegated-approval transitions to an **existing** workflow (a fresh install gets them from the seed). It is strictly additive and one-time: it only appends rows, never edits or deletes one, it mirrors the transitions your site actually has — copying each row's own `next_state`, so a re-routed chain gets twins following *your* routing — it skips steps you removed, and it skips any row whose `condition` you rewrote rather than guessing at it. Don't want delegated approval? Never grant the role, or delete the twin rows afterwards; nothing will put them back.
- To use delegated approval: grant **DMS Proxy Approver** to the standing-in user. Grant it sparingly — it confers approval authority at every stage, in every department.
- To use the CEO block: set **DMS Settings → CEO** (and/or a per-department override), and upload that user's signature on their Employee record. Documents already approved keep an empty block.

## [2.5.0] - 2026-07-27

### Fixed
- **Watermark size and opacity.** The diagonal watermark was a fixed 80pt at 30% opacity — "UNCONTROLLED COPY" overran the page edges and darkened the content. The font now scales to span ~60% of the page diagonal (capped at 60pt, so long texts shrink to fit and short ones don't balloon), the stamp aligns to the page diagonal, and opacity is reduced to 15% so the document stays readable.
- **Excel-sourced PDFs cut wide sheets into part-pages.** Sheets whose author configured no print scaling exported at 100% scale on the template's stored paper size. `render_xlsx` now sets A4 + fit-to-width (unlimited pages tall) on such sheets; templates with explicit fitToPage or a custom print scale are respected untouched.

### Changed
- **Removed the "Download PDF (Controlled Copy)" button** from the Get PDF menu. The menu now offers Uncontrolled Copy, Plain, and (managers) the clean Word file.

### Upgrade notes
- Rebuild the app assets (`bench build`) so the form UI picks up the button change; no migrate-side changes in this release.

## [2.4.0] - 2026-07-27

### Changed
- **Approval chain reordered: Regulatory Validation now comes before Manager Approval.** The chain is Draft → Supervisor → Under Review → QA Supervisor → [QA queue] → **Pending Regulatory Validation** → **Pending Manager Approval** → Final QA → Approved. "Validate (Regulatory)" advances to Pending Manager Approval and "Approve (Manager)" to Pending Final QA Approval; the sequential QA queue hands completed rounds to the Regulatory Manager. One-level returns follow the new order (Regulatory → QA Supervisor, Manager → Regulatory, Final QA → Manager); the old return rows are removed on migrate. Audit stamps keep their meaning (`regulatory_validated_*` on leaving Regulatory, `manager_approved_*` on leaving Manager).
- **"Cancel Revision" is no longer offered after supervisor approval.** The action now exists only in Draft, Revision Requested and Pending Supervisor Approval; the Under Review and Pending QA Supervisor rows are removed on migrate.
- **GMP Document Types are no longer seeded.** The GMP Document Type master is fully user-maintained. Patch `v1_4_0.remove_seeded_document_types` deletes the previously seeded rows on existing sites — only where the code and label still match the old seed and no GMP Document references the type.

### Added
- **DMS Settings link** in the DMS workspace under the Configuration card.

### Upgrade notes
- Run `bench --site <site> migrate`, then `bench restart`. The migrate re-points the workflow transitions, deletes the retired return/cancel rows and removes unused seeded document types.
- Documents parked in `Pending Manager Approval` at upgrade time will proceed to Final QA on the manager's approval without a regulatory validation stamp for that cycle — return them to Regulatory via "Return to Regulatory" if validation is required.
- If your custom Docker image predates it, ensure `libreoffice-calc` and `libreoffice-draw` are installed (see `docker/Dockerfile`) — without them, xlsx/vsdx PDF export fails with "PDF was not generated by LibreOffice".

## [1.3.1] - 2026-06-21

### Fixed
- **Reviewer/QA signature not rendered when the workflow step was performed by someone else.** The PDF resolved the signature only from the *actual* actor (`reviewed_by` / `approved_by`); if a step was done via the Administrator escape-hatch (or by any account without a signature), no signature rendered — even though the assigned Reviewer/QA had one (and the 1.3.0 validation passed, since it checks the assigned users). Rendering now resolves the actual signer's signature **and falls back to the assigned `reviewer` / `qa_approver`**, whose signature 1.3.0 validation guarantees — so a reviewer/QA signature is always present on an approved document. Added a regression test.

### Upgrade notes
- Run `bench --site <site> migrate`, then `bench restart`. **Note:** `bench migrate` does *not* update app code — make sure the `dms` app is actually on this version (`bench version` should show dms 1.3.1) by pulling it (`bench update --pull`, or `cd apps/dms && git fetch --tags && git checkout v1.3.1 && cd ../.. && bench build`) before migrating.

## [1.3.0] - 2026-06-21

### Added
- **Reviewer / QA Approver signature validation.** Saving or submitting a GMP Document now requires the assigned **Reviewer** and **QA Approver** to each have a usable signature image — a linked Employee (`Employee.user_id`) with `custom_signature_image` uploaded in PNG/JPG/JPEG. If a selected user lacks one, the save is blocked with a clear message naming the user and the reason (no Employee linked / no signature uploaded / file missing / wrong format), so a document can never reach approval and render with a missing reviewer/QA signature. Enforced server-side in `validate()` (`_validate_signatures`); the Reviewer/QA fields also pre-check on the form via a new `check_signature` endpoint and warn immediately on selection.

### Upgrade notes
- Run `bench --site <site> migrate`, then `bench restart`. Ensure the users assigned as Reviewer and QA Approver have a signature image on their Employee record, or those documents can no longer be saved. (The signature still renders from the *actual* reviewer/approver — `reviewed_by`/`approved_by` — so those should be the signature-bearing users.)

## [1.2.6] - 2026-06-21

### Fixed
- **Generated PDF could contain a different document's content (critical).** A document's PDF — especially on amendment — could render the wrong content, including content from a completely different document. Root cause: Frappe deduplicates uploaded files by content hash, so two byte-identical uploads (e.g. each version started from the same base file) are pointed at a single shared physical file; meanwhile the clean-render step overwrote the controlled `.docx` **in place without updating its `File.content_hash`**, leaving the hash stale and poisoning dedup. A subsequent upload then resolved to an already-rendered file, and the in-place rename/overwrite bled one document's content into another's render (and corrupted the other document's controlled file). Each GMP Document now writes its **own independent controlled `.docx`** (bytes written directly, bypassing dedup) and keeps that File's `content_hash` in sync on every render, so uploads and renders are fully isolated per document. Added end-to-end regression tests (identical-content uploads across two documents, and an amendment re-uploading content derived from the original).

### Upgrade notes
- Run `bench --site <site> migrate`, then `bench restart`. Documents approved before this fix that were affected should be re-amended/re-approved to regenerate a correct controlled file and PDF.

## [1.2.5] - 2026-06-20

### Fixed
- **No "Amend" button after cancelling an approved document (couldn't create a new version).** Cancelling moves the document into the **Obsolete** workflow state, and Frappe hides the Amend action whenever the current workflow state makes the form read-only for the user. Obsolete's `allow_edit` was `DMS Manager`, so a plain **QA Manager** (the approver who cancels) was treated as read-only and never saw Amend. Obsolete is now editable by **QA Manager**, so preparers/approvers can revise a cancelled document into a new version. (Administrator and module owners who also hold `QA Manager` were unaffected; the underlying amend/versioning logic was already correct — this was purely the button-visibility gate.) `_sync_gmp_workflow` re-asserts this on existing installs. Added a regression test.

### Upgrade notes
- Run `bench --site <site> migrate`, then `bench restart`.

## [1.2.4] - 2026-06-20

### Added
- **End-to-end PDF / template / role / regression validation suite (`test_e2e_pdf.py`).** Real documents are created, driven through the workflow to Approved (rendering the Word template and converting to PDF via LibreOffice), and the generated PDF bytes/text are extracted and compared:
  - **Template differentiation** — two documents on different templates produce non-identical PDFs with the correct, non-cross-contaminated content.
  - **Multi-version** — a document revised through v0…v4; every PDF carries its own version number with no stale content, and all five differ pairwise.
  - **Independent documents** — unique PDFs and isolated reference trees.
  - **Role-based access** — Owner / DMS Manager / QA Manager / Employee / outsider against a real approved document and its PDF download; cross-department denial; clean-Word manager-only.
  - **Regression hunts** — direct-submit workflow bypass is blocked; an approver signature is embedded in the rendered PDF.
- **CI now provisions Python 3.14** (matching Frappe's current `version-16` requirement) so `bench init` and the test run succeed in CI.

### Notes
- No product code changes vs 1.2.3 — this release bundles the runtime-validated test suite and the working CI test job. The full DMS suite is **67 tests, green on a live ERPNext + HRMS + DMS site**. The deep audit found no defects in the shipped behaviour; the only fixes were to the test harness itself (mandatory fields, content-hash dedup of dummy uploads, `copy_doc` docstatus, and unique per-version document content).

## [1.2.3] - 2026-06-20

### Added
- **Runtime test coverage + CI execution (release-readiness audit).** A new `test_permissions.py` suite plus CI that actually runs the tests on a real ERPNext + HRMS + DMS site (previously CI only did static checks, so the suite had never executed). Coverage: `has_permission`, `get_permission_query_conditions`, `_visibility_scope`, `_user_departments`, department-scoped vs. unrestricted access, named-participant access, the reference tree (existing/deleted/missing-root/cross-department/nesting/circular/large-graph/depth), `get_dms_tree_children` scoping, and the workflow `allow_edit` configuration. The DOCX→PDF end-to-end tests run under LibreOffice in CI. Full suite: 59 tests green on a live site.

### Fixed
- **Reference-tree recursion depth hardening.** `get_document_reference_tree` now coerces the whitelisted `depth` argument safely and clamps it to `MAX_REFERENCE_TREE_DEPTH` (10), so a malformed or oversized `depth` can neither crash nor drive runaway traversal of a dense graph.
- **Test suite was unrunnable (pre-existing, surfaced by the first real run).** The legacy `GMP Document` tests had drifted: the build helper never set the now-mandatory `reviewer`/`qa_approver`, the dummy-attachment helper wrote identical bytes (Frappe content-dedup collapsed "distinct" uploads), and the amend tests didn't reset `docstatus` after `copy_doc`. Fixed so the suite passes end-to-end.

### Upgrade notes
- Run `bench --site <site> migrate`, then `bench restart`. (No schema change beyond 1.2.2; the depth clamp is internal.)

## [1.2.2] - 2026-06-20

### Fixed
- **Reference tree crashed on a dangling reference (regression from 1.2.1).** The per-document permission check added in 1.2.1 (`frappe.has_permission(..., doc=name)`) loads the document via `frappe.get_doc`, which raised `DoesNotExistError` when a referenced GMP Document had been deleted (now possible since 1.2.0 gave `DMS Manager` delete rights) — taking down the whole reference-tree render. `get_document_reference_tree` now guards the root and every reference with `frappe.db.exists` and silently omits missing targets, restoring the pre-1.2.1 graceful degradation. A missing root yields a clean `DoesNotExistError` instead of an uncaught crash.

### Changed
- **Reference-tree performance.** Each node is now loaded once with `frappe.get_doc` and reused for the permission check, the label, and child enumeration (via the already-loaded `references` child table), removing the redundant `get_value` label lookup and the separate reference query that 1.2.1 incurred per node.

### Tests
- Added regression coverage: a document referencing a deleted target renders without a 500 and omits the deleted node; a non-existent root raises a clean `DoesNotExistError`.

### Upgrade notes
- Run `bench --site <site> migrate`, then `bench restart`.

## [1.2.1] - 2026-06-20

### Fixed
Hardening of the 1.2.0 access-control model (from a recall-biased review):
- **`DMS Manager` could not actually edit documents.** The active Workflow gates editing by each state's single `allow_edit` role, which listed only `QA Manager` / `System Manager` — so the new admin role got a read-only form despite its DocType write perm. `DMS Manager` now owns `allow_edit` for the in-pipeline/submitted states (Under Review, Pending QA Approval, Approved, Obsolete); Draft / Revision Requested stay with `QA Manager` for authors. `_sync_gmp_workflow` re-asserts this on existing installs. (A module owner who also authors drafts should hold both roles — see the guide.)
- **Reference tree leaked across departments.** `get_document_reference_tree` only ran a doctype-level read check, letting a scoped member pass any docname and read names/status of other departments' documents. It now checks read permission on the root document and omits any referenced document the caller cannot read.
- **Tree endpoint missing a read check.** `get_dms_tree_children` now calls `frappe.has_permission("GMP Document", "read", throw=True)`, so a user linked to a department but lacking the GMP read role can no longer enumerate document names/counts.
- **Read-only members could trigger writes.** Downloading a controlled PDF whose base file was missing ran `_render_and_generate_pdf` (which mutates the document and File records) from a read-only session. Regeneration is now restricted to manager/admin roles; members get a "temporarily unavailable" message.
- **Redundant Employee lookups.** `_user_departments` is now memoised per request (`frappe.flags`), so the repeated `has_permission` checks in one request no longer issue duplicate Employee queries.

### Upgrade notes
- Run `bench --site <site> migrate`, then `bench restart`.

## [1.2.0] - 2026-06-16

### Added
- **Department-scoped, role-based access control.** A new permission model on `GMP Document`:
  - **Read-only department members** (`Employee` role) now see only the **approved, active** controlled copies of the department(s) they belong to — resolved from their linked **Employee** record (`Employee.user_id` → `department`) — plus any document on which they are personally named. They can open those documents and download the watermarked **Controlled Copy PDF**, but cannot edit/create/cancel.
  - **New `DMS Manager` role** (module owner / admin): full create / edit / cancel / delete / amend access to every document in every department, regardless of creator. Seeded on install and migrate (and via a `v1_2_0` pre-model-sync patch on existing sites).
  - `QA Manager` (workflow actors) and `System Manager` continue to see and manage everything.
  - Enforced by `permission_query_conditions` (lists/reports/search) and `has_permission` (single doc + download endpoints) hooks; the **GMP Document Tree** applies the same scope.
- **Controlled-copy PDF download for members.** The in-form *Get PDF → Download PDF (Controlled Copy)* action is now available to any reader of an approved document (server-enforced); the clean **Word** download remains a manager-only control-distribution action.
- **Permissions guide** — `docs/permissions-guide.md` documents the model and how to configure roles, Employee links, and department scoping from the panel.

### Upgrade notes
- Run `bench --site <site> migrate`, then `bench restart`. Assign the `DMS Manager` role to module owners and ensure read-only consumers have the `Employee` role **and** an Employee record with `User ID` + `Department` set.

## [1.1.2] - 2026-06-16

### Fixed
- **Documents still hidden from users not named on them (completes the 1.1.1 fix).** 1.1.1 only flagged `document_owner` (→ Employee), but a User Permission on the **User** doctype was still applied through the `reviewer`, `qa_approver`, `prepared_by`, `reviewed_by`, `approved_by`, and `last_revision_by` Link fields — so an approver (or anyone not named on the document) couldn't see it. All of those fields, plus `department`, now set `ignore_user_permissions`, restoring purely role-based visibility. (Revert `department` if department-scoped visibility is later wanted.)
- **`Value missing for Attachment (.docx)` when amending.** On amend, `before_insert` decided whether an attachment was inherited from the predecessor by comparing `file_url` *strings*. Frappe deduplicates uploads by content hash, so a freshly attached `.docx` could be handed the predecessor's `file_url` and was wrongly nulled, failing the mandatory check. Inheritance is now determined by File *ownership* (is the `File` still attached to the predecessor?), so a newly uploaded revision file is always kept.

### Upgrade notes
- Run `bench --site <site> migrate`, then `bench restart`.

## [1.1.1] - 2026-06-15

### Fixed
- **Documents hidden once a Document Owner is set.** The `document_owner` Link field (→ Employee) lacked `ignore_user_permissions`, so any existing *Employee* User Permission was auto-applied to it — restricting a document to users whose allowed-Employee set included its owner. The field now ignores user permissions, restoring normal role-based visibility (matching the sibling `parent_gmp_document` field).
- **`AttributeError: 'GMPWordTemplate' object has no attribute 'template_file'` on save.** Schema/controller were already file-less since 1.1.0; this adds a `v1_1_1` patch that purges the leftover `template_file` column, Custom Field, and Property Setter on sites upgraded from 1.0.0 and rebuilds the cached meta. (If the error persists after migrate, `bench restart` to drop the stale in-memory controller.)
- **Replacing a `.docx` attachment served the old file.** The previous `File` row was never removed; because the controlled URL is deterministic (`{docname}.docx`), it ended up sharing a `file_url` with the new file and `_get_file_doc()` could resolve to it (and the unchanged URL let caches return stale bytes). Superseded `File` rows are now purged on every attachment change and the document cache is cleared.

### Upgrade notes
- Run `bench --site <site> migrate`, then `bench restart`.

## [1.1.0] - 2026-06-13

### Changed
- **Word Template flow (breaking change vs 1.0.0).** Templates are now **file-less** — a `GMP Word Template` consists only of a **Template Title** and **Tag Mappings**. Removed the template file upload (and the `document_type`, `is_active`, and `description` fields), the file-scan endpoint, and the "Scan Template Tags" button.
- On a `GMP Document`, both **Word Template** and the **`.docx` attachment** are now **mandatory**. The user uploads their own `.docx` and selects a template; the backend renders the user's file using the template's `custom_tag → system_field` mappings, then proceeds through the workflow.
- The render source is always the uploaded attachment (overwritten in place with the clean render); the template supplies only the mappings.
- On amendment, only an *inherited* attachment is cleared — a freshly uploaded revision file is kept so it satisfies the new mandatory rule.

### Fixed
- **Approver signature missing from the generated PDF.** The approver's signature is resolved from `approved_by` at render time, which was stamped only by the `on_update` workflow side-effect — running in the same save as the approval submit and skippable, leaving `approved_by` empty (preparer/reviewer were unaffected as their stamps are committed earlier). `on_submit` now stamps the approver (`_stamp_approver`) before the PDF render, guaranteeing the signature is embedded.

### Upgrade notes
- Run `bench --site <site> migrate`. This **drops the `template_file` column** from `GMP Word Template` (data loss there is expected).

## [1.0.0] - 2026-06-13

First stable release.

### Added
- **Searchable document types.** `document_type` is now a Link to a new `GMP Document Type` master (20 seeded types; short codes used in document IDs), replacing the hardcoded Select.
- **Word template engine.** New `GMP Word Template` library and `GMP Template Field Mapping` child table, with custom-tag → system-field mapping (text **and** signature images) driven by a single `TEMPLATE_FIELDS` catalog.
- `v0_8_0` patch remapping legacy `Form`/`Protocol`/`Policy` values to `FORM`/`PROT`/`POL` (document IDs left immutable for traceability).

### Fixed
- **Intermittent missing signatures.** Employee resolution is now deterministic when a user is linked to multiple Employee records (prefer one with a signature, then Active); accepts `.png/.jpg/.jpeg` instead of PNG-only.
- **Reference tree leaking a previously-open document into new records** — the HTML wrapper is now cleared before the `is_new()` guard.

### Changed
- Version files (`__init__.py`, `setup.py`) reconciled to a single source of truth.

## [0.7.0] - 2026-06-03

### Fixed
- PDF resolution, cancel status, and amend naming; injected the native Frappe Workflow.

## [0.6.0] - 2026-05-23

### Fixed
- Workflow type bugs; added document cross-references.

## [0.5.0] - 2026-04-28

### Added
- Auto-inject the Frappe Workflow on install.

## [0.4.0] - 2026-04-26

### Added
- PNG signatures; every field made bookmarkable in Word templates.

## [0.3.0] - 2026-04-26

### Added
- Three-stage workflow (Prepared / Reviewed / QA Approved).

## [0.2.0] - 2026-04-26

### Added
- GMP Document Tree page (Department → Type → Latest version) and the DMS workspace.

### Fixed
- Amended documents use `autoname` (`-v1`, `-v2`) instead of `-1`, `-2`.

## [0.1.0] - 2026-04-26

### Added
- Initial release of the GMP Document DocType: versioning, autonaming, file integrity hashing, Word template rendering, and PDF watermarking.

[1.3.1]: https://github.com/erenaydin-t/dms/releases/tag/v1.3.1
[1.3.0]: https://github.com/erenaydin-t/dms/releases/tag/v1.3.0
[1.2.6]: https://github.com/erenaydin-t/dms/releases/tag/v1.2.6
[1.2.5]: https://github.com/erenaydin-t/dms/releases/tag/v1.2.5
[1.2.4]: https://github.com/erenaydin-t/dms/releases/tag/v1.2.4
[1.2.3]: https://github.com/erenaydin-t/dms/releases/tag/v1.2.3
[1.2.2]: https://github.com/erenaydin-t/dms/releases/tag/v1.2.2
[1.2.1]: https://github.com/erenaydin-t/dms/releases/tag/v1.2.1
[1.2.0]: https://github.com/erenaydin-t/dms/releases/tag/v1.2.0
[1.1.2]: https://github.com/erenaydin-t/dms/releases/tag/v1.1.2
[1.1.1]: https://github.com/erenaydin-t/dms/releases/tag/v1.1.1
[1.1.0]: https://github.com/erenaydin-t/dms/releases/tag/v1.1.0
[1.0.0]: https://github.com/erenaydin-t/dms/releases/tag/v1.0.0
[0.7.0]: https://github.com/erenaydin-t/dms/releases/tag/v0.7.0
[0.6.0]: https://github.com/erenaydin-t/dms/releases/tag/v0.6.0
[0.5.0]: https://github.com/erenaydin-t/dms/releases/tag/v0.5.0
[0.4.0]: https://github.com/erenaydin-t/dms/releases/tag/v0.4.0
[0.3.0]: https://github.com/erenaydin-t/dms/releases/tag/v0.3.0
[0.2.0]: https://github.com/erenaydin-t/dms/releases/tag/v0.2.0
[0.1.0]: https://github.com/erenaydin-t/dms/releases/tag/v0.1.0
