# Authoring a Word Template for the DMS

This guide explains how to design a Microsoft Word `.docx` template that will
be auto-filled by the DMS module when a GMP Document is approved.

The DMS uses **[docxtpl](https://docxtpl.readthedocs.io/)**, which understands
**Jinja-style placeholders** of the form `{{ field_name }}` placed anywhere
inside the body, table cells, headers, footers, or footnotes of a `.docx`
file. These placeholders are *not* the same thing as Word's built-in
"Bookmarks" feature — placeholders are simple text that the DMS replaces
when the document is finalised.

> **Two outputs from one template.** When a GMP Document is QA-approved the
> DMS produces two artefacts from your `.docx`:
> - **`<docname>.docx` — the controlled Word file** (downloaded via *Get PDF →
>   Download Word*). All field placeholders are filled in. **Signatures are
>   _not_ embedded.**
> - **`<docname>.pdf` — the controlled PDF** (downloaded via *Get PDF →
>   Download PDF*). All field placeholders are filled in **and signatures
>   appear at every signature placeholder.** A `CONTROLLED COPY` /
>   `OBSOLETE` watermark is overlaid at download time.
>
> This is intentional: the editable Word source is signature-free for safe
> distribution; only the PDF carries actor signatures.

---

## 1. Quick start

1. Open Word and create a new `.docx` file.
2. Type a placeholder anywhere — for example:
   ```
   Document ID: {{ docname }}
   ```
3. Save the file.
4. Upload it as the **Attachment** on a new GMP Document.
5. Walk through the workflow: *Submit for Review → Reviewer Approve → QA
   Approve.*
6. After QA Approve, download the Word and PDF — the placeholder is replaced
   by the document's ID.

That's the whole trick: write `{{ field_name }}` and the DMS substitutes it.

---

## 2. Available field placeholders

Every editable field on the GMP Document is exposed as a placeholder. You
can also reference resolved human-readable names (`*_name`) where useful.

### Identifiers

| Placeholder        | Description                                  | Example              |
| ------------------ | -------------------------------------------- | -------------------- |
| `{{ docname }}`    | Auto-generated document ID with version      | `SOP-QA-01-v2`       |
| `{{ name }}`       | Same as `docname` (Frappe alias)             | `SOP-QA-01-v2`       |

### Names

| Placeholder                | Description                |
| -------------------------- | -------------------------- |
| `{{ document_name_fa }}`   | Document name in Farsi     |
| `{{ document_name_en }}`   | Document name in English   |

### Classification

| Placeholder                  | Description                                         |
| ---------------------------- | --------------------------------------------------- |
| `{{ document_type }}`        | `SOP`, `WI`, `Form`, `Protocol`, `Policy`           |
| `{{ department }}`           | Department record ID                                |
| `{{ department_name }}`      | Human-readable department name                      |
| `{{ document_owner }}`       | Employee record ID                                  |
| `{{ document_owner_name }}`  | Employee full name                                  |
| `{{ gmp_impact }}`           | `Critical`, `Major`, `Minor`                        |
| `{{ validity_period }}`      | `2 Years`, `3 Years`, `5 Years`                     |

### Lifecycle dates

| Placeholder                  | Description                                                 |
| ---------------------------- | ----------------------------------------------------------- |
| `{{ effective_date }}`       | Date the controlled copy goes live                          |
| `{{ expiry_date }}`          | Computed: effective + validity period                       |
| `{{ next_revision_date }}`   | Computed: 1 month before expiry                             |

### Versioning

| Placeholder              | Description                                   |
| ------------------------ | --------------------------------------------- |
| `{{ version_number }}`   | Integer; bumped on every amendment            |
| `{{ is_active }}`        | `1` for the controlled copy, `0` for obsolete |
| `{{ requires_training }}`| `1` / `0`                                     |

### Change control

| Placeholder                  | Description                                                |
| ---------------------------- | ---------------------------------------------------------- |
| `{{ reason_for_change }}`    | Mandatory on amendments; empty for first version           |

### Workflow assignments

| Placeholder                  | Description                                                |
| ---------------------------- | ---------------------------------------------------------- |
| `{{ prepared_by }}`          | Login email of the user who created the doc                |
| `{{ prepared_by_name }}`     | That user's full name                                      |
| `{{ reviewer }}`             | Login email of the assigned reviewer                       |
| `{{ reviewer_name }}`        | Reviewer's full name                                       |
| `{{ qa_approver }}`          | Login email of the assigned QA approver                    |
| `{{ qa_approver_name }}`     | QA approver's full name                                    |

### Workflow actuals (set during the approval cycle)

| Placeholder                  | Description                                                 |
| ---------------------------- | ----------------------------------------------------------- |
| `{{ reviewed_by }}`          | Login email of the user who actually reviewed (= reviewer)  |
| `{{ reviewed_by_name }}`     | Reviewer's full name (resolved at approval time)            |
| `{{ reviewed_on }}`          | Datetime the reviewer approved                              |
| `{{ approved_by }}`          | Login email of the QA approver                              |
| `{{ approved_by_name }}`     | QA approver's full name (resolved at approval time)         |
| `{{ approved_on }}`          | Datetime QA approved (= effective date)                     |
| `{{ workflow_status }}`      | `Approved` once QA has finalised                            |

---

## 3. Signature placeholders

Three special placeholders insert PNG signatures **only in the PDF
output** (they render as nothing in the Word output):

| Placeholder                    | Source                                                                    |
| ------------------------------ | ------------------------------------------------------------------------- |
| `{{ preparer_signature }}`     | `prepared_by`'s Employee → *Signature (PNG)*                              |
| `{{ reviewer_signature }}`     | `reviewed_by`'s Employee → *Signature (PNG)* (falls back to assigned reviewer pre-approval) |
| `{{ qa_signature }}`           | `approved_by`'s Employee → *Signature (PNG)* (falls back to assigned QA approver pre-approval) |

### How signatures get registered

1. Open the **Employee** record for the person who will sign.
2. In the *Image* section, find **Signature (PNG)** — added by the DMS app.
3. Click **Attach** and upload a transparent-background PNG of the
   signature. Recommended dimensions: ~600 × 200 px.
4. Save.

The DMS will inline that PNG every time that Employee's user prepared,
reviewed, or QA-approved a GMP Document — at exactly the position of the
matching `{{ ..._signature }}` placeholder.

### What if the user has no Employee record (e.g. Administrator)?

The signature placeholder renders as empty space — no error. So you can
freely place signature placeholders in templates that may be approved by
users without Employee records; you just won't get a signature image at
that spot.

### Sizing

Signatures render at **40 mm wide** by default, with the height
auto-calculated to preserve the PNG aspect ratio. The placeholder behaves
like text, so put it on its own paragraph (or on a table cell) for clean
alignment.

---

## 4. Recommended template layout

A signature block typically goes at the end of a controlled document.
Here's a Word table you can drop in:

```
+------------------------+------------------------+------------------------+
|       Prepared by      |      Reviewed by       |       QA Approved by   |
+========================+========================+========================+
|                        |                        |                        |
| {{ preparer_signature }}| {{ reviewer_signature }}| {{ qa_signature }}    |
|                        |                        |                        |
+------------------------+------------------------+------------------------+
| {{ prepared_by_name }} | {{ reviewed_by_name }} | {{ approved_by_name }} |
+------------------------+------------------------+------------------------+
|                        | {{ reviewed_on }}      | {{ approved_on }}      |
+------------------------+------------------------+------------------------+
```

The cells with the `_signature` placeholders will:
- Stay empty in the **.docx** output → safe to send by email or store
  externally without revealing handwritten signatures.
- Contain the actual PNG signatures in the **.pdf** output → suitable for
  printing as a controlled paper copy.

---

## 5. Conditionals and loops (advanced)

docxtpl supports the full Jinja2 syntax. Examples:

```
{% if requires_training %}
  Training is REQUIRED before this document is followed.
{% endif %}

{% if reason_for_change %}
  Reason for this revision: {{ reason_for_change }}
{% endif %}
```

For loops over child tables (we don't ship any on GMP Document yet, but
this is how it would look):

```
{% for row in items %}
  - {{ row.description }} ({{ row.qty }})
{% endfor %}
```

See the [docxtpl docs](https://docxtpl.readthedocs.io/) for the full
syntax reference.

---

## 6. Common pitfalls

| Symptom                                           | Cause                                                                          | Fix                                                                       |
| ------------------------------------------------- | ------------------------------------------------------------------------------ | ------------------------------------------------------------------------- |
| Placeholder rendered as `{{ docname }}` literally | Word "auto-corrected" the curly braces                                         | Turn off Word's smart quotes / autocorrect for that paragraph and retype  |
| `Failed to render Word template`                  | Typo in placeholder, undefined variable, mismatched `{% if %}` / `{% endif %}` | Open Error Log → look for the line number; fix and re-upload              |
| Signature missing in PDF                          | Employee has no `custom_signature_image`, or the file is not a PNG             | Upload a PNG to Employee → Signature (PNG)                                |
| Signature too small / too large                   | All signatures use a fixed 40 mm width                                         | Resize the source PNG so its aspect ratio matches the slot you want       |

---

## 7. Lifecycle of a template

1. **Author** — write the `.docx` once with placeholders.
2. **Upload** — attach to a draft GMP Document. The system stores the
   raw template alongside the doc.
3. **Submit for Review → Reviewer Approve → QA Approve** — the workflow
   advances. **No rendering happens until QA approves.**
4. **QA Approve** — the system makes two parallel renders: a clean
   `.docx` (replaces the source) and a signed `.docx` (converted to PDF
   immediately, then discarded).
5. **Distribute** — users with the *QA Manager* role download the Word
   and/or watermarked PDF.
6. **Amend** — when the doc is amended, the new draft starts fresh:
   re-upload a (potentially updated) template; same workflow runs again.

The template is never re-rendered after QA approval, so signatures and
field values reflect the state at the time of approval. This is by design
— a controlled document must be immutable once distributed.
