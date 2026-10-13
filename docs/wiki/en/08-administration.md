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
2. **Set the family name exactly as the server reports it.** Recent Vazir releases are named **`Vazirmatn`**, not `Vazir`. A name fontconfig cannot resolve would silently fall back to a substitute, so the module checks it at every approval: a family that is not installed is **not enforced** — the document keeps its template's own fonts and the Error Log (*DMS: document font*) says why. Saving DMS Settings with a missing family shows a warning.
3. Leave **Preserve Symbol Fonts** ticked unless you have a reason not to: GMP forms often draw checkboxes as Wingdings glyphs, and replacing those with a text font turns every ☑ into a stray letter.
4. Only if the font cannot be located automatically, attach a **`.ttf`** under *Font File for Watermarks*. TrueType only — reportlab cannot embed OpenType/CFF (`.otf`). A newly attached file takes effect on the next download, with no restart; a font installed on the server is picked up within five minutes.

What enforcement covers: the body, `docDefaults`, headers, footers, footnotes, numbering and the theme of the `.docx`; every populated cell of an `.xlsx`; the FaceNames table of a `.vsdx` (best-effort — Visio support in LibreOffice is thin); and the PDF watermark and footer. A watermark or footer the font cannot draw (a Persian-only font has no Latin glyphs) falls back to Helvetica so the status stays legible.

**Each document keeps the font it was approved in.** The font is frozen on the document at QA approval (*Font Applied at Approval*), and retirement, cancellation, expiry and regeneration of a missing file all re-render with that font, never with the current setting — otherwise changing the setting would silently re-set approved documents and change their integrity hash. A new font therefore applies only to documents approved after the change.

**Why this is needed for Persian.** OOXML gives every run three font slots — Latin, East Asian, and **complex script** (`w:cs`). Persian is complex-script, so `w:cs` is the slot that decides its font, and Word's ordinary font box only writes the Latin one. That mismatch is the usual reason a template "has the font set" yet still renders Persian in something else. The module writes all four slots and strips the theme attributes that would otherwise override them.

**Direction is left alone on purpose.** LibreOffice handles shaping, ligatures and RTL correctly once the font resolves, and the paragraph direction comes from the template. Forcing direction globally would break Latin paragraphs, so the module fixes the font and nothing else.

## Required setup checklist

1. **Roles** — authors **DMS Initiator**; supervisors, reviewers, QA supervisors and regulatory managers **DMS Approver**; the final QA approver **QA Manager**; module owners **DMS Manager** (see *Roles*). (Administrator implicitly passes all role gates — always verify workflows with a *real* user.)
2. **Departments** — set `custom_abbr` (e.g. QA, HR) on every department that will own documents; naming fails without it. The abbreviation and every document-type code become segments of the document ID, so they may not contain spaces, dashes, underscores, `%` or path characters. A document type already used by documents cannot be renamed.
3. **Signatures** — each preparer/reviewer/QA approver needs an Employee record with `user_id` linked and a PNG/JPG in *Signature (PNG)*.
4. **Workflow** — `GMP Document Workflow` must be **Active** (a disabled workflow breaks every transition with "Workflow not found").
5. **Word Templates** — at least one `GMP Word Template` record (tag mappings may be empty).
6. **LibreOffice** — `soffice` must be on PATH in the backend/worker containers (used for PDF conversion).
7. **Schedulers enabled** — daily jobs `activate_effective_documents` (first) and `expire_gmp_documents`.

## File storage (local disk or S3)

Each GMP Document's files — the controlled source, the rendered deliverable, the signed base PDF and the signature snapshots — are kept by the DMS itself, not as attachments, and listed in **DMS Stored File** (document, file, where it is, SHA-256). They are downloaded only through the DMS actions (see *Roles › Files of a document*).

- **Local (default):** `sites/<site>/private/files/dms/<document>/`. Included in `bench backup --with-files`. These files have no File record, so `/private/files/…` never serves them.
- **S3:** in the bucket of `frappe_s3_attachment`, under `<Folder Name>/dms/<document>/`, private. Documents are still rendered on the server: it fetches what it renders into a temporary cache (`/tmp/dms-cache/<site>`, at most 512 MB, verified on every use), renders, and stores the results in the bucket.

**Integrity.** Every read is checked against the recorded SHA-256. A file changed outside the DMS — on the disk or in the bucket — is refused with *"… does not match its recorded checksum"* and an Error Log entry *DMS: stored file failed its integrity check*; it is never served, rendered or regenerated over. A new version is written next to the old one and the old one is removed only after the transaction commits, so a failed approval or re-render never leaves a half-written file.

### Switching to S3

1. Install or update `frappe_s3_attachment` to **16.3.0** or later and configure **S3 File Attachment** (bucket, key, secret, region; for ArvanCloud also the endpoint URL).
2. **DMS Settings › File Storage** → tick **Store DMS Files on S3** → Save. From now on new files go to the bucket.
3. Click **Move Stored Files**. It runs in the background: each file is copied, read back and checked against its SHA-256, and only then removed from the disk. A file that fails stays where it was and goes to the Error Log — click again to retry. The table under the checkbox shows how many files are where, and a comment on DMS Settings records each run.

To go back, untick the box and click **Move Stored Files** again. Files are always read from wherever they are, so the setting can be changed at any time.

If the bucket cannot be reached, approvals, re-renders and downloads stop with a clear message; nothing is half-written. Turn on **bucket versioning** if your provider offers it: the DMS refuses a changed file, versioning lets you get the original back.

### `site_config.json` with `frappe_s3_attachment`

```json
"ignore_s3_upload_for_doctype": ["Data Import", "GMP Document"]
```

- Keep **GMP Document**: an upload then stays on the disk until the DMS has stored it, and is removed. Without it the DMS still reads the upload from the bucket, but the bucket keeps a copy whenever *Delete file from cloud* is off.
- **Employee** no longer needs to be listed with `frappe_s3_attachment` 16.3.0+: the DMS reads signature images from the bucket. (Earlier versions made an Employee's uploaded signature their profile photo.) Signatures already on the disk move with *S3 File Attachment › Migrate Existing Files*, which since 16.3.0 also updates the Employee's link to them and honours this list.
- **GMP Word Template** holds no files and can be dropped from the list.

### Upgrading to 2.8.0

`migrate` moves every document's files out of its attachments into the DMS storage (on the disk), one document at a time. A document that fails is logged (*DMS 2.8.0: files of … were not moved*) and is picked up again automatically the next time one of its files is needed. Other attachments — a scan added in the sidebar — stay attachments.

## REST API surface

| Endpoint | Purpose |
|---|---|
| `POST /api/method/frappe.model.workflow.apply_workflow` | drive transitions (`doc` JSON + `action`) |
| `POST …gmp_document.gmp_document.create_revision` | `docname`, `reason_for_change` → new draft name |
| `GET …gmp_document.gmp_document.download_watermarked_pdf` | `docname` + optional `variant` = `controlled` \| `uncontrolled` \| `plain` |
| `GET …gmp_document.gmp_document.download_word_document` | managers: clean source file |

## Verified by automated E2E (2026-07-09)

A live end-to-end suite (REST-driven, PDF text-layer + OCR verification of every download) covers: creation & naming, the full approval workflow, all three PDF variants (watermarks, Jalali footers, signatures, QA stamp), non-destructive revisions (predecessor stays effective; auto-obsolescence; reference repointing), cancelled revisions (retention + retry naming), the one-open-revision guard, future/backdated effective dates with the activation sweep, per-actor workflow permissions and self-approval, and the API negative cases. Bugs found during the run were fixed in v2.3.x patches: revision drafts insertable without the mandatory attachment, Cancel Revision on bare drafts, and workflow self-approval flags.
