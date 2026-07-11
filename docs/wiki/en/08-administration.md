# Administration & Deployment

## Installation / upgrade (Docker bench)

```bash
# inside the backend container
cd /home/frappe/frappe-bench/apps/dms
git fetch upstream --tags && git reset --hard vX.Y.Z     # or upstream/main
cd /home/frappe/frappe-bench
bench --site <site> migrate
bench build --app dms
# restart backend + workers + scheduler containers
```

Run the same `git reset` in the **frontend** container so its app tree matches, then restart the Python containers. `after_migrate` idempotently re-asserts: custom fields (`Department.custom_abbr`, `Employee.custom_signature_image`), document-type masters, the amend-naming rule, and the workflow (appending any missing states/transitions and re-asserting conditions + self-approval flags).

## Required setup checklist

1. **Roles** — give authors/approvers **QA Manager**; module owners **DMS Manager**. (Administrator implicitly passes all role gates — always verify workflows with a *real* user.)
2. **Departments** — set `custom_abbr` (e.g. QA, HR) on every department that will own documents; naming fails without it.
3. **Signatures** — each preparer/reviewer/QA approver needs an Employee record with `user_id` linked and a PNG/JPG in *Signature (PNG)*.
4. **Workflow** — `GMP Document Workflow` must be **Active** (a disabled workflow breaks every transition with "Workflow not found").
5. **Word Templates** — at least one `GMP Word Template` record (tag mappings may be empty).
6. **LibreOffice** — `soffice` must be on PATH in the backend/worker containers (used for PDF conversion).
7. **Schedulers enabled** — daily jobs `activate_effective_documents` (first) and `expire_gmp_documents`.

## S3 / external attachment offloading — required exemption

If `frappe_s3_attachment` (or similar) is installed, DMS files **must stay on local disk** (rendering, hashing, watermarking and re-stamping read them across requests). In `site_config.json`:

```json
"ignore_s3_upload_for_doctype": ["Data Import", "GMP Document", "GMP Word Template", "Employee"]
```

Symptoms of a missing exemption: *"Attached file is missing on disk"* on save, signature-lookup errors, or unrenderable approvals. Files uploaded against the placeholder name of a new unsaved document count as GMP Document files and stay local.

## REST API surface

| Endpoint | Purpose |
|---|---|
| `POST /api/method/frappe.model.workflow.apply_workflow` | drive transitions (`doc` JSON + `action`) |
| `POST …gmp_document.gmp_document.create_revision` | `docname`, `reason_for_change` → new draft name |
| `GET …gmp_document.gmp_document.download_watermarked_pdf` | `docname` + optional `variant` = `controlled` \| `uncontrolled` \| `plain` |
| `GET …gmp_document.gmp_document.download_word_document` | managers: clean source file |

## Verified by automated E2E (2026-07-09)

A live end-to-end suite (REST-driven, PDF text-layer + OCR verification of every download) covers: creation & naming, the full approval workflow, all three PDF variants (watermarks, Jalali footers, signatures, QA stamp), non-destructive revisions (predecessor stays effective; auto-obsolescence; reference repointing), cancelled revisions (retention + retry naming), the one-open-revision guard, future/backdated effective dates with the activation sweep, per-actor workflow permissions and self-approval, and the API negative cases. Bugs found during the run were fixed in v2.3.x patches: revision drafts insertable without the mandatory attachment, Cancel Revision on bare drafts, and workflow self-approval flags.
