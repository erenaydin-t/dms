# Changelog

All notable changes to the **Lyra DMS** (GMP / 21 CFR Part 11 Document Management System for ERPNext v16) are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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

[1.1.0]: https://github.com/erenaydin-t/dms/releases/tag/v1.1.0
[1.0.0]: https://github.com/erenaydin-t/dms/releases/tag/v1.0.0
[0.7.0]: https://github.com/erenaydin-t/dms/releases/tag/v0.7.0
[0.6.0]: https://github.com/erenaydin-t/dms/releases/tag/v0.6.0
[0.5.0]: https://github.com/erenaydin-t/dms/releases/tag/v0.5.0
[0.4.0]: https://github.com/erenaydin-t/dms/releases/tag/v0.4.0
[0.3.0]: https://github.com/erenaydin-t/dms/releases/tag/v0.3.0
[0.2.0]: https://github.com/erenaydin-t/dms/releases/tag/v0.2.0
[0.1.0]: https://github.com/erenaydin-t/dms/releases/tag/v0.1.0
