# DMS Quickstart — End-to-End Smoke Test

A 5-minute walkthrough that exercises every part of the system: department
naming, the three-stage workflow, signature uploads, and the asymmetric
Word/PDF download pipeline. Run this on a freshly-installed DMS to confirm
the stack is healthy.

> **Prerequisites.** A running DMS site (e.g. `http://dms.localhost:8080`)
> with `dms`, `erpnext`, and `hrms` installed. Default credentials after
> `./docker/create-site.sh`: `Administrator` / `admin`.

---

## 0. Resolve the hostname (first time only)

If the browser can't reach `dms.localhost`, add it to `/etc/hosts`:

```bash
echo "127.0.0.1 dms.localhost" | sudo tee -a /etc/hosts
```

Open <http://dms.localhost:8080> and log in. You should land directly on
the **DMS workspace** (set as default by the install hooks).

---

## 1. Configure a Department abbreviation

GMP Document IDs are built from `[type]-[dept_abbr]-[NN]-v[version]` —
naming will fail with a clear error if the department has no abbreviation.

1. **DMS workspace → Departments**
2. Open any department (e.g. *Accounts - E*)
3. Find the **Abbreviation** field (added by the DMS bootstrap hook)
4. Enter a short code (e.g. `ACC`, `QA`, `QC`, `PROD`)
5. **Save**

Repeat for every department that will own controlled documents.

---

## 2. Upload signatures (optional, but recommended for full test)

Signatures are PNG images attached to **Employee** records. The DMS finds
the right signature by walking `User → Employee.user_id → Employee.custom_signature_image`.

1. Go to <http://dms.localhost:8080/app/employee>
2. Open an Employee linked to a User (the *User ID* field must be set)
3. Scroll to the **Image** section
4. Find **Signature (PNG)** (added by the DMS bootstrap hook)
5. **Attach** a transparent-background PNG of the handwritten signature
   (~600 × 200 px is a good target)
6. **Save**
7. Repeat for the Employees linked to the **Reviewer** and **QA Approver**
   users you'll use in step 4

> **No Employee record? No problem.** A user without an Employee link
> (e.g. `Administrator`) just gets an empty signature spot in the PDF.
> No errors are thrown.

---

## 3. Author a Word template

Open Microsoft Word (or LibreOffice Writer) and create a `.docx`
containing **Jinja-style placeholders**. Minimum example:

```
GMP Document: {{ document_name_en }} ({{ docname }})
Type:         {{ document_type }}
Department:   {{ department_name }}
Effective:    {{ effective_date }}
Expires:      {{ expiry_date }}
Version:      v{{ version_number }}

────────────────────────────────────────────────────────────
                       Approval Record
────────────────────────────────────────────────────────────

Prepared by:   {{ preparer_signature }}
               {{ prepared_by_name }}

Reviewed by:   {{ reviewer_signature }}
               {{ reviewed_by_name }}     {{ reviewed_on }}

QA Approved:   {{ qa_signature }}
               {{ approved_by_name }}     {{ approved_on }}

{% if reason_for_change %}
Revision reason: {{ reason_for_change }}
{% endif %}
```

Save as `.docx`. The full placeholder reference is in
[`word-template-guide.md`](word-template-guide.md).

---

## 4. Create a GMP Document and walk the workflow

### 4.1 Create the draft

1. **DMS workspace → GMP Documents → + Add GMP Document**
2. Fill in:
   - **Document Name (Farsi)** + **Document Name (English)**
   - **Document Type** = `SOP`
   - **Department** = the one you set an abbreviation for (e.g. *Accounts - E*)
   - **Validity Period** = `2 Years`
3. In the **Workflow** section:
   - **Reviewer** = e.g. `admin@admin.com`
   - **QA Approver** = e.g. `Administrator`
4. **Attachment (.docx)** = upload the template from step 3
5. **Save** → the status pill shows **Draft**, name becomes
   `SOP-ACC-01-v0`

### 4.2 Preparer → Submit for Review

Top-right of the form: **Workflow → Submit for Review**.
Status flips to **Under Review**. A ToDo is created for the Reviewer; the
Reviewer/QA Approver fields lock so the assignments can't drift.

### 4.3 Reviewer → Approve (or Request Revision)

Stay logged in as `Administrator` (System Manager bypasses the actor
check) — or log in as the assigned Reviewer.

- **Workflow → Approve as Reviewer** → status flips to
  **Pending QA Approval**, ToDo bounces to QA.
- *Or* **Request Revision (Reviewer)** → enter a reason → status flips
  to **Revision Requested**, ToDo goes back to the preparer, and the
  reason is captured under the **Latest Revision Request** section.

### 4.4 QA Approver → Approve

**Workflow → Approve as QA**.

This is the moment everything happens server-side:

1. `approved_by` and `approved_on` are stamped
2. `doc.submit()` fires → `docstatus = 1`
3. `on_submit` hook runs `_render_and_generate_pdf()`:
   - **Clean render** of your `.docx` (signatures = empty) → overwrites
     the source file → this is the controlled Word file
   - **With-signatures render** of the same source → converted to PDF
     by LibreOffice → this is the controlled PDF
4. Watermark logic activates: future downloads will overlay
   `CONTROLLED COPY` on the PDF

The status pill now shows **Approved**, dashboard indicator shows
`CONTROLLED COPY` in green.

---

## 5. Download both outputs and verify the asymmetry

Top-right of the approved form: **Get PDF** menu.

| Action                       | Endpoint                          | What you should see                                                |
| ---------------------------- | --------------------------------- | ------------------------------------------------------------------ |
| **Download PDF (signed)**    | `download_watermarked_pdf`        | Text fields filled in, **signatures visible**, `CONTROLLED COPY` watermark overlaid |
| **Download Word (clean)**    | `download_word_document`          | Text fields filled in, **signature placeholders empty**            |

Open both files and confirm the asymmetry. This is the GMP-relevant
behaviour: the editable Word source travels signature-free; only the
controlled PDF carries the actor signatures.

---

## 6. Test the amendment flow

1. **Menu → Cancel** the submitted document → status becomes
   `Cancelled`, `is_active` flips to 0, dashboard indicator switches to
   `OBSOLETE` (red)
2. Re-download the PDF — the watermark is now `OBSOLETE` instead of
   `CONTROLLED COPY` (same base PDF, dynamic overlay)
3. **Menu → Amend** → a new draft opens with:
   - Same logical ID, version bumped: `SOP-ACC-01-v1`
   - `attachment_file`, `file_integrity_hash`, `effective_date` cleared
   - **Reason for Change** field visible AND mandatory
4. Fill **Reason for Change**, re-upload the `.docx`, run through the
   workflow again. The cycle repeats. Each version is a separate
   controlled document with its own audit trail.

---

## 7. Use the Tree View

DMS workspace → **Document Tree** shortcut, or
<http://dms.localhost:8080/app/gmp-document-tree>.

You should see a 3-level hierarchy:

```
▸ Accounts - E (1)
    ▸ SOP (1)
        SOP-ACC-01-v1  —  <your English name>          [ACTIVE]
```

Click expandable rows to drill in; click leaf rows to jump to the form.
Only **submitted** documents appear (drafts are intentionally hidden).

---

## 8. Use the "My Pending" filters

DMS workspace → **GMP Documents** → top toolbar → **My Pending** menu:

| Filter                       | Shows                                                         |
| ---------------------------- | ------------------------------------------------------------- |
| **Awaiting My Review**       | docs where current user is *Reviewer* and status = Under Review |
| **Awaiting My QA Approval**  | docs where current user is *QA Approver* and status = Pending QA Approval |
| **My Revisions to Address**  | docs where current user is *Preparer* and status = Revision Requested |

Or check **DMS workspace → My Tasks** for the underlying ToDo list.

---

## What to do if anything fails

| Symptom                                                       | Likely cause                                                              | Fix                                                                                       |
| ------------------------------------------------------------- | ------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------- |
| `Unknown column 'custom_abbr'` on first save                  | Department custom field not bootstrapped                                  | `bench --site dms.localhost migrate` re-runs `after_migrate` and seeds the field          |
| `Department X must have 'custom_abbr' set`                    | You skipped step 1 for that department                                    | Open the Department record and fill **Abbreviation**                                      |
| Workflow buttons don't appear                                 | Doc is dirty (unsaved), or you aren't the assigned actor                  | Save first; or log in as the assigned user; or use a System Manager account               |
| `Failed to render Word template`                              | Typo in placeholder, undefined variable, mismatched `{% if %}` / `{% endif %}` | Check **Error Log** in the desk for the line number                                       |
| Signatures missing in PDF                                     | Employee has no `custom_signature_image`, or the file isn't a `.png`      | Upload a PNG to *Employee → Signature (PNG)*                                              |
| `LibreOffice (soffice) is not installed`                      | Image was built without LibreOffice                                       | The shipped `Dockerfile` installs it; rebuild the image                                   |

---

## Cleaning up between tests

To wipe the site completely and re-bootstrap from scratch (destroys all
user data):

```bash
docker compose -p docker down -v
./docker/update-dms.sh
./docker/create-site.sh
```

This is what you'd run if a test goes sideways and you want a known-good
starting state.
