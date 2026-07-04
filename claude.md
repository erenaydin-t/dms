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
- `version_number` (Int, Default: 1)
- `is_active` (Check, Default: 1)
- `requires_training` (Check, Default: 0)
</database_schema>

<business_logic>
You must implement the following server-side logic in `gmp_document.py`:

1.  **Autonaming (`autoname`):**
    Strict name format: `[department_abbr]-[form_type]-[document_number]-[version]`, e.g. `PR-FRM-0001-1`.
    - Exactly four dash-separated segments — no prefixes, suffixes, spaces, or extra characters (no `v` before the version).
    - `document_number` is zero-padded to 4 digits (`0001`) and increments per department+form-type pair; amended versions share their predecessor's number.
    - `version` starts at 1 (never 0); on amendment the logical base (`[dept]-[type]-[number]`) is retained and only the version segment is bumped.
    *(Note: Fetch the department abbreviation from the Department DocType, assume a custom field `custom_abbr` exists).*

2.  **Date Calculations (`before_save`):**
    If `effective_date` is set, automatically calculate `expiry_date` based on `validity_period` (add years). Set `next_revision_date` to exactly 1 month prior to `expiry_date`.

3.  **File Integrity & Renaming (`before_save`):**
    If `attachment_file` is provided or changed:
    - Calculate the MD5 hash of the physical file and store it in `file_md5_hash`.
    - Rename the physical file on the server to match the document's generated ID (e.g., `SOP-QA-01-v0.docx`) using Frappe's `file_manager`.

4.  **Word Template Engine (`on_submit`):**
    Use the `docxtpl` library. Open the attached `.docx` file, render the context (pass docname, version, effective date, etc., to replace bookmarks/jinja tags in the word file), and overwrite the saved file with the rendered version.

5.  **Revision & Archiving (`on_cancel` or custom amend logic):**
    When a document is amended:
    - Increment `version_number` by 1.
    - Clear `attachment_file`, `file_md5_hash`, and `effective_date`.
    - Set the old document's `is_active` to 0.

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
