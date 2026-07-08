# ERPNext v16 GMP-DMS Module Development Task

<role>
You are an Expert Frappe & ERPNext (v16) Developer specializing in enterprise-grade, GMP/GxP compliant software architectures. You write clean, scalable, and secure Python and JS code adhering to standard Frappe framework conventions.
</role>

<context>
We are developing a custom App/Module named `DMS` (Document Management System) for an ERPNext v16 instance. The target organization is a Pharmaceutical company requiring strict 21 CFR Part 11 and GMP compliance. 
The core of this module is the `GMP Document` DocType, which handles versioning, Word template rendering, file integrity, and PDF watermarking.
</context>

<database_schema>
Target DocType: `GMP Document`
Properties: Is Submittable = True, Is Tree = True, Naming Rule = By Script.

Core Fields:
- `document_name_fa` (Data, Mandatory)
- `document_name_en` (Data, Mandatory)
- `document_type` (Select: SOP, WI, Form, Protocol, Policy)
- `department` (Link: Department)
- `document_owner` (Link: Employee)
- `gmp_impact` (Select: Critical, Major, Minor)
- `validity_period` (Select: 2 Years, 3 Years, 5 Years)
- `effective_date` (Date, set automatically on final submit)
- `expiry_date` (Date, Read Only)
- `next_revision_date` (Date, Read Only)
- `reason_for_change` (Small Text, Mandatory ONLY if `amended_from` is not empty)
- `attachment_file` (Attach)
- `file_md5_hash` (Data, Read Only)
- `version_number` (Int, Default: 1) — the human *revision* number rendered into the document body (content, not identity). Defaults to 1 for a new document, but a 0-based revision scheme may set it explicitly (first issue == rev 0). Distinct from the name's trailing `version` segment (see Autonaming): `version_number` is not forced and may be 0, whereas the name segment always starts at 1.
- `is_active` (Check, Default: 1)
- `requires_training` (Check, Default: 0)
</database_schema>

<business_logic>
You must implement the following server-side logic in `gmp_document.py`:

1.  **Autonaming (`autoname`):**
    Strict name format: `[department_abbr]-[form_type]-[document_number]-[version]`, e.g. `PR-FRM-0001-1`.
    - Exactly four dash-separated segments — no prefixes, suffixes, spaces, or extra characters (no `v` before the version).
    - `document_number` is zero-padded to 4 digits (`0001`) and increments per department+form-type pair; amended versions share their predecessor's number.
    - `version` here is the name's trailing segment, which reflects the document's **position in its amendment chain** — NOT the `version_number` field. It starts at 1 (never 0) for a brand-new document; on amendment the logical base (`[dept]-[type]-[number]`) is retained and the segment is set to the predecessor's segment + 1. Deriving it from the chain (rather than from `version_number`) keeps names collision-free and always 1-based even when `version_number` is numbered from 0.
    *(Note: Fetch the department abbreviation from the Department DocType, assume a custom field `custom_abbr` exists).*

2.  **Date Calculations (`before_save`) & Effective-Date Scheduling:**
    If `effective_date` is set, automatically calculate `expiry_date` based on `validity_period` (add years). Set `next_revision_date` to exactly 1 month prior to `expiry_date`.
    `effective_date` follows ERPNext's posting-date semantics via the `set_effective_date` checkbox ("Edit Effective Date", mirrors `set_posting_time`):
    - Checkbox OFF (default): system-controlled — any manual value on a draft is silently normalized away, and the QA-approval date is stamped at submit.
    - Checkbox ON: a QA/DMS/System Manager (server-enforced) may backdate or schedule a future date; submit requires the date to be filled.
    - A **future** `effective_date` makes the approved document **pending**: `is_active = 0`, workflow stays Approved, watermark reads "NOT YET EFFECTIVE", and the predecessor (revision flow) remains the effective version. The daily `activate_effective_documents` sweep (runs before the expiry sweep) flips it active on the date, retires the predecessor, and repoints references.

3.  **File Integrity & Renaming (`before_save`):**
    If `attachment_file` is provided or changed:
    - Calculate the MD5 hash of the physical file and store it in `file_md5_hash`.
    - Rename the physical file on the server to match the document's generated ID (e.g., `SOP-QA-01-v0.docx`) using Frappe's `file_manager`.

4.  **Word Template Engine (`on_submit`):**
    Use the `docxtpl` library. Open the attached `.docx` file, render the context (pass docname, version, effective date, etc., to replace bookmarks/jinja tags in the word file), and overwrite the saved file with the rendered version.

5.  **Revision & Archiving (non-destructive revise flow via `create_revision`):**
    Revisions do NOT use Frappe's cancel+amend. A whitelisted `create_revision(docname, reason_for_change)` copies the current effective version into a separate draft record linked back via `revision_of` (Link, read-only) — the source document stays **Approved, submitted and active** the whole time the draft moves through the review workflow:
    - Only an Approved + active + submitted document can be revised, and a document may have at most ONE open revision at a time (cancelled revisions don't count).
    - The draft gets the next chain name segment (`MAX(existing segments) + 1`, so retained cancelled revisions never collide), a candidate `version_number` (predecessor + 1), a cleared `attachment_file`/hash/dates, and a fresh Draft workflow cycle. The predecessor's controlled file is never reused or replaced.
    - On QA approval (`on_submit`), the predecessor is automatically retired: `is_active = 0`, `workflow_status = Obsolete` — but it KEEPS docstatus 1 (submitted, immutable audit record; not a cancel, so Frappe never offers "Amend" on it). The official version number therefore only advances at approval. References are repointed to the new version.
    - Abandoning a draft revision happens via the workflow action "Cancel Revision" (any pre-approval state → terminal state **Revision Cancelled**, doc_status 0). The predecessor is untouched and remains effective; the cancelled draft is retained for audit and `on_trash` blocks its deletion.
    - Legacy amend (`amended_from`) still works for manually cancelled documents and shares the same chain-naming, reset, and guard logic.

6.  **PDF Generation & Watermarking (Whitelisted Method):**
    Create a `@frappe.whitelist()` method `download_watermarked_pdf(docname)`.
    - Convert the `.docx` to PDF (assume LibreOffice `soffice` CLI is available on the host).
    - If `is_active == 1` and docstatus == 1, apply a "CONTROLLED COPY" watermark.
    - If `is_active == 0`, apply an "OBSOLETE" watermark.
    - Return the file URL or stream the file.
</business_logic>

<ui_behavior>
In `gmp_document.js`:
- Hide `reason_for_change` if it's version 0. Make it mandatory if it's an amended document.
- Add a custom button "Download PDF" under the standard 'Actions' or 'Get PDF' menu ONLY for users with the "QA Manager" role. This button calls the `download_watermarked_pdf` whitelisted method.
</ui_behavior>

<instructions>
Based on the provided context, please generate:
1. The complete and robust Python controller code (`gmp_document.py`). Ensure imports are handled (e.g., `hashlib`, `docxtpl`, `subprocess`).
2. The Client Script (`gmp_document.js`).
3. Briefly mention any specific `hooks.py` additions or Frappe app dependencies (e.g., adding `docxtpl` to `requirements.txt`).
</instructions>

<constraints>
- Strictly follow Frappe v16 standards.
- Use Frappe's built-in ORM (`frappe.qb` or `frappe.get_doc`); do not use raw SQL unless necessary.
- Include robust error handling (`frappe.throw`) for missing files or incorrect file extensions (only allow .docx).
</constraints>
