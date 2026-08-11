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

Run the same `git reset` in the **frontend** container so its app tree matches, then restart the Python containers. `after_migrate` idempotently re-asserts the schema the code cannot run without: custom fields (`Department.custom_abbr`, `Employee.custom_signature_image`), the module roles and the amend-naming rule.

### The workflow is yours

`GMP Document Workflow` is **seeded once, then owned by the site.** The module creates it on first install with the full chain — states, transitions, per-actor conditions, roles and `allow_edit` — and after that never touches it. Edit it freely from **Workflow** in the desk UI: add states, re-route transitions, change roles, adjust conditions. **Upgrades will not overwrite your changes** (since v2.6.0; before that, every migrate re-asserted the shipped definition).

Two consequences worth knowing:

- **New workflow features from an upgrade will not appear by themselves.** Where a release genuinely needs new rows, it ships a one-time, strictly additive patch (v2.6.0 does this for the delegated-approval transitions) — the release notes say so. Nothing else is added or changed.
- **Nothing self-heals any more.** If a transition's `allowed` role is lost — most often by *renaming a Role record*, which rewrites every link and silently locks real users out of the Actions menu — the workflow stays broken until you fix it. To put the shipped definition back deliberately:

  ```bash
  bench --site <site> execute dms.install.restore_workflow_defaults
  ```

  This re-asserts the module's states, transitions, conditions, roles and routing, and drops the transitions of retired chains. **It will overwrite your customisations to those rows** — that is the point of it. Rows you added by hand are left alone. Rename roles for display via **Translation** records, never by renaming the Role.

The controller keys off `workflow_status` *values*, so renaming a state in the workflow without remapping the `WF_*` constants in `gmp_document.py` will break the audit stamping for that stage. Adding states, actions and routing of your own is safe.

## Document font (Persian / RTL)

The module **enforces** one font across everything it generates — it does not ship one. Install the font yourself, then name it in **DMS Settings → Document Font**.

1. **Install it in every container that runs `soffice`** — the backend *and* the queue workers. A font present in only one container produces documents that render differently depending on which worker picked up the job. Verify in each:
   ```bash
   fc-list : family | grep -i vazir
   ```
2. **Set the family name exactly as the server reports it.** Recent Vazir releases are named **`Vazirmatn`**, not `Vazir`. A name fontconfig cannot resolve falls back to a substitute — silently, which is why the module verifies the match and writes to the Error Log when it fails.
3. Leave **Preserve Symbol Fonts** ticked unless you have a reason not to: GMP forms often draw checkboxes as Wingdings glyphs, and replacing those with a text font turns every ☑ into a stray letter.
4. Only if the font cannot be located automatically, attach a **`.ttf`** under *Font File for Watermarks*. TrueType only — reportlab cannot embed OpenType/CFF (`.otf`).

What enforcement covers: the body, `docDefaults`, headers, footers, footnotes, numbering and the theme of the `.docx`; every populated cell of an `.xlsx`; the FaceNames table of a `.vsdx` (best-effort — Visio support in LibreOffice is thin); and the PDF watermark and footer.

**Why this is needed for Persian.** OOXML gives every run three font slots — Latin, East Asian, and **complex script** (`w:cs`). Persian is complex-script, so `w:cs` is the slot that decides its font, and Word's ordinary font box only writes the Latin one. That mismatch is the usual reason a template "has the font set" yet still renders Persian in something else. The module writes all four slots and strips the theme attributes that would otherwise override them.

**Direction is left alone on purpose.** LibreOffice handles shaping, ligatures and RTL correctly once the font resolves, and the paragraph direction comes from the template. Forcing direction globally would break Latin paragraphs, so the module fixes the font and nothing else.

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
