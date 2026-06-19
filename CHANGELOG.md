# Changelog

All notable changes to the **Lyra DMS** (GMP / 21 CFR Part 11 Document Management System for ERPNext v16) are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
