# DMS — GMP Document Management System for ERPNext v16

A 21 CFR Part 11 / GMP-aware document control module for pharmaceutical
ERPNext deployments. Adds a `GMP Document` DocType that handles versioning,
file integrity, Word template rendering, controlled-copy watermarking, and
revision lifecycle.

## Features

- **Deterministic naming** — `[type]-[department_abbr]-[increment]-v[version]`,
  scoped per `(document_type, department)` pair.
- **File integrity** — SHA-256 of every uploaded `.docx`, recomputed after
  template render so the audit hash matches the distributed bytes.
- **Word template engine** — `docxtpl`-rendered Jinja tags inside the source
  `.docx` are populated at submit time with the document's metadata.
- **Versioned amendments** — Frappe's amend flow auto-bumps `version_number`,
  resets the file/hash/effective date, and inactivates the prior revision.
- **Two-stage PDF pipeline** — DOCX → PDF conversion via LibreOffice runs
  once at submit and is persisted; downloads dynamically apply the
  `CONTROLLED COPY` / `OBSOLETE` watermark from the cached base PDF.
- **Role-gated download** — only users with the `QA Manager` role see the
  in-form "Download PDF" button.

## Requirements

- ERPNext **v16** with Frappe v16
- Python 3.10+
- `libreoffice` (provides the `soffice` binary) installed on every bench host
- Python packages — see `requirements.txt`

## Installation

```bash
# In your bench directory
bench get-app https://github.com/<your-org>/dms.git
bench --site <your-site> install-app dms
bench --site <your-site> migrate
bench restart
```

## One-time configuration

1. Open each **Department** record and set the `custom_abbr` field (e.g.
   `QA`, `QC`, `PROD`). `GMP Document` naming will fail without it.
2. Ensure the `QA Manager` role exists (auto-created by `before_install`)
   and is assigned to relevant users.
3. Verify LibreOffice is callable as `soffice` from the bench user:
   ```bash
   which soffice && soffice --version
   ```

## Authoring Word templates

The Word `.docx` you upload to a GMP Document is auto-filled when QA
approves. Field values are bookmarkable as `{{ field_name }}` placeholders
and three signature placeholders (`{{ preparer_signature }}`,
`{{ reviewer_signature }}`, `{{ qa_signature }}`) inline PNG signatures —
**but only in the PDF output, never in the Word output.**

→ See **[`docs/word-template-guide.md`](docs/word-template-guide.md)** for
the full placeholder list, signature setup, and example template layout.

## Compliance notes (21 CFR Part 11 / GMP)

- **Audit trail** — every save creates an entry in Frappe's standard Version
  table; submit/cancel timestamps are immutable.
- **Integrity** — SHA-256 hash of the controlled file is stored on the
  document and recomputed after every render.
- **Change control** — `reason_for_change` is enforced server-side on every
  amendment.
- **Controlled copy distribution** — every downloaded PDF is watermarked at
  request time based on the *current* `is_active` / `docstatus` state.

## Running tests

```bash
bench --site <test-site> set-config allow_tests true
bench --site <test-site> run-tests --app dms --module dms.dms.doctype.gmp_document.test_gmp_document
```

## License

MIT
