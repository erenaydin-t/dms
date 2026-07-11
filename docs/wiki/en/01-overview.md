# DMS — GMP Document Management System

**Version 2.3.x · ERPNext v15/v16 · 21 CFR Part 11 / GMP-oriented**

The DMS app manages **controlled documents** (SOPs, Work Instructions, Forms, Protocols, Policies, …) for a pharmaceutical quality system. Every document is a `GMP Document` record that owns:

- a strict, collision-free **document ID** (`[Dept]-[Type]-[Number]-[Version]`, e.g. `QA-SOP-0001-2`),
- an uploaded **source file** (.docx / .xlsx / .vsdx) rendered through a template engine,
- a QA-signed **base PDF** with electronic signatures and a QA status stamp,
- a full **review → QA-approval workflow** with per-actor authorization,
- **versioning by revision**, where the approved document stays valid until its replacement is approved,
- lifecycle dates (**effective / expiry / next-revision**) with automatic scheduling sweeps,
- watermarked, variant-controlled **PDF distribution** (Controlled / Uncontrolled / Plain copies).

## Architecture

| Layer | Component | Role |
|---|---|---|
| DocType | `GMP Document` (submittable, tree) | The controlled document record and all lifecycle logic (`gmp_document.py`) |
| DocType | `GMP Document Type` | Master of type codes (SOP, WI, FRM, …) — the record name **is** the code used in document IDs |
| DocType | `GMP Word Template` | Tag-mapping profile: maps custom template tags to system fields |
| DocType | `GMP Document Reference` (child) | Cross-references between controlled documents |
| Workflow | `GMP Document Workflow` | Native Frappe workflow driving all state transitions (must stay **Active**) |
| Scheduler | `activate_effective_documents` (daily) | Promotes future-dated approved documents on their effective date |
| Scheduler | `expire_gmp_documents` (daily) | Obsoletes documents past their expiry date |
| Roles | `QA Manager`, `DMS Manager` | Authoring/workflow role and module-owner role |

### Document identity

`[department_abbr]-[type_code]-[number(4)]-[version]` — exactly four dash-separated segments:

- `department_abbr` comes from the Department's `custom_abbr` field (mandatory for naming).
- `type_code` is the `GMP Document Type` record name (e.g. `SOP`).
- `number` is zero-padded to 4 digits and increments per department+type pair. All versions of one document share it.
- The trailing `version` segment is the record's **position in its revision chain** (first issue = 1). It is *not* the `version_number` field: `version_number` is the human revision number rendered inside the document body (it may be 0-based), while the name segment is identity and always starts at 1. Cancelled revisions keep their names, so the next attempt takes the **next** free segment.

### File handling

- The uploaded source file is copied to a **controlled private file** named after the document (`QA-SOP-0001-1.docx`); a SHA-256 integrity hash is stored on the document.
- On QA approval the source is rendered twice: a clean deliverable (no stamp) and a **base PDF** carrying signatures + QA stamp. Watermarks are applied dynamically at download time, so status changes never require re-rendering.
- ⚠️ **S3 offloading must be disabled for DMS files.** If `frappe_s3_attachment` (or similar) is installed, add the DMS doctypes to the exemption list in `site_config.json` — LibreOffice rendering, hashing and re-stamping read these files from local disk across requests:

```json
"ignore_s3_upload_for_doctype": ["Data Import", "GMP Document", "GMP Word Template", "Employee"]
```

(`Employee` is included so signature images stay local for PDF embedding.)
