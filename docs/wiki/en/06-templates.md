# Templates, Tags & Bookmarks

The uploaded source file (.docx via `docxtpl`, .xlsx, .vsdx) is a **template**: at QA approval the system replaces Jinja tags with live document data, signatures and the QA stamp. The pristine uploaded file is preserved as an immutable render source, so re-renders always see the original tags.

## Using tags in a Word document

Place tags anywhere in the .docx body, headers or footers:

```
Document ID: {{ docname }}
Title: {{ document_name_en }}
Version: {{ version_number }}
Effective Date: {{ effective_date }}      Expiry Date: {{ expiry_date }}
Reason for Change: {{ reason_for_change }}

Prepared by: {{ prepared_by_name }}   {{ preparer_signature }}
Reviewed by: {{ reviewed_by_name }}   {{ reviewer_signature }}
QA Approved by: {{ approved_by_name }} {{ qa_signature }}
QA Stamp: {{ qa_stamp }}
```

> Type each tag in one go (don't edit it letter-by-letter) so Word keeps it in a single text run.

## Native tag catalog

**Identity / names:** `docname` (= `name`), `document_name_fa`, `document_name_en`
**Classification:** `document_type` (label), `document_type_code`, `department`, `department_name`, `document_owner`, `document_owner_name`, `gmp_impact`, `validity_period`
**Lifecycle:** `effective_date`, `expiry_date`, `next_revision_date`, `version_number`, `is_active`, `requires_training`, `workflow_status`
**Change control:** `reason_for_change`
**Actors:** `prepared_by`, `prepared_by_name`, `reviewer`, `reviewer_name`, `qa_approver`, `qa_approver_name`, `reviewed_by`, `reviewed_by_name`, `reviewed_on`, `approved_by`, `approved_by_name`, `approved_on`
**Images (rendered only in the signed PDF, empty in the clean deliverable):** `preparer_signature`, `reviewer_signature`, `qa_signature`, `qa_stamp`

## Custom tags (GMP Word Template)

Every document links a **GMP Word Template** record, whose *Field Mappings* table aliases custom tags to system fields — e.g. map `my_title` → `document_name_en` and author templates with `{{ my_title }}`. Aliases are additive; native tags keep working. An alias may also point at a signature tag.

## Signatures

Signature images come from **Employee → Signature (PNG)** (`custom_signature_image`) of the acting/assigned user, inlined at a fixed width. The QA stamp image is chosen by status (approved / rejected) at render time.
